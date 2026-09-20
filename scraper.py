#!/usr/bin/env python3
"""
InterpelliScuola Nazionale - scraper
=====================================
Legge sources_config.json, scarica ogni pagina, estrae i bandi (interpelli)
con euristiche per struttura HTML, e produce data/interpelli.json.
Se sono presenti le variabili d'ambiente SUPABASE_URL e SUPABASE_KEY,
sincronizza (upsert) i risultati anche su una tabella Supabase.
"""

import json
import os
import re
import sys
import time
import hashlib
from datetime import datetime, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

HERE = Path(__file__).parent
CONFIG_PATH = HERE / "sources_config.json"
OUTPUT_PATH = HERE / "data" / "interpelli.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; InterpelliScuolaBot/1.0; +https://github.com/) "
                  "- bot di raccolta interpelli pubblici per uso personale/didattico"
}

CDC_PATTERN = re.compile(r"\b(A[A-Z0-9]{3}|B0[0-9]{2}|AD[A-Z]{2}|AA[A-Z0-9]{2}|EEEE|AAAA)\b")
CODICE_MECC_PATTERN = re.compile(r"\b[A-Z]{2,4}[0-9]{5,6}[A-Z]\b")
DATE_PATTERNS = [
    re.compile(r"\b(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{2,4})\b"),
    re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"),
]


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
        (rec.get("scuola") or "").strip().lower(),
        (rec.get("classeConcorso") or "").strip().lower(),
        (rec.get("comune") or "").strip().lower(),
        (rec.get("dataPubblicazione") or ""),
    ])
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


def fetch(url):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        print(f"  [ERRORE] impossibile scaricare {url}: {e}", file=sys.stderr)
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
    m = re.search(r"((?:Istituto|IC|I\.C\.|IIS|I\.I\.S\.|Liceo|ITC|ITIS|Direzione Didattica)[^\n,.;]{3,70})", text, re.I)
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
        link = el.get("href") if el.name == "a" else (el.find("a")["href"] if el.find("a") else url)
        if not link or link in seen_links:
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
        link = a.get("href", url)
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


PARSERS = {
    "generic_wp": parser_generic_wp,
    "mim_web": parser_mim_web,
    "umbria_table": parser_umbria_table,
    "piemonte_php": parser_piemonte_php,
}


def scrape_all(config):
    all_records = []
    for src in config["sources"]:
        sigla, regione, url, parser_name = src["sigla"], src["regione"], src["url"], src["parser"]
        print(f"-> {regione} / {sigla}: {url}")
        html = fetch(url)
        if not html:
            continue
        parser = PARSERS.get(parser_name, parser_generic_wp)
        try:
            records = parser(html, url, sigla, regione)
        except Exception as e:
            print(f"  [ERRORE parser {parser_name}] {e}", file=sys.stderr)
            records = []
        print(f"   trovati {len(records)} possibili record")
        all_records.extend(records)
        time.sleep(1.5)
    return all_records


def dedupe(records):
    seen = {}
    for r in records:
        seen[record_key(r)] = r
    return list(seen.values())


def push_to_supabase(records):
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_KEY non impostate: salto la sincronizzazione remota.")
        return
    endpoint = f"{url}/rest/v1/interpelli"
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Prefer": "resolution=merge-duplicates",
    }
    rows = [{
        "record_key": record_key(r),
        "scuola": r.get("scuola"),
        "codice_meccanografico": r.get("codiceMeccanografico"),
        "regione": r.get("regione"),
        "provincia": r.get("provincia"),
        "comune": r.get("comune"),
        "grado_scuola": r.get("gradoScuola"),
        "classe_concorso": r.get("classeConcorso"),
        "tipologia_posto": r.get("tipologiaPosto"),
        "ore_settimanali": r.get("oreSettimanali"),
        "data_pubblicazione": r.get("dataPubblicazione") or None,
        "data_scadenza": r.get("dataScadenza") or None,
        "url_diretto": r.get("urlDiretto"),
        "last_seen_at": datetime.now(timezone.utc).isoformat(),
    } for r in records]
    try:
        resp = requests.post(endpoint, headers=headers, json=rows, timeout=30)
        resp.raise_for_status()
        print(f"Sincronizzati {len(rows)} record su Supabase.")
    except requests.RequestException as e:
        print(f"[ERRORE] sync Supabase fallita: {e}", file=sys.stderr)


def main():
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    records = scrape_all(config)
    records = dedupe(records)
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps({
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "count": len(records),
        "records": records,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nScritti {len(records)} record in {OUTPUT_PATH}")
    push_to_supabase(records)


if __name__ == "__main__":
    main()
