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

def items_als_context(items: list[dict]) -> str:
    lines = []
    for i, item in enumerate(items, 1):
        lines.append(
            f"[{i}] {item['type'].replace('_', ' ').title()} — {item['titel']}\n"
            f"    Indiener: {item['indiener'] or '—'} | Datum: {item['datum_ingediend'] or '?'} | "
            f"Uitslag: {item['uitslag'] or 'openstaand'}\n"
            f"    URL: {item['bron_url']}"
        )
    return "\n\n".join(lines)


# ── Pagina's ──────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def home(request: Request, q: str = "", type: str = "", page: int = 1):
    resultaat = db.zoek_items(q=q, type_filter=type, page=page)
    stats = db.get_stats()
    today = date.today().isoformat()
    return templates.TemplateResponse("index.html", {
        "request": request,
        "items": resultaat["items"],
        "totaal": resultaat["totaal"],
        "page": resultaat["page"],
        "pages": resultaat["pages"],
        "q": q,
        "type": type,
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


@app.get("/vraag", response_class=HTMLResponse)
async def vraag_page(request: Request):
    return templates.TemplateResponse("vraag.html", {"request": request})


@app.get("/chat", response_class=HTMLResponse)
async def chat_page(request: Request):
    return templates.TemplateResponse("chat.html", {"request": request})


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
    prompt = f"""Je bent een politiek assistent voor een Amsterdams raadslid.

Onderwerpen voor de briefing: {', '.join(termen)}

Relevante raadsitems uit het archief ({len(items)} stuks):
{context}

Schrijf een heldere pre-meeting briefing. Structuur:
1. **Kernpunten** (wat speelt er op deze onderwerpen?)
2. **Openstaande items** (moties of vragen zonder afdoening)
3. **Aandachtspunten** (termijnen die naderen of verstreken zijn)
4. **Suggestievragen** (2-3 concrete vragen die het raadslid kan stellen)

Wees bondig. Verwijs naar stukken met [nummer] zoals aangegeven in de context."""

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
    termen = [w for w in vraag.split() if len(w) > 2][:8]
    items = db.zoek_voor_briefing(termen, limit=12)

    if not items:
        items = db.get_recent_items(limit=10)

    context = items_als_context(items)
    prompt = f"""Je bent een assistent voor het Amsterdamse raadsarchief. Geef een kort, direct antwoord in maximaal 3-4 zinnen. Verwijs naar items met [nummer]. Als iets niet in de items staat, zeg dat eerlijk.

Vraag: {vraag}

Raadsitems:
{context}"""

    async def stream():
        client = get_claude()
        with client.messages.stream(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}],
        ) as stream:
            for text in stream.text_stream:
                escaped = text.replace("\n", "\\n")
                yield f"data: {escaped}\n\n"
        bronnen = [{"nr": i+1, "titel": it["titel"], "url": it["bron_url"]} for i, it in enumerate(items)]
        yield f"data: [BRONNEN]{json.dumps(bronnen, ensure_ascii=False)}\n\n"
        yield "data: [KLAAR]\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ── API: scrape trigger (voor cron) ──────────────────────────────────────────

@app.post("/api/scrape")
async def api_scrape(background_tasks: BackgroundTasks, token: str = Form(...)):
    if token != os.environ.get("SCRAPE_TOKEN", ""):
        return {"error": "Ongeldig token"}

    def run_scrape():
        import yaml
        from scraper import scrape_all
        with open("config.yaml") as f:
            config = yaml.safe_load(f)
        items = scrape_all(config)
        nieuw = 0
        for item in items:
            is_new, _ = db.upsert_item(item)
            if is_new:
                nieuw += 1
        try:
            db.rebuild_fts()
        except Exception:
            pass
        logger.info(f"Scrape klaar: {nieuw} nieuw van {len(items)}")

    background_tasks.add_task(run_scrape)
    return {"status": "scrape gestart"}
