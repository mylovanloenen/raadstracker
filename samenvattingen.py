"""
Korte samenvattingen van raadsstukken voor de mail.

Leest het Notubiz-hoofddocument (PDF) van een stuk en laat Claude er een samenvatting van
één à twee zinnen van maken. Resultaat wordt in items.samenvatting bewaard, zodat elk stuk
maar één keer wordt samengevat.
"""

import io
import json
import logging
import os
import re
import time

import anthropic
import requests

import database as db

logger = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_PER_RUN = 30
BATCH = 8

# Procedurele stukken: geen samenvatting nodig
PROCEDUREEL = re.compile(
    r"verzamelagenda|besluitenlijst|agenda (concept|definitief)|lijst van ingekomen|"
    r"aanwijzing cameragebied|mandaatbesluit|rooster van aftreden|uitnodiging", re.I)


def _pdf_tekst(url: str, max_chars: int = 3500, max_pages: int = 4) -> str:
    try:
        import pypdf
        r = requests.get(url, timeout=30, headers={"User-Agent": "Mozilla/5.0 (Raadstracker)"})
        r.raise_for_status()
        if r.content[:4] != b"%PDF":
            return ""
        reader = pypdf.PdfReader(io.BytesIO(r.content))
        tekst = " ".join(" ".join((p.extract_text() or "") for p in reader.pages[:max_pages]).split())
        return tekst[:max_chars]
    except Exception as ex:
        logger.warning(f"PDF niet gelezen ({url}): {type(ex).__name__}")
        return ""


def _heeft_bron(it: dict) -> bool:
    return bool(it.get("doc_url") or it.get("toelichting"))


def vul_samenvattingen(items: list[dict]) -> int:
    """Vult it['samenvatting'] voor items zonder samenvatting. Geeft het aantal nieuwe samenvattingen."""
    uniek: dict[int, dict] = {}
    for it in items:
        if it.get("id") and it["id"] not in uniek:
            uniek[it["id"]] = it
    te_doen = [it for it in uniek.values()
               if not it.get("samenvatting") and _heeft_bron(it) and not PROCEDUREEL.search(it.get("titel") or "")]
    te_doen = te_doen[:MAX_PER_RUN]
    if not te_doen:
        return 0

    for it in te_doen:
        tekst = _pdf_tekst(it["doc_url"]) if it.get("doc_url") else ""
        it["_brontekst"] = tekst or it.get("toelichting") or ""
        time.sleep(0.2)
    te_doen = [it for it in te_doen if len(it["_brontekst"]) > 60]

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=2, timeout=90)
    gemaakt = 0
    for i in range(0, len(te_doen), BATCH):
        batch = te_doen[i:i + BATCH]
        blokken = "\n\n".join(
            f"### STUK {it['id']}\nType: {it['type']}\nTitel: {it['titel']}\nIndiener: {it.get('indiener') or '?'}\n"
            f"Tekst:\n{it['_brontekst']}" for it in batch)
        prompt = f"""Vat elk onderstaand stuk uit de Amsterdamse gemeenteraad samen voor een raadslid dat 's ochtends de mail scant.

Regels:
- Eén à twee zinnen, maximaal 35 woorden per stuk, in het Nederlands.
- Zeg wat er concreet gevraagd, voorgesteld of gemeld wordt. Bij een motie: wat het college wordt verzocht. Bij schriftelijke vragen: waar de vragen over gaan en wat de vrager wil weten. Bij een raadsbrief: het belangrijkste besluit of nieuws, met bedragen of aantallen als die er staan.
- Begin niet met de titel te herhalen en niet met "Dit stuk" of "In deze motie".
- Alleen wat in de tekst staat; niets verzinnen.

Antwoord UITSLUITEND met JSON: {{"<id>": "<samenvatting>", ...}}

{blokken}"""
        try:
            tekst = client.messages.create(model=MODEL, max_tokens=1500,
                                           messages=[{"role": "user", "content": prompt}]).content[0].text
            data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", tekst.strip()))
        except Exception as ex:
            logger.error(f"Samenvattingen batch mislukt: {ex}")
            continue
        for it in batch:
            sam = str(data.get(str(it["id"])) or "").strip()
            if sam:
                it["samenvatting"] = sam[:400]
                gemaakt += 1
                try:
                    db.bewaar_samenvatting(it["id"], it["samenvatting"])
                except Exception as ex:
                    logger.warning(f"Samenvatting niet opgeslagen voor item {it['id']}: {ex}")
    for it in te_doen:
        it.pop("_brontekst", None)
    # Zorg dat ook duplicaten in de oorspronkelijke lijst de samenvatting krijgen
    for it in items:
        if not it.get("samenvatting") and it.get("id") in uniek and uniek[it["id"]].get("samenvatting"):
            it["samenvatting"] = uniek[it["id"]]["samenvatting"]
    logger.info(f"Samenvattingen: {gemaakt} nieuw gemaakt")
    return gemaakt
