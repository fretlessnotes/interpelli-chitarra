"""
Costruisce data/scuole.csv: un sito web per ogni istituto statale delle regioni scelte
che ha una scuola media (AB56) o un liceo / istituto superiore (AB55).

Fonte: open data del Ministero (anagrafe scuole statali, file SCUANAGRAFESTAT...csv e,
per Valle d'Aosta/Trento/Bolzano, SCUANAAUTSTAT...csv).

L'anagrafe non dice quali medie sono a indirizzo musicale: per questo si prendono tutte le
medie e tutti i licei/superiori; gli avvisi non di chitarra vengono scartati dal crawler.

Uso:
    python costruisci_elenco_scuole.py                       # regioni predefinite, file più recenti
    python costruisci_elenco_scuole.py --csv a.csv b.csv     # usa file già scaricati
    python costruisci_elenco_scuole.py --regioni PIEMONTE VALLE-D-AOSTA
"""
import argparse
import csv
import datetime as dt
import io
import re
import sys
from urllib.parse import urljoin

import httpx

CATALOGO = "https://dati.istruzione.it/opendata/opendata/catalogo/elements1/?area=Scuole"
BASE = "https://dati.istruzione.it/opendata/opendata/catalogo/elements1/"
UA = "CrawlerInterpelliChitarra/1.0 (uso personale)"

# Tipologie che possono avere cattedre di chitarra: medie (AB56), licei musicali e superiori (AB55)
TIPOLOGIE_UTILI = re.compile(
    # i licei musicali spesso stanno dentro istituti magistrali, d'arte o IIS
    r"PRIMO GRADO|COMPRENSIVO|OMNICOMPRENSIVO|LICEO|MAGISTRALE|ARTE|ARTISTIC|MUSICAL|"
    r"ISTITUTO SUPERIORE|ISTITUTO D.ISTRUZIONE SUPERIORE",
    re.I,
)


# L'anagrafe è divisa in due file: tutta Italia tranne le province autonome (SCUANAGRAFESTAT)
# e Valle d'Aosta, Trento, Bolzano (SCUANAAUTSTAT)
FILE_ANAGRAFE = ["SCUANAGRAFESTAT", "SCUANAAUTSTAT"]

REGIONI_PREDEFINITE = ["PIEMONTE", "LOMBARDIA", "LIGURIA", "VALLE-D-AOSTA",
                       "EMILIA-ROMAGNA", "VENETO", "TOSCANA", "UMBRIA"]


def chiave_regione(s: str) -> str:
    """'Valle d'Aosta', "VALLE D' AOSTA", 'valle-d-aosta' -> 'VALLEDAOSTA'."""
    return re.sub(r"[^A-Z]", "", (s or "").upper())


def trova_url_csv(client: httpx.Client, prefisso: str) -> str | None:
    """Cerca nel catalogo il link al CSV più recente con quel prefisso; se non lo trova prova i nomi noti."""
    try:
        r = client.get(CATALOGO)
        nomi = re.findall(rf'["\']([^"\']*{prefisso}\d+\.csv)["\']', r.text)
        if nomi:
            nomi.sort(key=lambda n: re.search(r"(\d+)\.csv", n).group(1), reverse=True)
            return urljoin(BASE, nomi[0])
    except httpx.HTTPError as e:
        print(f"Catalogo non raggiungibile ({e}), provo i nomi standard", file=sys.stderr)

    anno = dt.date.today().year
    for a in range(anno, anno - 4, -1):
        url = BASE + f"{prefisso}{a}{str(a + 1)[2:]}{a}0901.csv"
        try:
            if client.head(url).status_code == 200:
                return url
        except httpx.HTTPError:
            pass
    return None


def normalizza_sito(s: str) -> str | None:
    """Ripulisce gli indirizzi dell'anagrafe, che spesso sono scritti male:
    'https://https//www.x.it', 'HTPPS//WWW.X.IT', 'www.https//x.it', 'x.edu,it', 'x@gov.it', 'x.org.'.
    Restituisce None se l'indirizzo non è recuperabile."""
    s = (s or "").strip()
    if not s or "non disponibile" in s.lower():
        return None
    s = s.split()[0].replace(",", ".").replace("@", ".").replace("\\", "/")
    # tieni http solo se l'indirizzo era scritto correttamente così; altrimenti https
    schema = "http" if re.match(r"http://(?!https?/)", s, re.I) else "https"
    prima = None
    while prima != s:  # ripete finché c'è qualcosa da togliere (es. https://www.http//...)
        prima = s
        s = re.sub(r"^www\.(?=h[a-z]{1,5}(?::|/))", "", s, flags=re.I)        # www.https//...
        s = re.sub(r"^(?:h[a-z]{1,5}(?::/{0,3}|/{1,3}))+", "", s, flags=re.I)  # https://, https//, htpps//, http/
        s = s.lstrip("/:")
    host, _, resto = s.partition("/")
    host = re.sub(r"\.{2,}", ".", host.lower()).strip(".")
    if not re.fullmatch(r"[a-z0-9-]+(\.[a-z0-9-]+)*\.[a-z]{2,}", host):
        return None
    resto = resto.strip()
    return f"{schema}://{host}/{resto}"


