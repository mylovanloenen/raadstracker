"""
Media monitoring via Google News RSS.
Haalt nieuws op over Amsterdam politiek, gemeenteraad en relevante TK-onderwerpen.

Gebruik:
  python3 media_import.py
"""

import hashlib
import time
import xml.etree.ElementTree as ET
import requests
import database as db

QUERIES = [
    ("amsterdam gemeenteraad", "gemeenteraad"),
    ("amsterdam raadslid motie", "raad"),
    ("amsterdam wonen huur", "wonen"),
    ("amsterdam verkeer OV", "verkeer"),
    ("amsterdam klimaat duurzaamheid", "klimaat"),
    ("amsterdam onderwijs jeugd", "onderwijs"),
    ("amsterdam veiligheid politie", "veiligheid"),
    ("amsterdam financiën begroting", "financiën"),
    ("amsterdam zorg welzijn", "zorg"),
    ("provincie noord-holland politiek", "provincie"),
    ("waterschap amstel gooi vecht", "waterschap"),
    ("amsterdam volkshuisvesting woningbouw", "wonen"),
    ("amsterdam stikstof natuur milieu", "milieu"),
    ("amsterdam metropoolregio MRA", "regio"),
]

RSS_URL = "https://news.google.com/rss/search?q={query}&hl=nl&gl=NL&ceid=NL:nl"


def haal_feed(query: str) -> list[dict]:
    url = RSS_URL.format(query=requests.utils.quote(query))
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        root = ET.fromstring(r.content)
        items = []
        for item in root.findall(".//item"):
            titel = item.findtext("title", "").strip()
            link = item.findtext("link", "").strip()
            datum_str = item.findtext("pubDate", "")
            bron_el = item.find("source")
            bron = bron_el.text if bron_el is not None else "Onbekend"
            desc = item.findtext("description", "").strip()
            # Verwijder HTML tags uit description
            import re
            desc = re.sub(r"<[^>]+>", "", desc)[:300]

            if not titel or not link:
                continue

            # Datum parsen
            datum = None
            if datum_str:
                for fmt in ["%a, %d %b %Y %H:%M:%S %z", "%a, %d %b %Y %H:%M:%S GMT"]:
                    try:
                        from datetime import datetime
                        datum = datetime.strptime(datum_str[:31], fmt).strftime("%Y-%m-%d")
                        break
                    except ValueError:
                        continue

            extern_id = hashlib.md5(link.encode()).hexdigest()
            items.append({
                "extern_id": extern_id,
                "bron": bron,
                "titel": titel,
                "url": link,
                "samenvatting": desc,
                "datum": datum,
                "query_tag": query,
            })
        return items
    except Exception as e:
        print(f"  Fout bij '{query}': {e}")
        return []


def importeer() -> int:
    db.init_db()
    db.init_media_db()
    nieuw = 0
    for query, tag in QUERIES:
        items = haal_feed(query)
        for item in items:
            item["query_tag"] = tag
            if db.upsert_media_item(item):
                nieuw += 1
        print(f"  [{tag}] {len(items)} artikelen, {nieuw} totaal nieuw")
        time.sleep(0.5)
    return nieuw


if __name__ == "__main__":
    print("Media import gestart...")
    nieuw = importeer()
    print(f"Klaar: {nieuw} nieuwe media-items")
