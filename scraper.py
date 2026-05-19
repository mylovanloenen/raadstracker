"""
Scraper voor Amsterdam raadsinformatie (amsterdam.raadsinformatie.nl).

Strategie: server-side rendered HTML parsing.
De pagina's renderen de meest recente 10 items per module volledig in HTML
(voor JavaScript-disabled browsers). Data zit in <tr data-id="..."> rows
met CSS-klassen als field_1 (titel), field_15 (datum), field_17 (afdoening).
"""

import re
import time
import logging
from datetime import date, timedelta

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "nl-NL,nl;q=0.9",
}

BASE_URL = "https://amsterdam.raadsinformatie.nl"

MODULES = {
    "motie": {
        "module_id": 6,
        "list_path": "moties_en_amendementen",
        "detail_name": "Moties%20en%20amendementen",
        "termijn_weken": 13,
        "datum_field": 15,
        "afdoening_field": 17,
    },
    "schriftelijke_vraag": {
        "module_id": 4,
        "list_path": "schriftelijke_vragen",
        "detail_name": "Schriftelijke%20vragen",
        "termijn_weken": 4,
        "datum_field": 15,
        "afdoening_field": 17,
    },
    "ingekomen_stuk": {
        "module_id": 1,
        "list_path": "berichten_uit_het_college",
        "detail_name": "Ingekomen%20stukken",
        "termijn_weken": 6,
        "datum_field": 15,
        "afdoening_field": 17,
    },
}


def _parse_nl_date(val: str | None) -> date | None:
    if not val:
        return None
    val = val.strip()
    for fmt in ("%d-%m-%Y", "%Y-%m-%d"):
        try:
            return date(*time.strptime(val, fmt)[:3])
        except (ValueError, TypeError):
            continue
    return None


def _field_text(row, field_id: int) -> str | None:
    el = row.select_one(f".field_{field_id} div")
    if not el:
        el = row.select_one(f".field_{field_id}")
    if not el:
        return None
    text = el.get_text(separator=" ", strip=True)
    # Normalize whitespace including embedded newlines
    text = " ".join(text.split())
    return text if text else None


def scrape_module(module_type: str, termijnen_config: dict | None = None) -> list[dict]:
    """
    Scrape the most recent items from an Amsterdam RIS module.
    Returns a list of item dicts ready for database insertion.
    """
    cfg = MODULES[module_type]
    url = f"{BASE_URL}/modules/{cfg['module_id']}/{cfg['list_path']}/view"

    logger.info(f"Scraping {module_type} from {url}")

    response = requests.get(url, headers=HEADERS, timeout=30)
    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")
    rows = soup.select("tr[data-id]")

    logger.info(f"Found {len(rows)} rows for {module_type}")

    termijn_weken = cfg["termijn_weken"]
    if termijnen_config and module_type in termijnen_config:
        termijn_weken = termijnen_config[module_type]

    items = []
    for row in rows:
        extern_id = row.get("data-id")
        if not extern_id:
            continue

        titel = _field_text(row, 1)
        datum_str = _field_text(row, cfg["datum_field"])
        type_val = _field_text(row, 45) or module_type
        fractie = _field_text(row, 37)
        uitslag = _field_text(row, 62)
        gekoppeld = _field_text(row, 54)
        datum_afdoening_str = _field_text(row, cfg["afdoening_field"])

        datum_ingediend = _parse_nl_date(datum_str)
        datum_afdoening = _parse_nl_date(datum_afdoening_str)

        termijn_einde = None
        if datum_ingediend:
            termijn_einde = datum_ingediend + timedelta(weeks=termijn_weken)

        detail_name = cfg["detail_name"]
        bron_url = f"{BASE_URL}/modules/{cfg['module_id']}/{detail_name}/{extern_id}"

        items.append(
            {
                "extern_id": extern_id,
                "type": module_type,
                "titel": titel,
                "indiener": fractie,
                "datum_ingediend": datum_ingediend.isoformat() if datum_ingediend else None,
                "termijn_einde": termijn_einde.isoformat() if termijn_einde else None,
                "datum_afdoening": datum_afdoening.isoformat() if datum_afdoening else None,
                "uitslag": uitslag,
                "gekoppeld_evenement": gekoppeld,
                "bron_url": bron_url,
            }
        )

    return items


def scrape_all(config: dict) -> list[dict]:
    """Scrape all enabled modules. Returns combined list of items."""
    bronnen = config.get("bronnen", {})
    termijnen = config.get("termijnen_weken", {})

    all_items = []

    module_map = {
        "moties": "motie",
        "schriftelijke_vragen": "schriftelijke_vraag",
        "ingekomen_stukken": "ingekomen_stuk",
    }

    for config_key, module_type in module_map.items():
        if not bronnen.get(config_key, True):
            logger.info(f"Bron {config_key} uitgeschakeld in config")
            continue
        try:
            items = scrape_module(module_type, termijnen)
            all_items.extend(items)
            time.sleep(1)
        except Exception as e:
            logger.error(f"Fout bij scrapen {module_type}: {e}")

    return all_items
