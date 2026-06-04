"""
Raadstracker webapp — FastAPI.

Starten: uvicorn app:app --reload
"""

import os
import json
import asyncio
import logging
from pathlib import Path

import anthropic
from datetime import date, timedelta
from fastapi import FastAPI, Request, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

import database as db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Raadstracker")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

db.init_db()


def get_claude() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


# ── Helpers ──────────────────────────────────────────────────────────────────

STOP_WOORDEN = {
    'de','het','een','van','in','op','aan','voor','door','met','is','zijn','worden',
    'heeft','hebben','dat','dit','die','deze','er','ook','maar','als','wat','wie',
    'hoe','waar','wanneer','niet','wel','zo','nog','dan','te','na','bij','tot','uit',
    'over','naar','om','geen','werd','kan','hij','zij','wij','ik','je','ze','we',
    'me','mij','hun','hen','al','wel','dus','nu','al','of','en','om','du','was',
}

TYPE_LABEL = {
    'motie': 'Motie',
    'schriftelijke_vraag': 'Schriftelijke vraag',
    'ingekomen_stuk': 'Ingekomen stuk',
    'interpellatie': 'Interpellatie',
}

BRON_LABEL = {
    'amsterdam': 'Amsterdam',
    'tweedekamer': 'Tweede Kamer',
    'waterschap_agv': 'Waterschap AGV',
}


def extraheer_zoektermen(vraag: str) -> list[str]:
    woorden = vraag.lower().replace('?', '').replace('!', '').split()
    return [w for w in woorden if len(w) > 2 and w not in STOP_WOORDEN][:10]


def items_als_context(items: list[dict]) -> str:
    lines = []
    for i, item in enumerate(items, 1):
        type_naam = TYPE_LABEL.get(item['type'], item['type'])
        uitslag = item.get('uitslag') or 'openstaand'
        bron = BRON_LABEL.get(item.get('gemeente_slug', ''), item.get('gemeente_slug', ''))
        lines.append(
            f"[{i}] [{bron}] {type_naam}: {item['titel']}\n"
            f"    Indiener: {item['indiener'] or 'onbekend'} | "
            f"Datum: {item['datum_ingediend'] or 'onbekend'} | "
            f"Status: {uitslag}"
        )
    return "\n".join(lines)


# ── Pagina's ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, q: str = "", type: str = "", bron: str = "", page: int = 1):
    resultaat = db.zoek_items(q=q, type_filter=type, gemeente_slug=bron, page=page)
    stats = db.get_stats(bron)
    today = date.today().isoformat()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "items": resultaat["items"],
        "totaal": resultaat["totaal"],
        "page": resultaat["page"],
        "pages": resultaat["pages"],
        "q": q,
        "type": type,
        "bron": bron,
        "stats": stats,
        "today": today,
        "week": (date.today() + timedelta(days=7)).isoformat(),
    })


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/item/{item_id}", response_class=HTMLResponse)
async def item_detail(request: Request, item_id: int):
    item = db.get_item(item_id)
    if not item:
        return HTMLResponse("Item niet gevonden", status_code=404)
    return templates.TemplateResponse("item.html", {"request": request, "item": item})


@app.get("/briefing", response_class=HTMLResponse)
async def briefing_page(request: Request):
    return templates.TemplateResponse("briefing.html", {"request": request})


@app.get("/nieuws", response_class=HTMLResponse)
async def nieuws_page(request: Request):
    db.init_media_db()
    vandaag = db.get_nieuw_vandaag(uren=24)
    media = db.get_recent_media(uren=48)
    stats = db.get_stats()
    heeft_nieuws = any([
        vandaag["amsterdam"], vandaag["tweedekamer"], vandaag["agv"],
        vandaag["toezeggingen_nieuw"], vandaag["toezeggingen_over"],
        media,
    ])
    return templates.TemplateResponse("nieuws.html", {
        "request": request,
        "vandaag": vandaag,
        "media": media,
        "heeft_nieuws": heeft_nieuws,
        "today": date.today().isoformat(),
        "stats": stats,
    })


@app.get("/vraag", response_class=HTMLResponse)
async def vraag_page(request: Request):
    return templates.TemplateResponse("vraag.html", {"request": request})


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    return templates.TemplateResponse("chat.html", {"request": request})