def leggi_righe(testo: str):
    dialetto = csv.Sniffer().sniff(testo[:5000], delimiters=",;")
    return csv.DictReader(io.StringIO(testo), dialect=dialetto)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--csv", nargs="+", help="uno o più file anagrafe già scaricati")
    p.add_argument("--regioni", nargs="+", default=REGIONI_PREDEFINITE,
                   help="es. PIEMONTE VALLE-D-AOSTA EMILIA-ROMAGNA (maiuscole/trattini indifferenti)")
    p.add_argument("--out", default="data/scuole.csv")
    a = p.parse_args()

    testi = []
    if a.csv:
        for percorso in a.csv:
            with open(percorso, encoding="utf-8-sig", errors="replace") as f:
                testi.append(f.read())
    else:
        with httpx.Client(headers={"User-Agent": UA}, timeout=90, follow_redirects=True) as c:
            for prefisso in FILE_ANAGRAFE:
                url = trova_url_csv(c, prefisso)
                if url is None:
                    print(f"Attenzione: non trovo il file {prefisso}", file=sys.stderr)
                    continue
                print(f"Scarico {url}")
                r = c.get(url)
                r.raise_for_status()
                testi.append(r.content.decode("utf-8-sig", errors="replace"))
        if not testi:
            raise SystemExit(
                "Non trovo i file dell'anagrafe scuole. Scaricali a mano da "
                f"{CATALOGO} (Anagrafe scuole statali, CSV) e rilancia con --csv file1.csv file2.csv"
            )

    regioni = {chiave_regione(x) for x in a.regioni}
    trovate = set()
    istituti: dict[str, dict] = {}
    righe_anagrafe = (r for t in testi for r in leggi_righe(t))
    for riga in righe_anagrafe:
        riga = {k.strip().upper(): (v or "").strip() for k, v in riga.items() if k}
        if chiave_regione(riga.get("REGIONE", "")) not in regioni:
            continue
        trovate.add(chiave_regione(riga.get("REGIONE", "")))
        if not TIPOLOGIE_UTILI.search(riga.get("DESCRIZIONETIPOLOGIAGRADOISTRUZIONESCUOLA", "")):
            continue
        cod = riga.get("CODICEISTITUTORIFERIMENTO") or riga.get("CODICESCUOLA")
        sito = normalizza_sito(riga.get("SITOWEBSCUOLA", ""))
        ist = istituti.setdefault(cod, {
            "codice": cod,
            "nome": riga.get("DENOMINAZIONEISTITUTORIFERIMENTO") or riga.get("DENOMINAZIONESCUOLA", ""),
            "provincia": riga.get("PROVINCIA", ""),
            "sito": None,
        })
        # preferisci il sito della sede principale, altrimenti il primo disponibile
        if sito and (ist["sito"] is None or riga.get("CODICESCUOLA") == cod):
            ist["sito"] = sito

    # un sito può servire più codici: tieni una riga per sito
    visti, righe = set(), []
    for ist in sorted(istituti.values(), key=lambda x: (x["provincia"], x["nome"])):
        if ist["sito"] and ist["sito"] not in visti:
            visti.add(ist["sito"])
            righe.append(ist)

    import os
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(a.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["codice", "nome", "provincia", "sito"])
        w.writeheader()
        w.writerows(righe)
    senza = [i for i in istituti.values() if not i["sito"]]
    percorso_senza = os.path.join(os.path.dirname(a.out) or ".", "scuole_senza_sito.csv")
    with open(percorso_senza, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["codice", "nome", "provincia", "sito"])
        w.writeheader()
        w.writerows(sorted(senza, key=lambda x: (x["provincia"], x["nome"])))
    print(f"{len(righe)} siti scritti in {a.out} · {len(senza)} istituti senza sito "
          f"nell'anagrafe, elencati in {percorso_senza}")
    mancanti = regioni - trovate
    if mancanti:
        print(f"Attenzione: nessuna scuola trovata per {', '.join(sorted(mancanti))}", file=sys.stderr)


if __name__ == "__main__":
    main()
