"""
Rijksoverheid open data importer.
Haalt persberichten, rapporten en brieven op relevant voor Amsterdam.

Gebruik:
  python3 rijksoverheid_import.py
"""

import time
import requests
import database as db

BASE = "https://opendata.rijksoverheid.nl/v1/documents"

# Zoekopdrachten die relevant zijn voor Amsterdam raadsleden
QUERIES = [
    ("amsterdam", "amsterdam"),
    ("woningbouw huurwoningen", "wonen"),
    ("gemeenten lokaal bestuur", "bestuur"),
    ("openbaar vervoer NS", "ov"),
    ("klimaat duurzaamheid steden", "klimaat"),
    ("onderwijs kinderopvang", "onderwijs"),
    ("politie veiligheid", "veiligheid"),
    ("zorg welzijn gemeenten", "zorg"),
    ("stikstof natuur", "natuur"),
    ("asiel migratie opvang", "migratie"),
]

# Relevante document types
DOC_TYPES = ["persbericht", "brief", "rapport", "beleidsnota", "kamerstuk"]


def haal_pagina(query: str, start: int = 0) -> list[dict]:
    params = {
        "rows": 20,
        "start": start,
        "output": "json",
        "query": query,
    }
    try:
        r = requests.get(BASE, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        print(f"  Fout bij '{query}': {e}")
        return []


def rgo_naar_item(doc: dict, query_tag: str) -> dict | None:
    titel = doc.get("title", "").strip()
    if not titel:
        return None

    # Datum
    datum = None
    for veld in ["available", "lastmodified", "frontenddate"]:
        val = doc.get(veld, "")
        if val and not val.startswith("000"):
            datum = val[:10]
            break

    # Sla oude en irrelevante documenten over
    if datum and datum < "2022-01-01":
        return None

    extern_id = f"rgo_{doc['id']}"
    return {
        "extern_id": extern_id,
        "bron": "Rijksoverheid",
        "titel": titel,
        "url": doc.get("canonical", ""),
        "samenvatting": doc.get("introduction", "")[:400].replace("<p>", "").replace("</p>", " ").strip(),
        "datum": datum,
        "query_tag": query_tag,
    }


def importeer() -> int:
    db.init_db()
    db.init_media_db()
    nieuw = 0
    gezien = set()

    for query, tag in QUERIES:
        docs = haal_pagina(query)
        for doc in docs:
            doc_id = doc.get("id")
            if not doc_id or doc_id in gezien:
                continue
            gezien.add(doc_id)
            item = rgo_naar_item(doc, tag)
            if item and db.upsert_media_item(item):
                nieuw += 1
        print(f"  [{tag}] {len(docs)} docs, {nieuw} totaal nieuw")
        time.sleep(0.3)

    return nieuw


if __name__ == "__main__":
    print("Rijksoverheid import gestart...")
    nieuw = importeer()
    print(f"Klaar: {nieuw} nieuwe items")
