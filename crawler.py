"""
Crawler interpelli di chitarra (AB55 / AB56).

Per ogni sito in data/scuole.csv (+ data/siti_extra.txt):
  1. apre la home e le pagine "Interpelli" già individuate;
  2. raccoglie i link che parlano di interpelli/supplenze;
  3. apre solo quelli nuovi (pagina + allegati PDF) e cerca AB55, AB56 o "chitarra";
  4. se trova qualcosa manda una notifica Telegram e lo annota in trovati.md.

Rispetta robots.txt, fa poche richieste per sito e si ferma entro un tempo massimo.

Uso:
    python crawler.py                       # giro normale
    python crawler.py --prova               # stampa le notifiche invece di inviarle
    python crawler.py --siti mio_elenco.txt # usa un elenco diverso (un URL per riga)
"""
import argparse
import asyncio
import csv
import datetime as dt
import html
import io
import json
import os
import re
import sys
import time
from urllib import robotparser
from urllib.parse import urldefrag, urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from pypdf import PdfReader

UA = os.environ.get("CRAWLER_UA", "CrawlerInterpelliChitarra/1.0 (uso personale)")
UA_TOKEN = "CrawlerInterpelliChitarra"

# Cosa cerchiamo
RX_CHITARRA = re.compile(r"\bA\s?B\s?[-–]?\s?5[56]\b|chitarr", re.I)
# Link che vale la pena aprire
RX_PAROLE = re.compile(r"interpell|supplenz|messa a disposizione|\bA\s?B\s?[-–]?\s?5[56]\b|chitarr", re.I)
# Link che di solito sono allegati
RX_ALLEGATO = re.compile(r"\.pdf($|\?)|download|allegat|attachment|getfile|scarica", re.I)

MAX_NUOVE_PAGINE_PER_SITO = 10   # pagine nuove aperte per sito a ogni giro
MAX_ALLEGATI_PER_PAGINA = 4
MAX_PDF_BYTES = 8_000_000
MAX_PDF_PAGINE = 8
GIORNI_MEMORIA = 150             # dopo quanto dimenticare un link già visto
SITI_IN_PARALLELO = 40
PAUSA_TRA_RICHIESTE = 0.4        # secondi, sullo stesso sito

STATO = "data/stato.json"
TROVATI = "trovati.md"


# ---------------------------------------------------------------- utilità

def norm(url: str) -> str:
    return urldefrag(url)[0].rstrip("/")


def stesso_sito(a: str, b: str) -> bool:
    ha, hb = urlparse(a).hostname or "", urlparse(b).hostname or ""
    return ha.removeprefix("www.") == hb.removeprefix("www.")


def estratto(testo: str, m: re.Match, raggio: int = 80) -> str:
    s = testo[max(0, m.start() - raggio): m.end() + raggio]
    return " ".join(s.split())


def carica_siti(percorso_csv: str, extra: str, solo: str | None):
    siti = []
    if solo:
        with open(solo, encoding="utf-8") as f:
            for r in f:
                r = r.strip()
                if r and not r.startswith("#"):
                    siti.append({"nome": urlparse(r).hostname, "provincia": "", "sito": r})
        return siti
    if os.path.exists(percorso_csv):
        with open(percorso_csv, encoding="utf-8") as f:
            siti += list(csv.DictReader(f))
    if os.path.exists(extra):
        with open(extra, encoding="utf-8") as f:
            for r in f:
                r = r.strip()
                if r and not r.startswith("#"):
                    parti = [p.strip() for p in r.split("|")]
                    siti.append({"nome": parti[1] if len(parti) > 1 else urlparse(parti[0]).hostname,
                                 "provincia": parti[2] if len(parti) > 2 else "",
                                 "sito": parti[0]})
    return siti


def carica_stato():
    if os.path.exists(STATO):
        with open(STATO, encoding="utf-8") as f:
            return json.load(f)
    return {"visti": {}, "sezioni": {}, "segnalati": {}}


