 #!/usr/bin/env python3
"""
InterpelliScuola Nazionale - scraper
=====================================
Legge sources_config.json, scarica ogni pagina, estrae i bandi (interpelli)
con euristiche per struttura HTML, e produce data/interpelli.json.
Se sono presenti le variabili d'ambiente SUPABASE_URL e SUPABASE_KEY,
sincronizza (upsert) i risultati anche su una tabella Supabase.

Novita' di questa versione:
- La chiave del record include la provincia: due province che usano lo stesso
  sito (es. Chieti e Pescara) non si sovrascrivono piu'.
- Una fonte con lo stesso indirizzo di una fonte di un'ALTRA regione viene
  saltata (evita dati di una regione etichettati con un'altra).
- Ogni pagina viene scaricata una sola volta anche se usata da piu' province.
- Dopo una sincronizzazione riuscita vengono tolti i record che non sono piu'
  presenti sulle fonti (senza toccare le province la cui fonte e' irraggiungibile).
- Il nome della scuola non viene piu' preso dentro una parola (es. "pubblicati").
"""

import json
import os
import re
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "sources_config.json"
OUTPUT_PATH = HERE / "data" / "interpelli.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "it-IT,it;q=0.9,en;q=0.8",
}

MESI_ITALIANI = {
    "gennaio": 1, "febbraio": 2, "marzo": 3, "aprile": 4, "maggio": 5, "giugno": 6,
    "luglio": 7, "agosto": 8, "settembre": 9, "ottobre": 10, "novembre": 11, "dicembre": 12,
}

