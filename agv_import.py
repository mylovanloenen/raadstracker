"""
Waterschap Amstel, Gooi en Vecht (AGV) importer via Notubiz API.
Importeert moties en schriftelijke vragen van het waterschap.

Gebruik:
  python3 agv_import.py
"""

import time
import logging
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

API_BASE = "https://api.notubiz.nl"
ORG_ID = 1707
ORG_NAAM = "Waterschap Amstel, Gooi en Vecht"
GEMEENTE_SLUG = "waterschap_agv"

MODULES = {
    6: "motie",
    4: "schriftelijke_vraag",
}

# Notubiz field IDs voor AGV
FIELD_TITEL = 1
FIELD_DATUM_MOTIE = 15      # "Datum motie" / "Datum vraag"
FIELD_PARTIJEN = 37         # "Betrokken partijen" (indiener)
FIELD_UITSLAG = 62          # "Uitslag"
FIELD_DOCUMENT = 2          # "Hoofddocument" / "De vraag"


def get_attr(attrs: list, field_id: int):
    """Haal veldwaarde op uit Notubiz attribute-lijst."""
    for attr in attrs:
        if attr.get("@attributes", {}).get("id") == field_id:
            val = attr.get("value") or attr.get("values", {}).get("value")
            if isinstance(val, list):
                return ", ".join(str(v) for v in val)
            if isinstance(val, dict):
                return val.get("title") or val.get("url")
            return str(val) if val else None
    return None


def get_doc_url(attrs: list, field_id: int) -> str | None:
    """Haal document URL op uit attribute."""
    for attr in attrs:
        if attr.get("@attributes", {}).get("id") == field_id:
            val = attr.get("value")
            if isinstance(val, dict):
                return val.get("url")
    return None


def haal_items(module_id: int) -> list[dict]:
    url = f"{API_BASE}/organisations/{ORG_ID}/modules/{module_id}/items"
    params = {"format": "json", "rows": 500}
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        if isinstance(data, list):
            return data
        return data.get("item", [])
    except Exception as e:
        logger.error(f"Fout bij ophalen module {module_id}: {e}")
        return []


def agv_naar_item(raw: dict, module_id: int) -> dict | None:
    item_id = raw.get("@attributes", {}).get("id")
    if not item_id:
        return None

    attrs = raw.get("attributes", {}).get("attribute", [])
    if isinstance(attrs, dict):
        attrs = [attrs]

    titel = get_attr(attrs, FIELD_TITEL)
    if not titel:
        return None

    datum_str = get_attr(attrs, FIELD_DATUM_MOTIE) or raw.get("@attributes", {}).get("last_modified", "")
    datum = datum_str[:10] if datum_str else None

    indiener = get_attr(attrs, FIELD_PARTIJEN)
    uitslag = get_attr(attrs, FIELD_UITSLAG)
    type_naam = MODULES.get(module_id, "motie")

    # Document URL
    bron_url = get_doc_url(attrs, FIELD_DOCUMENT)
    if not bron_url:
        bron_url = f"https://api.notubiz.nl/organisations/{ORG_ID}/modules/{module_id}/items/{item_id}?format=json"

    return {
        "extern_id": f"agv_{ORG_ID}_{item_id}",
        "gemeente_slug": GEMEENTE_SLUG,
        "type": type_naam,
        "titel": titel,
        "indiener": indiener,
        "datum_ingediend": datum,
        "termijn_einde": None,
        "datum_afdoening": None,
        "uitslag": uitslag,
        "gekoppeld_evenement": ORG_NAAM,
        "bron_url": bron_url,
    }


def importeer() -> int:
    db.init_db()
    totaal = 0

    for module_id, type_naam in MODULES.items():
        logger.info(f"Haal {type_naam}s op van {ORG_NAAM}...")
        items = haal_items(module_id)
        nieuw = 0

        for raw in items:
            item = agv_naar_item(raw, module_id)
            if item:
                db.upsert_item(item)
                nieuw += 1

        logger.info(f"  {type_naam}: {nieuw} items verwerkt")
        totaal += nieuw
        time.sleep(0.3)

    db.rebuild_fts()
    logger.info(f"✅ AGV import klaar: {totaal} items")
    return totaal


if __name__ == "__main__":
    importeer()
