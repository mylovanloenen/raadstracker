"""
Tweede Kamer open data importer.
Haalt moties, schriftelijke vragen en interpellaties op via de TK OData API.

Gebruik:
  python tk_import.py                    # afgelopen 2 jaar
  python tk_import.py --jaar 2023-2024   # specifiek vergaderjaar
  python tk_import.py --alles            # alle jaren (traag!)
"""

import argparse
import time
import requests
import database as db
from datetime import date, timedelta

BASE = "https://gegevensmagazijn.tweedekamer.nl/OData/v4/2.0"

SOORTEN = ["Motie", "Schriftelijke vragen", "Interpellatie"]

TYPE_MAP = {
    "Motie": "motie",
    "Schriftelijke vragen": "schriftelijke_vraag",
    "Interpellatie": "interpellatie",
}

GEMEENTE_SLUG = "tweedekamer"


def tk_url(nummer: str, soort: str) -> str:
    slug = {
        "Motie": "moties",
        "Schriftelijke vragen": "vragen",
        "Interpellatie": "vragen",
    }.get(soort, "kamerstukken")
    return f"https://www.tweedekamer.nl/kamerstukken/{slug}/detail?id={nummer}"


def haal_pagina(url: str, params: dict, poging: int = 0) -> dict:
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        if poging < 3:
            time.sleep(2 ** poging)
            return haal_pagina(url, params, poging + 1)
        raise


def haal_zaken(soort: str, vanaf: str | None, vergaderjaar: str | None) -> list[dict]:
    """Haal alle Zaak records op voor één soort, gepagineerd."""
    url = f"{BASE}/Zaak"
    filters = [f"Soort eq '{soort}'", "Verwijderd eq false"]
    if vanaf:
        filters.append(f"GestartOp ge {vanaf}T00:00:00Z")
    if vergaderjaar:
        filters.append(f"Vergaderjaar eq '{vergaderjaar}'")

    params = {
        "$filter": " and ".join(filters),
        "$select": "Id,Nummer,Soort,Onderwerp,GestartOp,Afgedaan,Termijn,Vergaderjaar",
        "$expand": "ZaakActor($select=ActorNaam,Functie)",
        "$top": 100,
        "$orderby": "GestartOp desc",
    }

    resultaten = []
    skip = 0
    while True:
        params["$skip"] = skip
        data = haal_pagina(url, params)
        batch = data.get("value", [])
        if not batch:
            break
        resultaten.extend(batch)
        print(f"  {soort}: {len(resultaten)} opgehaald...", end="\r")
        if len(batch) < 100:
            break
        skip += 100
        time.sleep(0.3)

    print()
    return resultaten


def zaak_naar_item(zaak: dict) -> dict:
    soort = zaak["Soort"]

    # Indiener: eerste actor met Functie 'Tweede Kamerlid', anders eerste actor
    actors = zaak.get("ZaakActor", [])
    indiener = next(
        (a["ActorNaam"] for a in actors if a.get("Functie") == "Tweede Kamerlid"),
        next((a["ActorNaam"] for a in actors if a.get("ActorNaam") and a["ActorNaam"] != "TK"), None),
    )

    datum = zaak.get("GestartOp", "")[:10] if zaak.get("GestartOp") else None
    termijn = zaak.get("Termijn", "")[:10] if zaak.get("Termijn") else None
    afgedaan = zaak.get("Afgedaan", False)

    return {
        "extern_id": f"tk_{zaak['Id']}",
        "gemeente_slug": GEMEENTE_SLUG,
        "type": TYPE_MAP.get(soort, soort.lower()),
        "titel": zaak.get("Onderwerp") or zaak.get("Nummer", ""),
        "indiener": indiener,
        "datum_ingediend": datum,
        "termijn_einde": termijn,
        "datum_afdoening": datum if afgedaan else None,
        "uitslag": "aangenomen" if afgedaan else "openstaand",
        "gekoppeld_evenement": zaak.get("Vergaderjaar"),
        "bron_url": tk_url(zaak["Nummer"], soort),
    }


def importeer(vanaf: str | None = None, vergaderjaar: str | None = None) -> int:
    db.init_db()

    with db.get_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO gemeenten (slug, naam) VALUES (?, ?)",
            (GEMEENTE_SLUG, "Tweede Kamer"),
        )

    totaal_nieuw = 0
    for soort in SOORTEN:
        print(f"Ophalen: {soort}")
        zaken = haal_zaken(soort, vanaf, vergaderjaar)
        print(f"  → {len(zaken)} gevonden, opslaan...")

        nieuw = 0
        for zaak in zaken:
            item = zaak_naar_item(zaak)
            is_new, _ = db.upsert_item(item)
            if is_new:
                nieuw += 1

        print(f"  → {nieuw} nieuw opgeslagen")
        totaal_nieuw += nieuw

    try:
        db.rebuild_fts()
        print("FTS index herbouwd")
    except Exception as e:
        print(f"FTS rebuild: {e}")

    return totaal_nieuw


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--jaar", help="Vergaderjaar, bijv. 2024-2025")
    parser.add_argument("--alles", action="store_true", help="Alle jaren importeren")
    parser.add_argument("--vanaf", help="Datum vanaf (YYYY-MM-DD)")
    args = parser.parse_args()

    if args.alles:
        vanaf = None
        vergaderjaar = None
    elif args.jaar:
        vanaf = None
        vergaderjaar = args.jaar
    elif args.vanaf:
        vanaf = args.vanaf
        vergaderjaar = None
    else:
        # Standaard: afgelopen 2 jaar
        vanaf = (date.today() - timedelta(days=730)).isoformat()
        vergaderjaar = None

    print(f"Tweede Kamer import — vanaf: {vanaf or 'begin'}, jaar: {vergaderjaar or 'alle'}")
    nieuw = importeer(vanaf=vanaf, vergaderjaar=vergaderjaar)
    print(f"\nKlaar: {nieuw} nieuwe items geïmporteerd")