def salva_stato(stato):
    oggi = dt.date.today()
    limite = (oggi - dt.timedelta(days=GIORNI_MEMORIA)).isoformat()
    stato["visti"] = {u: d for u, d in stato["visti"].items() if d >= limite}
    os.makedirs(os.path.dirname(STATO), exist_ok=True)
    tmp = STATO + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(stato, f, ensure_ascii=False, indent=0, sort_keys=True)
    os.replace(tmp, STATO)


# ---------------------------------------------------------------- rete

class Sito:
    """Gestisce le richieste verso un singolo sito: robots.txt, pause, errori SSL."""

    def __init__(self, client: httpx.AsyncClient, home: str):
        self.client = client
        self.home = home
        self.robots = None
        self.ultima = 0.0
        self.ssl_rilassato = False

    async def _get(self, url: str):
        attesa = PAUSA_TRA_RICHIESTE - (time.monotonic() - self.ultima)
        if attesa > 0:
            await asyncio.sleep(attesa)
        self.ultima = time.monotonic()
        try:
            return await self.client.get(url)
        except httpx.ConnectError as e:
            # molti siti scolastici hanno certificati scaduti o incompleti: leggiamo solo pagine pubbliche
            if "CERTIFICATE" in str(e).upper() or "SSL" in str(e).upper():
                self.ssl_rilassato = True
                async with httpx.AsyncClient(verify=False, headers=self.client.headers,
                                             timeout=self.client.timeout, follow_redirects=True) as c:
                    return await c.get(url)
            raise

    async def permesso(self, url: str) -> bool:
        if self.robots is None:
            rp = robotparser.RobotFileParser()
            try:
                r = await self._get(urljoin(self.home, "/robots.txt"))
                rp.parse(r.text.splitlines() if r.status_code == 200 else [])
            except httpx.HTTPError:
                rp.parse([])
            self.robots = rp
        return self.robots.can_fetch(UA_TOKEN, url)

    async def scarica(self, url: str):
        if not await self.permesso(url):
            return None
        r = await self._get(url)
        if r.status_code >= 400:
            return None
        return r


def testo_pdf(dati: bytes) -> str:
    try:
        lettore = PdfReader(io.BytesIO(dati))
        return "\n".join((p.extract_text() or "") for p in lettore.pages[:MAX_PDF_PAGINE])
    except Exception:
        return ""


def analizza_html(r: httpx.Response):
    """Restituisce (testo principale, link interessanti [(url, testo)], link allegati)."""
    soup = BeautifulSoup(r.text, "html.parser")
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    principale = soup.find("main") or soup.find("article") or soup.body or soup
    testo = principale.get_text(" ", strip=True)

    interessanti, allegati = [], []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        url = norm(urljoin(str(r.url), href))
        etichetta = " ".join((a.get_text(" ", strip=True) + " " + (a.get("title") or "")).split())
        if RX_PAROLE.search(etichetta) or RX_PAROLE.search(href):
            interessanti.append((url, etichetta))
        if RX_ALLEGATO.search(href) and principale in a.parents:
            allegati.append((url, etichetta))
    return testo, interessanti, allegati


# ---------------------------------------------------------------- logica per sito

