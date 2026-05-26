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
        vandaag["amsterdam"], vandaag["tweedekamer"],
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


# ── API: briefing (streaming) ─────────────────────────────────────────────────

@app.post("/api/briefing")
async def api_briefing(onderwerpen: str = Form(...)):
    termen = [t.strip() for t in onderwerpen.replace(",", "\n").splitlines() if t.strip()]
    items = db.zoek_voor_briefing(termen)

    if not items:
        async def geen_items():
            yield "data: Geen relevante items gevonden in het archief voor deze onderwerpen.\n\n"
            yield "data: [KLAAR]\n\n"
        return StreamingResponse(geen_items(), media_type="text/event-stream")

    context = items_als_context(items)
    prompt = f"""Je bent een ervaren politiek adviseur die een pre-meeting briefing opstelt voor een Amsterdams raadslid.

Onderwerpen: {', '.join(termen)}

Items uit het archief ({len(items)} stuks, van Amsterdam én Tweede Kamer):
{context}

Schrijf een heldere, professionele briefing in vloeiend Nederlands met de volgende opbouw:

**Kernpunten**
Wat speelt er momenteel op deze onderwerpen, zowel lokaal als nationaal? Benoem de belangrijkste ontwikkelingen concreet.

**Openstaande items**
Welke moties of vragen zijn nog niet afgedaan? Noem indiener en datum.

**Nationaal vs. lokaal**
Zijn er relevante ontwikkelingen in de Tweede Kamer die het Amsterdamse beleid raken?

**Suggestievragen**
2-3 scherpe, concrete vragen die het raadslid kan stellen.

Verwijs naar stukken met [nummer]. Geef aan of een stuk van Amsterdam of de Tweede Kamer komt. Wees concreet."""

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
async def api_vraag(vraag: str = Form(...)):
    termen = extraheer_zoektermen(vraag)
    items = db.zoek_voor_briefing(termen, limit=15)

    if not items:
        items = db.get_recent_items(limit=10)

    context = items_als_context(items)
    prompt = f"""Je bent een deskundige politiek assistent met toegang tot het archief van de Amsterdamse gemeenteraad én de Tweede Kamer.

Beantwoord de vraag op basis van de onderstaande raads- en Kameritems. Richtlijnen:
- Schrijf in vloeiend, helder Nederlands
- Geef aan of een item van Amsterdam of de Tweede Kamer afkomstig is wanneer dat relevant is
- Noem concrete details zoals indiener, datum en uitslag wanneer relevant
- Verwijs naar items met [nummer] als je ernaar verwijst
- Pas de lengte aan op de vraag: eenvoudige vragen krijgen een bondig antwoord, complexe vragen een uitgebreider antwoord
- Geef structuur aan langere antwoorden met alinea's
- Als iets niet in de beschikbare items staat, zeg dat eerlijk

Vraag: {vraag}

Beschikbare items ({len(items)} stuks):
{context}"""

    async def stream():
        client = get_claude()
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
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