# Nome provincia come scritto da Docenti.it -> (sigla, regione), per la fonte aggregatrice nazionale
PROVINCIA_MAP = {
    "chieti": ("CH", "Abruzzo"), "l'aquila": ("AQ", "Abruzzo"), "pescara": ("PE", "Abruzzo"), "teramo": ("TE", "Abruzzo"),
    "matera": ("MT", "Basilicata"), "potenza": ("PZ", "Basilicata"),
    "catanzaro": ("CZ", "Calabria"), "cosenza": ("CS", "Calabria"), "crotone": ("KR", "Calabria"),
    "reggio calabria": ("RC", "Calabria"), "vibo valentia": ("VV", "Calabria"),
    "avellino": ("AV", "Campania"), "benevento": ("BN", "Campania"), "caserta": ("CE", "Campania"),
    "napoli": ("NA", "Campania"), "salerno": ("SA", "Campania"),
    "bologna": ("BO", "Emilia-Romagna"), "ferrara": ("FE", "Emilia-Romagna"),
    "forli-cesena": ("FC", "Emilia-Romagna"), "forlì-cesena": ("FC", "Emilia-Romagna"),
    "modena": ("MO", "Emilia-Romagna"), "parma": ("PR", "Emilia-Romagna"), "piacenza": ("PC", "Emilia-Romagna"),
    "ravenna": ("RA", "Emilia-Romagna"), "reggio emilia": ("RE", "Emilia-Romagna"), "rimini": ("RN", "Emilia-Romagna"),
    "gorizia": ("GO", "Friuli-Venezia Giulia"), "pordenone": ("PN", "Friuli-Venezia Giulia"),
    "trieste": ("TS", "Friuli-Venezia Giulia"), "udine": ("UD", "Friuli-Venezia Giulia"),
    "frosinone": ("FR", "Lazio"), "latina": ("LT", "Lazio"), "rieti": ("RI", "Lazio"),
    "roma": ("RM", "Lazio"), "viterbo": ("VT", "Lazio"),
    "genova": ("GE", "Liguria"), "imperia": ("IM", "Liguria"), "la spezia": ("SP", "Liguria"), "savona": ("SV", "Liguria"),
    "bergamo": ("BG", "Lombardia"), "brescia": ("BS", "Lombardia"), "como": ("CO", "Lombardia"),
    "cremona": ("CR", "Lombardia"), "lecco": ("LC", "Lombardia"), "lodi": ("LO", "Lombardia"),
    "mantova": ("MN", "Lombardia"), "milano": ("MI", "Lombardia"),
    "monza e della brianza": ("MB", "Lombardia"), "monza e brianza": ("MB", "Lombardia"),
    "pavia": ("PV", "Lombardia"), "sondrio": ("SO", "Lombardia"), "varese": ("VA", "Lombardia"),
    "ancona": ("AN", "Marche"), "ascoli piceno": ("AP", "Marche"), "fermo": ("FM", "Marche"),
    "macerata": ("MC", "Marche"), "pesaro-urbino": ("PU", "Marche"), "pesaro e urbino": ("PU", "Marche"),
    "campobasso": ("CB", "Molise"), "isernia": ("IS", "Molise"),
    "alessandria": ("AL", "Piemonte"), "asti": ("AT", "Piemonte"), "biella": ("BI", "Piemonte"),
    "cuneo": ("CN", "Piemonte"), "novara": ("NO", "Piemonte"), "torino": ("TO", "Piemonte"),
    "verbano-cusio-ossola": ("VB", "Piemonte"), "vercelli": ("VC", "Piemonte"),
    "bari": ("BA", "Puglia"), "barletta-andria-trani": ("BT", "Puglia"), "brindisi": ("BR", "Puglia"),
    "foggia": ("FG", "Puglia"), "lecce": ("LE", "Puglia"), "taranto": ("TA", "Puglia"),
    "cagliari": ("CA", "Sardegna"), "medio campidano": ("SU", "Sardegna"), "nuoro": ("NU", "Sardegna"),
    "ogliastra": ("NU", "Sardegna"), "oristano": ("OR", "Sardegna"), "sassari": ("SS", "Sardegna"),
    "carbonia-iglesias": ("SU", "Sardegna"), "sud sardegna": ("SU", "Sardegna"), "olbia-tempio": ("SS", "Sardegna"),
    "agrigento": ("AG", "Sicilia"), "caltanissetta": ("CL", "Sicilia"), "catania": ("CT", "Sicilia"),
    "enna": ("EN", "Sicilia"), "messina": ("ME", "Sicilia"), "palermo": ("PA", "Sicilia"),
    "ragusa": ("RG", "Sicilia"), "siracusa": ("SR", "Sicilia"), "trapani": ("TP", "Sicilia"),
    "arezzo": ("AR", "Toscana"), "firenze": ("FI", "Toscana"), "grosseto": ("GR", "Toscana"),
    "livorno": ("LI", "Toscana"), "lucca": ("LU", "Toscana"), "massa-carrara": ("MS", "Toscana"),
    "pisa": ("PI", "Toscana"), "pistoia": ("PT", "Toscana"), "prato": ("PO", "Toscana"), "siena": ("SI", "Toscana"),
    "bolzano": ("BZ", "Trentino-Alto Adige"), "bolzano/bozen": ("BZ", "Trentino-Alto Adige"),
    "trento": ("TN", "Trentino-Alto Adige"),
    "perugia": ("PG", "Umbria"), "terni": ("TR", "Umbria"),
    "aosta": ("AO", "Valle d'Aosta"),
    "belluno": ("BL", "Veneto"), "padova": ("PD", "Veneto"), "rovigo": ("RO", "Veneto"),
    "treviso": ("TV", "Veneto"), "venezia": ("VE", "Veneto"), "verona": ("VR", "Veneto"), "vicenza": ("VI", "Veneto"),
}

CDC_PATTERN = re.compile(r"\b(A[A-Z0-9]{3}|B0[0-9]{2}|AD[A-Z]{2}|AA[A-Z0-9]{2}|EEEE|AAAA)\b")
CODICE_MECC_PATTERN = re.compile(r"\b[A-Z]{2,4}[0-9]{5,6}[A-Z]\b")
DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b"),
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
]
SCUOLA_PATTERN = re.compile(
    r"((?<![A-Za-z])(?:Istituto|IC|I\.C\.|IIS|I\.I\.S\.|Liceo|ITC|ITIS|Direzione Didattica)"
    r"(?![a-z])[^\n,.;]{3,70})",
    re.I,
)

FAILED = []   # (url, motivo) dei siti non scaricati
SKIPPED = []  # (sigla, motivo) delle fonti saltate


