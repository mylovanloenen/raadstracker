"""Vergaderagenda gemeenteraad Amsterdam via de Notubiz events-API. Gedeeld door /agenda en de mail."""

import logging
from datetime import date, timedelta

import requests

logger = logging.getLogger(__name__)


def haal_agenda(dagen: int = 56, org_id: int = 281) -> list[dict]:
    vanaf = date.today().isoformat()
    tot = (date.today() + timedelta(days=dagen)).isoformat()
    try:
        r = requests.get(
            f"https://api.notubiz.nl/organisations/{org_id}/events",
            params={"format": "json", "date_from": vanaf, "date_to": tot},
            timeout=10,
        )
        events_raw = r.json().get("events", {}).get("event", [])
        if isinstance(events_raw, dict):
            events_raw = [events_raw]
    except Exception as ex:
        logger.error(f"Agenda ophalen mislukt: {ex}")
        return []
    vergaderingen = []
    for e in events_raw:
        attrs = e.get("@attributes", {})
        cat = e.get("category", {}) or {}
        vergaderingen.append({
            "id": attrs.get("id"),
            "datum": attrs.get("date"),
            "tijd": attrs.get("time", ""),
            "titel": e.get("title", ""),
            "locatie": e.get("location", ""),
            "categorie": cat.get("title", ""),
            "categorie_type": (cat.get("type") or {}).get("label", ""),
            "agenda_items": attrs.get("agenda_item_count", 0),
            "url": (e.get("url", "") or "").replace("http://", "https://"),
            "kleur": (cat.get("type") or {}).get("color", "#666"),
        })
    vergaderingen.sort(key=lambda v: (v["datum"] or "", v["tijd"] or ""))
    return vergaderingen