@app.get("/toezeggingen", response_class=HTMLResponse)
async def toezeggingen_page(
    request: Request,
    q: str = "",
    status: str = "Openstaand",
    ministerie: str = "",
    page: int = 1,
):
    resultaat = db.get_toezeggingen(status=status, ministerie=ministerie, q=q, page=page)
    stats = db.get_toezegging_stats()
    today = date.today().isoformat()
    return templates.TemplateResponse("toezeggingen.html", {
        "request": request,
        "items": resultaat["items"],
        "totaal": resultaat["totaal"],
        "page": resultaat["page"],
        "pages": resultaat["pages"],
        "ministeries": resultaat["ministeries"],
        "stats": stats,
        "q": q,
        "status": status,
        "ministerie": ministerie,
        "today": today,
    })


@app.get("/agenda", response_class=HTMLResponse)
async def agenda_page(request: Request):
    import requests as req
    from datetime import timedelta
    vanaf = date.today().isoformat()
    tot = (date.today() + timedelta(days=56)).isoformat()
    try:
        r = req.get(
            "https://api.notubiz.nl/organisations/281/events",
            params={"format": "json", "date_from": vanaf, "date_to": tot},
            timeout=10,
        )
        events_raw = r.json().get("events", {}).get("event", [])
        if isinstance(events_raw, dict):
            events_raw = [events_raw]
        vergaderingen = []
        for e in events_raw:
            attrs = e.get("@attributes", {})
            cat = e.get("category", {})
            cat_type = cat.get("type", {}).get("label", "")
            vergaderingen.append({
                "id": attrs.get("id"),
                "datum": attrs.get("date"),
                "tijd": attrs.get("time", ""),
                "titel": e.get("title", ""),
                "locatie": e.get("location", ""),
                "categorie": cat.get("title", ""),
                "categorie_type": cat_type,
                "agenda_items": attrs.get("agenda_item_count", 0),
                "url": e.get("url", "").replace("http://", "https://"),
                "kleur": cat.get("type", {}).get("color", "#666"),
            })
    except Exception as ex:
        logger.error(f"Agenda ophalen mislukt: {ex}")
        vergaderingen = []
    return templates.TemplateResponse("agenda.html", {
        "request": request,
        "vergaderingen": vergaderingen,
        "today": date.today().isoformat(),
    })


@app.get("/statistieken", response_class=HTMLResponse)
async def statistieken_page(request: Request):
    stats = db.get_statistieken()
    return templates.TemplateResponse("statistieken.html", {
        "request": request,
        "stats": stats,
    })


@app.get("/fracties", response_class=HTMLResponse)
async def fracties_page(request: Request, fractie: str = "", page: int = 1):
    resultaat = db.zoek_items(q=fractie, gemeente_slug="amsterdam", page=page) if fractie else {"items": [], "totaal": 0, "page": 1, "pages": 0}
    # Haal top fracties op voor de lijst
    import sqlite3
    with db.get_connection() as conn:
        top = conn.execute(
            """SELECT indiener, COUNT(*) as n FROM items
               WHERE gemeente_slug = 'amsterdam' AND indiener IS NOT NULL AND indiener != ''
               AND indiener NOT LIKE '%,%'
               GROUP BY indiener ORDER BY n DESC LIMIT 20"""
        ).fetchall()
    return templates.TemplateResponse("fracties.html", {
        "request": request,
        "top_fracties": [(r["indiener"], r["n"]) for r in top],
        "fractie": fractie,
        "items": resultaat["items"],
        "totaal": resultaat["totaal"],
        "page": resultaat["page"],
        "pages": resultaat["pages"],
    })


# ── API: briefing (streaming) ─────────────────────────────────────────────────

# Synoniemen voor dunne onderwerpen — verbreed de zoekactie
SYNONIEMEN = {
    "democratisering":  ["democratisering", "participatie", "inspraak", "burgerberaad", "bewonersinitiatieven"],
    "digitale stad":    ["digitale stad", "digitalisering", "ICT", "technologie", "data", "smart city", "algoritme"],
    "opvang":           ["opvang", "daklozen", "asiel", "vluchtelingen", "maatschappelijke opvang", "noodopvang"],
    "jongerenwerk":     ["jongerenwerk", "jongeren", "jeugd", "jongerencentrum", "straatwerk"],
    "masterplan nieuw-west": ["nieuw-west", "masterplan nieuw-west", "osdorp", "geuzenveld", "slotervaart"],
    "masterplan zuidoost":   ["zuidoost", "masterplan zuidoost", "bijlmer", "amsterdam-zuidoost", "gaasperdam"],
    "stadsdeel zuidoost":    ["zuidoost", "stadsdeel zuidoost", "bijlmer", "amsterdam-zuidoost"],
}