async def controlla_sito(client, scuola, stato, scadenza, risultati, contatori):
    home = scuola["sito"]
    sito = Sito(client, home)
    oggi = dt.date.today().isoformat()
    sezioni = stato["sezioni"].setdefault(norm(home), [])
    da_listare = [home] + sezioni
    gia_listate = set()
    coda = []  # (url, etichetta, pagina_di_provenienza)

    def segnala(url, etichetta, dove, frammento):
        if url in stato["segnalati"]:
            return
        stato["segnalati"][url] = oggi
        risultati.append({"scuola": scuola.get("nome") or urlparse(home).hostname,
                          "provincia": scuola.get("provincia", ""),
                          "titolo": etichetta, "url": url, "pagina": dove, "estratto": frammento})

    async def lista(url):
        """Apre una pagina elenco e mette in coda i link nuovi."""
        if url in gia_listate or time.monotonic() > scadenza:
            return
        gia_listate.add(url)
        r = await sito.scarica(url)
        contatori["richieste"] += 1
        if r is None or "html" not in r.headers.get("content-type", "html"):
            return
        _, link, _ = analizza_html(r)
        for u, etichetta in link:
            if not (stesso_sito(u, home) or RX_ALLEGATO.search(u)):
                continue
            m = RX_CHITARRA.search(etichetta)
            if m:
                segnala(u, etichetta, url, etichetta)
            if u not in stato["visti"] and u not in gia_listate and u != norm(home):
                coda.append((u, etichetta, url))

    try:
        for u in da_listare:
            await lista(u)

        aperte = 0
        visti_in_coda = set()
        while coda and aperte < MAX_NUOVE_PAGINE_PER_SITO and time.monotonic() < scadenza:
            url, etichetta, provenienza = coda.pop(0)
            if url in visti_in_coda or url in stato["visti"]:
                continue
            visti_in_coda.add(url)
            aperte += 1
            r = await sito.scarica(url)
            contatori["richieste"] += 1
            stato["visti"][url] = oggi
            if r is None:
                continue
            tipo = r.headers.get("content-type", "")

            if "pdf" in tipo or url.lower().endswith(".pdf"):
                if len(r.content) <= MAX_PDF_BYTES:
                    t = testo_pdf(r.content)
                    m = RX_CHITARRA.search(t)
                    if m:
                        segnala(url, etichetta, provenienza, estratto(t, m))
                continue
            if "html" not in tipo:
                continue

            testo, link, allegati = analizza_html(r)
            # Una pagina con molti link a interpelli è un elenco: la ricontrolleremo a ogni giro
            if sum(1 for u, _ in link if stesso_sito(u, home)) >= 3 and re.search(r"interpell", testo, re.I):
                if url not in sezioni:
                    sezioni.append(url)
                gia_listate.discard(url)
                del stato["visti"][url]
                await lista(url)
                continue

            m = RX_CHITARRA.search(testo)
            if m and re.search(r"interpell|supplenz", testo, re.I):
                segnala(url, etichetta, provenienza, estratto(testo, m))
                continue
            # La pagina parla di interpelli ma la classe è solo negli allegati
            if re.search(r"interpell|supplenz", etichetta + " " + testo[:3000], re.I):
                for u, et in allegati[:MAX_ALLEGATI_PER_PAGINA]:
                    if u in stato["visti"]:
                        continue
                    ra = await sito.scarica(u)
                    contatori["richieste"] += 1
                    stato["visti"][u] = oggi
                    if ra is None or len(ra.content) > MAX_PDF_BYTES:
                        continue
                    if "pdf" in ra.headers.get("content-type", "") or u.lower().endswith(".pdf"):
                        t = testo_pdf(ra.content)
                        m = RX_CHITARRA.search(t)
                        if m:
                            segnala(url, etichetta, provenienza, estratto(t, m))
                            break
        contatori["ok"] += 1
    except (httpx.HTTPError, asyncio.TimeoutError, ValueError) as e:
        contatori["errori"] += 1
        contatori["ultimi_errori"].append(f"{home}: {type(e).__name__}")
    if not sezioni:
        stato["sezioni"].pop(norm(home), None)


# ---------------------------------------------------------------- notifiche

def messaggio(r) -> str:
    righe = ["🎸 <b>Possibile interpello di chitarra</b>",
             f"<b>{html.escape(r['scuola'])}</b>" + (f" ({html.escape(r['provincia'])})" if r["provincia"] else "")]
    if r["titolo"]:
        righe.append(html.escape(r["titolo"][:200]))
    if r["estratto"] and r["estratto"] != r["titolo"]:
        righe.append(f"<i>…{html.escape(r['estratto'][:300])}…</i>")
    righe.append(r["url"])
    return "\n".join(righe)


