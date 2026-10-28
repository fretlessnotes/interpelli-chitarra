# Crawler interpelli di chitarra (AB55 / AB56)

Controlla ogni 30 minuti i siti delle scuole statali di Piemonte, Lombardia, Liguria, Valle d'Aosta, Emilia-Romagna, Veneto, Toscana e Umbria che possono
avere una cattedra di chitarra (scuole medie per AB56, licei e istituti superiori per AB55).
Quando trova un interpello che nomina AB55, AB56 o "chitarra", ti manda un messaggio su Telegram.

Gira gratis su GitHub Actions: non serve tenere acceso il computer.

## Come funziona

1. **Elenco scuole** – `costruisci_elenco_scuole.py` scarica l'anagrafe ufficiale delle scuole
   statali dagli open data del Ministero e tiene un sito per istituto (circa 4.000–5.000 siti per
   le otto regioni). Si aggiorna da solo ogni lunedì.
2. **Giro** – `crawler.py` apre la home di ogni sito e le pagine "Interpelli", raccoglie i link che
   parlano di interpelli o supplenze e apre solo quelli nuovi, compresi gli allegati PDF.
   Cerca AB55, AB56 (anche scritti "AB 56", "AB-56") e "chitarra".
3. **Memoria** – in `data/stato.json` si ricorda cosa ha già visto e quali pagine sono le sezioni
   "Interpelli" di ogni scuola, così ai giri successivi controlla solo quelle e le novità.
   Ogni avviso viene segnalato una volta sola.
4. **Storico** – tutto quello che trova finisce anche in `trovati.md`.

È educato con i siti: rispetta `robots.txt`, aspetta tra una richiesta e l'altra sullo stesso sito
e apre al massimo 10 pagine nuove per sito a giro.

## Installazione (circa 15 minuti, una volta sola)

### 1. Crea il bot Telegram
1. Su Telegram apri **@BotFather**, scrivi `/newbot` e scegli un nome.
2. Copia il **token** che ti dà (tipo `123456:ABC-...`).
3. Apri il tuo nuovo bot e premi **Avvia** (o scrivigli un messaggio qualsiasi).
4. Apri nel browser `https://api.telegram.org/bot<TOKEN>/getUpdates` (con il tuo token al posto di
   `<TOKEN>`) e copia il numero dopo `"chat":{"id":` : è il tuo **chat id**.

### 2. Metti il codice su GitHub
1. Crea un account su github.com se non ce l'hai.
2. Crea un nuovo repository **pubblico** (i minuti di Actions sono illimitati per i repository
   pubblici; in uno privato il giro ogni 30 minuti supererebbe i minuti gratuiti).
   Nel repository non c'è niente di personale: token e chat id restano nei Secrets, invisibili.
3. Carica tutti i file di questa cartella, compresa la cartella `.github` (con "Add file → Upload
   files" puoi trascinare la cartella intera).

### 3. Inserisci token e chat id
Nel repository: **Settings → Secrets and variables → Actions → New repository secret**, e crea:
- `TELEGRAM_TOKEN` → il token del bot
- `TELEGRAM_CHAT_ID` → il tuo chat id

### 4. Avvia
1. Vai su **Actions**, e se richiesto abilita i workflow.
2. Apri **Aggiorna elenco scuole** → **Run workflow**: crea `data/scuole.csv` (1–2 minuti).
3. Apri **Interpelli chitarra** → **Run workflow** per il primo giro.

Da lì in poi parte da solo ogni 30 minuti. Il primo giorno è più lento perché deve esplorare tutti i
siti; dal secondo giorno controlla solo le pagine interpelli e le novità.

## Personalizzare

- **Altre regioni**: in `.github/workflows/elenco_scuole.yml` cambia l'elenco dopo `--regioni`
  (nomi senza spazi né apostrofi, es. `MARCHE`, `FRIULI-VENEZIA-GIULIA`), poi rilancia "Aggiorna elenco scuole".
- **Scuole in più**: aggiungile in `data/siti_extra.txt`, una per riga. In
  `data/scuole_senza_sito.csv` trovi gli istituti che nell'anagrafe non hanno un sito.
- **Altre parole da cercare**: modifica `RX_CHITARRA` in cima a `crawler.py`.

## Provarlo sul tuo computer (facoltativo)

```bash
pip install -r requirements.txt
python costruisci_elenco_scuole.py
python crawler.py --prova --minuti 5     # stampa i risultati invece di mandarli su Telegram
```

## Limiti da conoscere

- **Albi online esterni** (Argo, Axios, Spaggiari…): alcune scuole pubblicano l'interpello solo
  lì. Di solito lo mettono anche nelle news o nella sezione "Interpelli" del sito, ma non sempre.
- **PDF scansionati**: se l'avviso è un'immagine senza testo, il crawler vede solo il titolo del link.
- **Siti che vietano i crawler** nel `robots.txt` (come il portale interpelli dell'USR Piemonte)
  vengono saltati di proposito.
- **Falsi positivi**: una pagina di interpello con "chitarra" in un menu laterale può generare una
  notifica inutile. Il link nella notifica ti fa verificare in un attimo.
- **Ritardi di GitHub**: i workflow programmati possono partire con qualche minuto di ritardo.
  GitHub inoltre disattiva i workflow dopo 60 giorni senza attività nel repository: i commit del
  crawler contano come attività, ma se ricevi una mail da GitHub basta riattivarlo da Actions.
- **Verifica sempre l'avviso originale**: requisiti, scadenza e modalità di candidatura fanno fede
  solo lì.