@app.post("/api/briefing")
async def api_briefing(onderwerpen: str = Form(...)):
    termen = [t.strip() for t in onderwerpen.replace(",", "\n").splitlines() if t.strip()]

    # Verbreed zoektermen met synoniemen
    zoektermen_breed = []
    for t in termen:
        zoektermen_breed.append(t)
        for k, v in SYNONIEMEN.items():
            if t.lower() == k or t.lower() in v:
                zoektermen_breed.extend(v)
    zoektermen_breed = list(dict.fromkeys(zoektermen_breed))  # uniek

    items = db.zoek_voor_briefing(zoektermen_breed, limit=25)

    # Voeg actueel nieuws toe als aanvulling
    db.init_media_db()
    media_items = []
    try:
        with db.get_connection() as conn:
            placeholders = " OR ".join(["titel LIKE ?"] * len(termen))
            params = [f"%{t}%" for t in zoektermen_breed[:8]]
            media_rows = conn.execute(
                f"SELECT * FROM media_items WHERE ({placeholders}) ORDER BY datum DESC LIMIT 8",
                params,
            ).fetchall()
            media_items = [dict(r) for r in media_rows]
    except Exception:
        pass

    if not items and not media_items:
        async def geen_items():
            yield "data: Geen relevante items gevonden in het archief voor deze onderwerpen.\n\n"
            yield "data: [KLAAR]\n\n"
        return StreamingResponse(geen_items(), media_type="text/event-stream")

    context = items_als_context(items)

    nieuws_context = ""
    if media_items:
        nieuws_context = f"\n\nActueel nieuws over deze onderwerpen ({len(media_items)} artikelen):\n"
        for m in media_items:
            nieuws_context += f"- [{m['bron']}] {m['titel']} ({m['datum'] or '?'})\n"

    prompt = f"""Je bent een ervaren politiek adviseur die een pre-meeting briefing opstelt voor een Amsterdams raadslid.

Onderwerpen: {', '.join(termen)}

Raads- en Kameritems uit het archief ({len(items)} stuks):
{context}
{nieuws_context}
Schrijf een heldere, professionele briefing in vloeiend Nederlands met de volgende opbouw:

**Kernpunten**
Wat speelt er momenteel op deze onderwerpen? Benoem de belangrijkste ontwikkelingen concreet, inclusief actueel nieuws.

**Openstaande items**
Welke moties of vragen zijn nog niet afgedaan? Noem indiener en datum.

**Recente ontwikkelingen**
Wat is er de afgelopen periode nieuws op dit terrein (uit het nieuws en de raad)?

**Suggestievragen**
2-3 scherpe, concrete vragen die het raadslid kan stellen.

Verwijs naar archief-items met [nummer]. Wees concreet en geef aan hoe actueel de informatie is."""

    async def stream():
        client = get_claude()
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                escaped = text.replace("\n", "\\n")
                yield f"data: {escaped}\n\n"
        # Stuur bronnen mee
        bronnen = [{"nr": i+1, "titel": it["titel"], "url": it["bron_url"]} for i, it in enumerate(items)]
        yield f"data: [BRONNEN]{json.dumps(bronnen, ensure_ascii=False)}\n\n"
        yield "data: [KLAAR]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── API: vraag (streaming) ────────────────────────────────────────────────────

@app.post("/api/vraag")
async def api_vraag(vraag: str = Form(...), history: str = Form(default="[]")):
    termen = extraheer_zoektermen(vraag)
    items = db.zoek_voor_briefing(termen, limit=15)

    if not items:
        items = db.get_recent_items(limit=10)

    context = items_als_context(items)
    systeem = f"""Je bent een deskundige politiek assistent met toegang tot het archief van de Amsterdamse gemeenteraad, Tweede Kamer en Waterschap AGV.

Beantwoord vragen op basis van de onderstaande raadsitems. Richtlijnen:
- Schrijf in vloeiend, helder Nederlands
- Noem concrete details zoals indiener, datum en uitslag wanneer relevant
- Verwijs naar items met [nummer] als je ernaar verwijst
- Pas de lengte aan op de vraag: bondig voor simpele vragen, uitgebreid voor complexe
- Als iets niet in de beschikbare items staat, zeg dat eerlijk

Beschikbare items ({len(items)} stuks):
{context}"""

    # Bouw gespreksgeschiedenis op
    try:
        hist = json.loads(history)
    except Exception:
        hist = []

    messages = []
    for h in hist[-10:]:  # max 10 eerdere berichten meesturen
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": vraag})

    async def stream():
        client = get_claude()
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=1200,
            system=systeem,
            messages=messages,
        ) as s:
            for text in s.text_stream:
                escaped = text.replace("\n", "\\n")
                yield f"data: {escaped}\n\n"
        bronnen = [{"nr": i+1, "titel": it["titel"], "url": it["bron_url"]} for i, it in enumerate(items)]
        yield f"data: [BRONNEN]{json.dumps(bronnen, ensure_ascii=False)}\n\n"
        yield "data: [KLAAR]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── API: dagelijkse update (voor cron) ───────────────────────────────────────

