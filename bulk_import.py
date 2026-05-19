"""
Bulk importeer ALLE raadsitems van Amsterdam via de Notubiz API.

Gebruik:
  python3 bulk_import.py            # volledige import (alle modules)
  python3 bulk_import.py --update   # alleen nieuwe items (sneller)

De API geeft items terug op volgorde van ID (oud → nieuw).
Met --update start de import vanaf het hoogste bekende ID in de database.
"""

import sys
import time
import logging
import argparse
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)
import database as db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

API_BASE = "https://api.notubiz.nl"
ORG_ID = 281
BATCH_SIZE = 100
SLEEP_SEC = 0.4

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json",
}

MODULES = {
    "motie": {
        "module_id": 6,
        "detail_naam": "Moties%20en%20amendementen",
        "termijn_weken": 13,
        "datum_label": "Datum indiening",
    },
    "schriftelijke_vraag": {
        "module_id": 4,
        "detail_naam": "Schriftelijke%20vragen",
        "termijn_weken": 4,
        "datum_label": "Datum vraag",
    },
    "ingekomen_stuk": {
        "module_id": 1,
        "detail_naam": "Ingekomen%20stukken",
        "termijn_weken": 6,
        "datum_label": "Datum",
    },
}


def _parse_datum(val: str | None) -> date | None:
    if not val:
        return None
    try:
        return date.fromisoformat(val[:10])
    except (ValueError, TypeError):
        return None


def _attribs(api_item: dict) -> dict:
    """Zet Notubiz attributenlijst om naar {label: value} dict."""
    result = {}
    attrs = api_item.get("attributes", {}).get("attribute", [])
    if isinstance(attrs, dict):
        attrs = [attrs]
    for a in attrs:
        label = a.get("label", "")
        value = a.get("value") or a.get("values", {}).get("value")
        result[label] = value
    return result


def _parse_api_item(api_item: dict, module_type: str, cfg: dict) -> dict | None:
    item_id = api_item.get("@attributes", {}).get("id")
    if not item_id:
        return None

    attrs = _attribs(api_item)

    # Titel ophalen
    titel = attrs.get("Titel") or attrs.get("titel") or attrs.get("Schriftelijke vraag")
    if isinstance(titel, dict):
        titel = titel.get("title") or str(titel)

    # Datum ophalen (meerdere mogelijke labels)
    datum_val = (
        attrs.get(cfg["datum_label"])
        or attrs.get("Datum indiening")
        or attrs.get("Datum vraag")
        or attrs.get("Aanmaakdatum")
    )
    datum_ingediend = _parse_datum(datum_val)

    # Indiener / fractie
    fractie = attrs.get("Fractie") or attrs.get("Indiener(s)")
    if isinstance(fractie, list):
        fractie = ", ".join(str(f) for f in fractie)
    elif isinstance(fractie, dict):
        vals = fractie
        fractie = str(vals)

    # Uitslag
    uitslag = attrs.get("Uitslag")
    if isinstance(uitslag, dict):
        uitslag = str(uitslag)

    # Datum afdoening
    datum_afdoening = _parse_datum(attrs.get("Datum afdoening"))

    # Termijn berekenen
    termijn_einde = None
    if datum_ingediend:
        termijn_einde = datum_ingediend + timedelta(weeks=cfg["termijn_weken"])

    # Type (motie vs amendement)
    type_val = attrs.get("Type") or module_type

    # Gekoppeld evenement
    gekoppeld = attrs.get("Ingediend in") or attrs.get("Gekoppeld evenement")
    if isinstance(gekoppeld, dict):
        gekoppeld = str(gekoppeld)

    bron_url = (
        f"https://amsterdam.raadsinformatie.nl"
        f"/modules/{cfg['module_id']}/{cfg['detail_naam']}/{item_id}"
    )

    return {
        "extern_id": str(item_id),
        "gemeente_slug": "amsterdam",
        "type": module_type,
        "titel": str(titel)[:500] if titel else None,
        "indiener": str(fractie)[:300] if fractie else None,
        "datum_ingediend": datum_ingediend.isoformat() if datum_ingediend else None,
        "termijn_einde": termijn_einde.isoformat() if termijn_einde else None,
        "datum_afdoening": datum_afdoening.isoformat() if datum_afdoening else None,
        "uitslag": str(uitslag)[:100] if uitslag else None,
        "gekoppeld_evenement": str(gekoppeld)[:300] if gekoppeld else None,
        "bron_url": bron_url,
    }