def make_session():
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(
        total=3,
        connect=1,
        backoff_factor=2,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("https://", adapter)
    s.mount("http://", adapter)
    return s


SESSION = make_session()


def normalize_date(raw):
    raw = raw.strip()
    m = re.match(r"^(\d{4})-(\d{2})-(\d{2})$", raw)
    if m:
        return raw
    m = re.match(r"^(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})$", raw)
    if m:
        d, mo, y = m.groups()
        if len(y) == 2:
            y = "20" + y
        try:
            return f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        except ValueError:
            return None
    return None


def record_key(rec):
    raw = "|".join([
        (rec.get("provincia") or "").strip().upper(),
        (rec.get("scuola") or "").strip().lower(),
        (rec.get("classeConcorso") or "").strip().lower(),
        (rec.get("comune") or "").strip().lower(),
        (rec.get("dataPubblicazione") or ""),
        (rec.get("urlDiretto") or "").strip(),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def fetch(url):
    try:
        r = SESSION.get(url, timeout=(10, 30))
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status:
            motivo = f"HTTP {status}"
            if status == 403:
                motivo += " (il sito blocca lo scraper)"
        elif "Failed to resolve" in str(e) or "NameResolutionError" in str(e):
            motivo = "dominio non trovato: controlla l'URL in sources_config.json"
        else:
            motivo = type(e).__name__
        FAILED.append((url, motivo))
        print(f"  [ERRORE] impossibile scaricare {url}: {motivo}", file=sys.stderr)
        return None


def extract_from_block(text, url, sigla, regione):
    rec = {
        "scuola": "", "codiceMeccanografico": "", "regione": regione, "provincia": sigla,
        "comune": "", "gradoScuola": "", "classeConcorso": "", "tipologiaPosto": "",
        "oreSettimanali": "", "dataPubblicazione": "", "dataScadenza": "", "urlDiretto": url,
    }
    m = CODICE_MECC_PATTERN.search(text)
    if m:
        rec["codiceMeccanografico"] = m.group(0)
    m = CDC_PATTERN.search(text)
    if m:
        rec["classeConcorso"] = m.group(0)
    dates = []
    for pat in DATE_PATTERNS:
        for mm in pat.finditer(text):
            nd = normalize_date(mm.group(0))
            if nd:
                dates.append(nd)
    if dates:
        rec["dataPubblicazione"] = dates[0]
        if len(dates) > 1:
            rec["dataScadenza"] = dates[1]
    if re.search(r"sostegno", text, re.I):
        rec["tipologiaPosto"] = "Sostegno"
    elif re.search(r"posto comune", text, re.I):
        rec["tipologiaPosto"] = "Posto Comune"
    m = SCUOLA_PATTERN.search(text)
    rec["scuola"] = m.group(1).strip() if m else text.strip()[:80]
    return rec


def parser_generic_wp(html, url, sigla, regione):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    candidates = soup.select("article, .post, li a, h2 a, h3 a, .entry-title a")
    seen_links = set()
    for el in candidates:
        title = el.get_text(" ", strip=True)
        if not title or "interpell" not in title.lower():
            continue
        if el.name == "a":
            link = el.get("href")
        else:
            a = el.find("a", href=True)
            link = a["href"] if a else url
        if not link:
            continue
        link = urljoin(url, link)
        if link in seen_links:
            continue
        seen_links.add(link)
        rec = extract_from_block(title, link, sigla, regione)
        rec["scuola"] = rec["scuola"] or title[:80]
        out.append(rec)
    return out


def parser_mim_web(html, url, sigla, regione):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.select("a"):
        title = a.get_text(" ", strip=True)
        if not title or "interpell" not in title.lower():
            continue
        link = urljoin(url, a.get("href") or url)
        rec = extract_from_block(title, link, sigla, regione)
        out.append(rec)
    return out


def parser_umbria_table(html, url, sigla, regione):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for row in soup.select("table tr"):
        cells = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
        if not cells or len(cells) < 2:
            continue
        joined = " | ".join(cells)
        if not CODICE_MECC_PATTERN.search(joined):
            continue
        rec = extract_from_block(joined, url, sigla, regione)
        rec["scuola"] = cells[0]
        out.append(rec)
    return out


def parser_piemonte_php(html, url, sigla, regione):
    return parser_umbria_table(html, url, sigla, regione)


def parse_italian_date(raw):
    m = re.search(r"(\d{1,2})\s+(\w+)\s+(\d{4})", raw or "")
    if not m:
        return ""
    giorno, mese_nome, anno = m.groups()
    mese = MESI_ITALIANI.get(mese_nome.lower())
    if not mese:
        return ""
    return f"{int(anno):04d}-{mese:02d}-{int(giorno):02d}"