@app.get("/api/nieuws-briefing-get")
@app.post("/api/nieuws-briefing")
async def api_nieuws_briefing():
    """Streaming AI-samenvatting van wat er vandaag nieuw is."""
    vandaag = db.get_nieuw_vandaag(uren=48)

    regels = []
    if vandaag["amsterdam"]:
        regels.append(f"\n**Nieuw Amsterdam ({len(vandaag['amsterdam'])} items):**")
        for it in vandaag["amsterdam"][:10]:
            regels.append(f"- [{TYPE_LABEL.get(it['type'], it['type'])}] {it['titel']} (indiener: {it['indiener'] or '?'}, {it['datum_ingediend'] or '?'})")

    if vandaag["tweedekamer"]:
        regels.append(f"\n**Nieuw Tweede Kamer ({len(vandaag['tweedekamer'])} items):**")
        for it in vandaag["tweedekamer"][:10]:
            regels.append(f"- [{TYPE_LABEL.get(it['type'], it['type'])}] {it['titel']} (indiener: {it['indiener'] or '?'}, {it['datum_ingediend'] or '?'})")

    if vandaag.get("agv"):
        regels.append(f"\n**Nieuw Waterschap AGV ({len(vandaag['agv'])} items):**")
        for it in vandaag["agv"][:5]:
            regels.append(f"- [{TYPE_LABEL.get(it['type'], it['type'])}] {it['titel']} (indiener: {it['indiener'] or '?'}, {it['datum_ingediend'] or '?'})")

    if vandaag["toezeggingen_over"]:
        regels.append(f"\n**Toezeggingen met verstreken deadline ({len(vandaag['toezeggingen_over'])}):**")
        for tz in vandaag["toezeggingen_over"][:5]:
            regels.append(f"- {tz['naam']} ({tz['functie']}): {tz['tekst'][:120]}… [deadline: {tz['datum_nakoming']}]")

    if not regels:
        async def leeg():
            yield "data: Er zijn vandaag geen nieuwe items in het archief.\\n\\n"
            yield "data: [KLAAR]\\n\\n"
        return StreamingResponse(leeg(), media_type="text/event-stream")

    context = "\n".join(regels)
    prompt = f"""Je bent een politiek adviseur die elke ochtend een compacte briefing opstelt voor een Amsterdams raadslid.

Hier is een overzicht van wat er de afgelopen 48 uur nieuw is binnengekomen:

{context}

Schrijf een heldere ochtend-briefing in vloeiend Nederlands. Structuur:
- Start met een zin over de stemming/toon van de nieuwe stukken
- Benoem de meest politiek relevante nieuwe items (focus op Amsterdam)
- Signaleer als er nationale TK-items zijn die direct raken aan Amsterdams beleid
- Sluit af met max. 2 aandachtspunten voor vandaag

Wees bondig — maximaal 250 woorden. Geen bullet points, gewoon lopende tekst in alinea's."""

    async def stream():
        client = get_claude()
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        ) as s:
            for text in s.text_stream:
                escaped = text.replace("\n", "\\n")
                yield f"data: {escaped}\n\n"
        yield "data: [KLAAR]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@app.post("/api/dagelijkse-update")
async def api_dagelijkse_update(background_tasks: BackgroundTasks, token: str = Form(...)):
    if token != os.environ.get("SCRAPE_TOKEN", ""):
        return {"error": "Ongeldig token"}

    def run_update():
        from dagelijkse_update import run
        run(dagen=3)

    background_tasks.add_task(run_update)
    return {"status": "dagelijkse update gestart"}


@app.post("/api/dagelijkse-briefing")
async def api_dagelijkse_briefing(background_tasks: BackgroundTasks, token: str = Form(...)):
    if token != os.environ.get("SCRAPE_TOKEN", ""):
        return {"error": "Ongeldig token"}

    def run_briefing():
        from dagelijkse_briefing import run
        run()

    background_tasks.add_task(run_briefing)
    return {"status": "briefing verstuurd"}


@app.post("/api/scrape")
async def api_scrape(background_tasks: BackgroundTasks, token: str = Form(...)):
    """Backwards-compat alias voor /api/dagelijkse-update."""
    if token != os.environ.get("SCRAPE_TOKEN", ""):
        return {"error": "Ongeldig token"}

    def run_scrape():
        from dagelijkse_update import run
        run(dagen=3)

    background_tasks.add_task(run_scrape)
    return {"status": "scrape gestart"}