def importeer_module(module_type: str, start_id: int = 0, update_mode: bool = False) -> int:
    cfg = MODULES[module_type]
    module_id = cfg["module_id"]
    url = f"{API_BASE}/organisations/{ORG_ID}/modules/{module_id}/items"

    # Bepaal startpositie voor update-mode
    start_pos = 0
    if update_mode and start_id > 0:
        # Schat positie op basis van hoogste bekende ID
        # We doen een kleine scan vanaf het einde
        start_pos = max(0, _schat_startpositie(url, start_id))
        logger.info(f"Update mode: start bij positie {start_pos} (na ID {start_id})")

    nieuw = bijgewerkt = overgeslagen = 0
    positie = start_pos

    while True:
        try:
            resp = requests.get(
                url,
                params={"limit": BATCH_SIZE, "start": positie},
                headers=HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            logger.error(f"API fout bij {module_type} positie {positie}: {e}")
            time.sleep(5)
            continue

        pag = data.get("pagination", {}).get("@attributes", {})
        totaal = pag.get("total_results", 0)
        list_end = pag.get("list_end", positie)

        items = data.get("item", [])
        if isinstance(items, dict):
            items = [items]

        if not items:
            break

        for api_item in items:
            item = _parse_api_item(api_item, module_type, cfg)
            if not item:
                continue

            # In update-mode: stop als we een al bekend ID tegenkomen
            if update_mode and start_id > 0:
                item_id = int(item["extern_id"])
                if item_id <= start_id:
                    overgeslagen += 1
                    continue

            is_new, _ = db.upsert_item(item)
            if is_new:
                nieuw += 1
            else:
                bijgewerkt += 1

        pct = int(list_end / totaal * 100) if totaal else 0
        logger.info(
            f"{module_type}: {list_end}/{totaal} ({pct}%) — "
            f"{nieuw} nieuw, {bijgewerkt} bijgewerkt"
        )

        if list_end >= totaal:
            break

        positie = list_end
        time.sleep(SLEEP_SEC)

    return nieuw


def _schat_startpositie(url: str, known_id: int) -> int:
    """Schat de positie in de API-lijst op basis van een bekende ID."""
    # Haal totaal op
    resp = requests.get(url, params={"limit": 1}, headers=HEADERS, timeout=15)
    totaal = resp.json().get("pagination", {}).get("@attributes", {}).get("total_results", 0)
    # Begin 200 posities voor het einde om zeker niet te missen
    return max(0, totaal - 200)


def get_max_extern_id(module_type: str) -> int:
    """Hoogste bekende extern_id voor dit module type."""
    with db.get_connection() as conn:
        row = conn.execute(
            "SELECT MAX(CAST(extern_id AS INTEGER)) FROM items WHERE type = ? AND gemeente_slug = 'amsterdam'",
            (module_type,),
        ).fetchone()
    return row[0] or 0


def main():
    parser = argparse.ArgumentParser(description="Raadstracker bulk import")
    parser.add_argument("--update", action="store_true", help="Alleen nieuwe items ophalen")
    parser.add_argument("--module", choices=list(MODULES.keys()), help="Alleen één module importeren")
    args = parser.parse_args()

    db.init_db()

    modules = [args.module] if args.module else list(MODULES.keys())

    totaal_nieuw = 0
    for module_type in modules:
        start_id = get_max_extern_id(module_type) if args.update else 0
        if args.update:
            logger.info(f"=== {module_type} UPDATE (vanaf ID {start_id}) ===")
        else:
            logger.info(f"=== {module_type} VOLLEDIGE IMPORT ===")

        nieuw = importeer_module(module_type, start_id=start_id, update_mode=args.update)
        totaal_nieuw += nieuw
        logger.info(f"=== {module_type} klaar: {nieuw} nieuwe items ===\n")
        time.sleep(1)

    logger.info(f"Totaal {totaal_nieuw} nieuwe items geïmporteerd. FTS index bijwerken...")
    db.rebuild_fts()
    logger.info("✅ Klaar.")


if __name__ == "__main__":
    main()