async def invia(risultati, prova: bool):
    token, chat = os.environ.get("TELEGRAM_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if prova or not (token and chat):
        for r in risultati:
            print("\n" + re.sub(r"<[^>]+>", "", messaggio(r)))
        if not prova and risultati:
            print("\n(TELEGRAM_TOKEN / TELEGRAM_CHAT_ID non impostati: notifiche solo nel log)")
        return
    async with httpx.AsyncClient(timeout=20) as c:
        for r in risultati:
            await c.post(f"https://api.telegram.org/bot{token}/sendMessage",
                         data={"chat_id": chat, "text": messaggio(r), "parse_mode": "HTML",
                               "disable_web_page_preview": "true"})
            await asyncio.sleep(1)


def annota(risultati):
    if not risultati:
        return
    nuovo = not os.path.exists(TROVATI)
    with open(TROVATI, "a", encoding="utf-8") as f:
        if nuovo:
            f.write("# Interpelli di chitarra trovati\n\n| Data | Scuola | Provincia | Titolo | Link |\n|---|---|---|---|---|\n")
        quando = dt.datetime.now().strftime("%Y-%m-%d %H:%M")
        for r in risultati:
            cella = lambda s: (s or "").replace("|", "/").replace("\n", " ")[:120]
            f.write(f"| {quando} | {cella(r['scuola'])} | {cella(r['provincia'])} | {cella(r['titolo'])} | {r['url']} |\n")


# ---------------------------------------------------------------- main

async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scuole", default="data/scuole.csv")
    p.add_argument("--extra", default="data/siti_extra.txt")
    p.add_argument("--siti", help="file con un URL per riga (sostituisce l'elenco scuole)")
    p.add_argument("--minuti", type=float, default=22, help="tempo massimo del giro")
    p.add_argument("--prova", action="store_true", help="non invia notifiche, le stampa")
    a = p.parse_args()

    scuole = carica_siti(a.scuole, a.extra, a.siti)
    if not scuole:
        sys.exit("Nessun sito da controllare: lancia prima costruisci_elenco_scuole.py")
    stato = carica_stato()
    scadenza = time.monotonic() + a.minuti * 60
    risultati = []
    contatori = {"ok": 0, "errori": 0, "richieste": 0, "ultimi_errori": []}

    # Ordine a rotazione: se il tempo finisce, il giro dopo riparte da dove ci si era fermati
    inizio = stato.get("prossimo_indice", 0) % len(scuole)
    ordine = scuole[inizio:] + scuole[:inizio]

    limiti = httpx.Limits(max_connections=SITI_IN_PARALLELO * 2)
    sem = asyncio.Semaphore(SITI_IN_PARALLELO)
    completati = 0

    async with httpx.AsyncClient(headers={"User-Agent": UA, "Accept-Language": "it"},
                                 timeout=httpx.Timeout(20, connect=10),
                                 follow_redirects=True, limits=limiti) as client:
        async def uno(scuola):
            nonlocal completati
            if time.monotonic() > scadenza:
                return
            async with sem:
                if time.monotonic() > scadenza:
                    return
                try:
                    await asyncio.wait_for(
                        controlla_sito(client, scuola, stato, scadenza, risultati, contatori), timeout=180)
                except asyncio.TimeoutError:
                    contatori["errori"] += 1
                completati += 1

        await asyncio.gather(*(uno(s) for s in ordine))

    stato["prossimo_indice"] = (inizio + completati) % len(scuole)
    stato["ultimo_giro"] = dt.datetime.now().isoformat(timespec="minutes")
    salva_stato(stato)
    annota(risultati)
    await invia(risultati, a.prova)

    print(f"\nSiti controllati: {completati}/{len(scuole)} · ok {contatori['ok']} · "
          f"errori {contatori['errori']} · richieste {contatori['richieste']} · "
          f"trovati {len(risultati)}")
    for e in contatori["ultimi_errori"][:10]:
        print("  errore:", e)


if __name__ == "__main__":
    asyncio.run(main())
