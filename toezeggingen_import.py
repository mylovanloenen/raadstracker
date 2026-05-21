"""
Tweede Kamer toezeggingen importer.
Haalt alle openstaande (en recent afgedane) toezeggingen op.

Gebruik:
  python3 toezeggingen_import.py              # openstaand + deels afgedaan
  python3 toezeggingen_import.py --alles      # alle statussen
"""

import argparse
import time
import requests
import database as db

BASE = "https://gegevensmagazijn.tweedekamer.nl/OData/v4/2.0"


def haal_pagina(url: str, params: dict, poging: int = 0) -> dict:
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json()
    except Exception:
        if poging < 3:
            time.sleep(2 ** poging)
            return haal_pagina(url, params, poging + 1)
        raise


def haal_toezeggingen(statussen: list[str]) -> list[dict]:
    url = f"{BASE}/Toezegging"
    status_filter = " or ".join(f"Status eq '{s}'" for s in statussen)
    params = {
        "$filter": f"Verwijderd eq false and ({status_filter})",
        "$select": "Id,Nummer,Naam,Functie,Ministerie,Status,Tekst,Aanmaakdatum,DatumNakoming,ActiviteitNummer",
        "$orderby": "Aanmaakdatum desc",
        "$top": 100,
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
        print(f"  Toezeggingen: {len(resultaten)} opgehaald...", end="\r")
        if len(batch) < 100:
            break
        skip += 100
        time.sleep(0.2)

    print()
    return resultaten


def tk_naar_toezegging(raw: dict) -> dict:
    datum_nakoming = raw.get("DatumNakoming", "")
    if datum_nakoming and datum_nakoming.startswith("0001"):
        datum_nakoming = None
    elif datum_nakoming:
        datum_nakoming = datum_nakoming[:10]

    aanmaakdatum = raw.get("Aanmaakdatum", "")
    if aanmaakdatum:
        aanmaakdatum = aanmaakdatum[:10]

    return {
        "extern_id": f"tk_tz_{raw['Id']}",
        "nummer": raw.get("Nummer"),
        "bron": "tweedekamer",
        "tekst": raw.get("Tekst", "").strip(),
        "status": raw.get("Status", "Openstaand"),
        "naam": raw.get("Naam"),
        "functie": raw.get("Functie"),
        "ministerie": raw.get("Ministerie"),
        "activiteit_nummer": raw.get("ActiviteitNummer"),
        "aanmaakdatum": aanmaakdatum,
        "datum_nakoming": datum_nakoming,
    }


def importeer(alles: bool = False) -> int:
    db.init_db()

    if alles:
        statussen = ["Openstaand", "Deels Afgedaan", "Afgedaan", "Vervallen"]
    else:
        statussen = ["Openstaand", "Deels Afgedaan"]

    print(f"Ophalen toezeggingen met status: {', '.join(statussen)}")
    raws = haal_toezeggingen(statussen)
    print(f"→ {len(raws)} gevonden, opslaan...")

    nieuw = 0
    for raw in raws:
        t = tk_naar_toezegging(raw)
        if t["tekst"] and db.upsert_toezegging(t):
            nieuw += 1

    print(f"→ {nieuw} nieuw opgeslagen")
    return nieuw


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--alles", action="store_true")
    args = parser.parse_args()

    nieuw = importeer(alles=args.alles)
    print(f"\nKlaar: {nieuw} nieuwe toezeggingen geïmporteerd")
