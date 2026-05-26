"""
Stadsdeelcommissies Amsterdam importer via Notubiz API.
Importeert moties en schriftelijke vragen van alle 7 stadsdeelcommissies.

Gebruik:
  python3 sdc_import.py
"""

import time
import requests
import database as db

API_BASE = "https://api.notubiz.nl"

COMMISSIES = {
    547:  "Centrum",
    977:  "Noord",
    1413: "West",
    1424: "Zuid",
    1425: "Oost",
    2122: "Zuidoost",
    2328: "Nieuw-West",
}

MODULES = {
    6: "motie",
    4: "schriftelijke_vraag",
    1: "ingekomen_stuk",
}


def get_attr(attrs, field_id: int) -> str | None:
    """Haal veldwaarde op uit Notubiz attribute-lijst."""
    for attr in attrs:
        if attr.get("@attributes", {}).get("id") == field_id:
            val = attr.get("value") or attr.get("values", {}).get("value")
            if isinstance(val, list):
                return ", ".join(str(v) for v in val)
            return str(val) if val else None
    return None


def haal_items(org_id: int, module_id: int) -> list[dict]:
    url = f"{API_BASE}/organisations/{org_id}/modules/{module_id}/items"
    params = {"format": "json", "rows": 100}
    try:
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        # Response kan een lijst of dict zijn
        if isinstance(data, list):
            return data
        return data.get("item", [])
    except Exception as e:
        print(f"  Fout org {org_id} module {module_id}: {e}")
        return []


def notubiz_naar_item(raw: dict, org_id: int, module_id: int, naam: str) -> dict | None:
    item_id = raw.get("@attributes", {}).get("id")
    if not item_id:
        return None

    attrs = raw.get("attributes", {}).get("attribute", [])
    if isinstance(attrs, dict):
        attrs = [attrs]

    titel = get_attr(attrs, 1)
    if not titel:
        return None

    datum_str = get_attr(attrs, 15) or raw.get("@attributes", {}).get("last_modified", "")
    datum = datum_str[:10] if datum_str else None

    indiener = get_attr(attrs, 36) or get_attr(attrs, 37)
    uitslag = get_attr(attrs, 35) or get_attr(attrs, 62)
    type_naam = MODULES.get(module_id, "motie")
    slug = f"sdc_{naam.lower().replace('-', '')}"

    bron_url = (
        f"https://amsterdam.raadsinformatie.nl/modules/{module_id}/"
        f"stadsdeelcommissie_{naam.lower()}/{item_id}"
    )

    return {
        "extern_id": f"sdc_{org_id}_{item_id}",
        "gemeente_slug": slug,
        "type": type_naam,
        "titel": titel,
        "indiener": indiener,
        "datum_ingediend": datum,
        "termijn_einde": None,
        "datum_afdoening": None,
        "uitslag": uitslag,
        "gekoppeld_evenement": f"Stadsdeelcommissie {naam}",
        "bron_url": bron_url,
    }


def importeer() -> int:
    db.init_db()

    # Registreer stadsdeelcommissies als "gemeenten"
    with db.get_connection() as conn:
        for org_id, naam in COMMISSIES.items():
            slug = f"sdc_{naam.lower().replace('-', '')}"
            conn.execute(
                "INSERT OR IGNORE INTO gemeenten (slug, naam) VALUES (?, ?)",
                (slug, f"SDC {naam}"),
            )

    nieuw = 0
    for org_id, naam in COMMISSIES.items():
        for module_id, type_naam in MODULES.items():
            raws = haal_items(org_id, module_id)
            for raw in raws:
                item = notubiz_naar_item(raw, org_id, module_id, naam)
                if item:
                    is_new, _ = db.upsert_item(item)
                    if is_new:
                        nieuw += 1
            if raws:
                print(f"  SDC {naam} {type_naam}: {len(raws)} items")
            time.sleep(0.3)

    try:
        db.rebuild_fts()
    except Exception:
        pass

    return nieuw


if __name__ == "__main__":
    print("Stadsdeelcommissies import gestart...")
    nieuw = importeer()
    print(f"Klaar: {nieuw} nieuwe items")
