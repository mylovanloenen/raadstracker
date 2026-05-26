"""
Dagelijkse briefing via e-mail.
Genereert een AI-samenvatting van het nieuws van de dag en verstuurt die per mail.

Gebruik:
  python3 dagelijkse_briefing.py
"""

import os
import logging
from datetime import date, datetime

import anthropic
import resend
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env", override=True)
import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

MAIL_TEMPLATE = """<!DOCTYPE html>
<html lang="nl">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
         font-size: 15px; color: #1a1a2e; max-width: 680px; margin: 0 auto;
         background: #f9f9f8; padding: 0; }}
  .header {{ background: #003082; color: white; padding: 24px 32px; }}
  .header h1 {{ margin: 0; font-size: 20px; font-weight: 700; }}
  .header p {{ margin: 4px 0 0; font-size: 13px; opacity: .75; }}
  .body {{ background: white; padding: 28px 32px; }}
  .ai-blok {{ background: #f0eeeb; border-left: 4px solid #003082;
              padding: 16px 20px; border-radius: 0 8px 8px 0; margin-bottom: 28px; }}
  .ai-blok p {{ margin: 0 0 8px; line-height: 1.65; }}
  .ai-blok p:last-child {{ margin-bottom: 0; }}
  h2 {{ font-size: 14px; font-weight: 700; color: #6b6963; text-transform: uppercase;
        letter-spacing: .5px; margin: 28px 0 12px; border-bottom: 1px solid #e2ddd8;
        padding-bottom: 6px; }}
  .item {{ padding: 10px 0; border-bottom: 1px solid #f0eeeb; }}
  .item:last-child {{ border-bottom: none; }}
  .item-titel {{ font-weight: 600; font-size: 14px; color: #1a1a2e; }}
  .item-titel a {{ color: #003082; text-decoration: none; }}
  .item-titel a:hover {{ text-decoration: underline; }}
  .item-meta {{ font-size: 12px; color: #9b9790; margin-top: 3px; }}
  .badge {{ display: inline-block; padding: 1px 7px; border-radius: 20px;
            font-size: 11px; font-weight: 600; margin-right: 4px; }}
  .badge-ams {{ background: #e8edf6; color: #003082; }}
  .badge-tk {{ background: #fff3e0; color: #b45309; }}
  .badge-agv {{ background: #e8f5e9; color: #2e7d32; }}
  .badge-media {{ background: #f0eeeb; color: #6b6963; }}
  .badge-tz {{ background: #fdecea; color: #c0392b; }}
  .footer {{ padding: 20px 32px; font-size: 12px; color: #9b9790;
             border-top: 1px solid #e2ddd8; background: #f9f9f8; }}
  .footer a {{ color: #003082; }}
</style>
</head>
<body>
<div class="header">
  <h1>Raadstracker — Dagelijkse briefing</h1>
  <p>{naam} &bull; {datum}</p>
</div>
<div class="body">

<div class="ai-blok">
{ai_samenvatting}
</div>

{nieuw_amsterdam}
{nieuw_tk}
{nieuw_agv}
{media_sectie}
{toezeggingen_sectie}

</div>
<div class="footer">
  <a href="https://raadstracker.fly.dev">Raadstracker</a> &bull;
  <a href="https://raadstracker.fly.dev/nieuws">Nieuws vandaag</a> &bull;
  <a href="https://raadstracker.fly.dev/toezeggingen">Toezeggingen</a>
</div>
</body>
</html>"""


def genereer_ai_samenvatting(vandaag: dict, media: list) -> str:
    """Genereer een vloeiende AI-briefing via Claude."""
    regels = []

    if vandaag["amsterdam"]:
        regels.append(f"\nNieuwe Amsterdam-items ({len(vandaag['amsterdam'])}):")
        for it in vandaag["amsterdam"][:8]:
            regels.append(f"- [{it['type']}] {it['titel']} (indiener: {it['indiener'] or '?'})")

    if vandaag["tweedekamer"]:
        regels.append(f"\nNieuwe TK-items ({len(vandaag['tweedekamer'])}):")
        for it in vandaag["tweedekamer"][:6]:
            regels.append(f"- {it['titel']} (indiener: {it['indiener'] or '?'})")

    if vandaag.get("agv"):
        regels.append(f"\nNieuwe Waterschap AGV-items ({len(vandaag['agv'])}):")
        for it in vandaag["agv"][:4]:
            regels.append(f"- [{it['type']}] {it['titel']} (indiener: {it['indiener'] or '?'})")

    if vandaag["toezeggingen_over"]:
        regels.append(f"\nToezeggingen met verstreken deadline ({len(vandaag['toezeggingen_over'])}):")
        for tz in vandaag["toezeggingen_over"][:3]:
            regels.append(f"- {tz['naam']} ({tz['functie']}): {tz['tekst'][:100]}…")

    if media:
        regels.append(f"\nActueel nieuws ({len(media)} artikelen):")
        for m in media[:6]:
            regels.append(f"- [{m['bron']}] {m['titel']}")

    if not regels:
        return "<p>Geen nieuwe items vandaag.</p>"

    context = "\n".join(regels)
    prompt = f"""Je bent een politiek adviseur die elke ochtend een compacte nieuwsbrief schrijft voor een Amsterdams raadslid.

Hier is een overzicht van wat er de afgelopen 24 uur nieuw is:
{context}

Schrijf een vloeiende ochtend-briefing van maximaal 200 woorden. Gebruik alinea's, geen bullet points.
Begin met de belangrijkste ontwikkeling. Sluit af met één concrete aandachtspunt voor vandaag.
Schrijf in vloeiend Nederlands, informeel maar professioneel."""

    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    tekst = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=400,
        messages=[{"role": "user", "content": prompt}],
    ).content[0].text

    # Zet alinea's om naar HTML
    html = "".join(f"<p>{p.strip()}</p>" for p in tekst.split("\n\n") if p.strip())
    return html


def bouw_items_html(items: list, badge_class: str, badge_label: str) -> str:
    if not items:
        return ""
    html = ""
    for it in items[:10]:
        url = it.get("bron_url", "#")
        titel = it.get("titel", "(geen titel)")
        meta = f"{it.get('indiener') or '—'} &bull; {it.get('datum_ingediend') or '?'}"
        html += f"""
    <div class="item">
      <div class="item-titel">
        <span class="badge {badge_class}">{badge_label}</span>
        <a href="{url}">{titel}</a>
      </div>
      <div class="item-meta">{meta}</div>
    </div>"""
    return html


def bouw_media_html(media: list) -> str:
    if not media:
        return ""
    html = "<h2>Nieuws</h2>"
    for m in media[:8]:
        html += f"""
    <div class="item">
      <div class="item-titel">
        <span class="badge badge-media">{m.get('bron','Nieuws')}</span>
        <a href="{m.get('url','#')}">{m.get('titel','')}</a>
      </div>
      <div class="item-meta">{m.get('datum') or '—'}</div>
    </div>"""
    return html


def bouw_toezeggingen_html(items: list) -> str:
    if not items:
        return ""
    html = "<h2>⚠ Toezeggingen — deadline verstreken</h2>"
    for tz in items[:5]:
        html += f"""
    <div class="item">
      <div class="item-titel">
        <span class="badge badge-tz">Verstreken</span>
        {tz.get('naam','')} ({tz.get('functie','')})
      </div>
      <div class="item-meta">{tz.get('tekst','')[:150]}…</div>
      <div class="item-meta">Deadline: {tz.get('datum_nakoming','?')}</div>
    </div>"""
    return html


def stuur_briefing(naam: str, email: str) -> bool:
    db.init_db()
    db.init_media_db()

    vandaag = db.get_nieuw_vandaag(uren=24)
    media = db.get_recent_media(uren=24)

    heeft_inhoud = any([
        vandaag["amsterdam"], vandaag["tweedekamer"], vandaag.get("agv"),
        vandaag["toezeggingen_over"], media,
    ])

    if not heeft_inhoud:
        logger.info(f"Geen nieuw nieuws voor {email} — mail overgeslagen")
        return False

    logger.info(f"AI briefing genereren voor {naam}...")
    ai_html = genereer_ai_samenvatting(vandaag, media)

    ams_html = ""
    if vandaag["amsterdam"]:
        ams_html = f"<h2>Nieuw Amsterdam ({len(vandaag['amsterdam'])})</h2>"
        ams_html += bouw_items_html(vandaag["amsterdam"], "badge-ams", "AMS")

    tk_html = ""
    if vandaag["tweedekamer"]:
        tk_html = f"<h2>Nieuw Tweede Kamer ({len(vandaag['tweedekamer'])})</h2>"
        tk_html += bouw_items_html(vandaag["tweedekamer"], "badge-tk", "TK")

    agv_html = ""
    if vandaag.get("agv"):
        agv_html = f"<h2>Nieuw Waterschap AGV ({len(vandaag['agv'])})</h2>"
        agv_html += bouw_items_html(vandaag["agv"], "badge-agv", "AGV")

    html = MAIL_TEMPLATE.format(
        naam=naam,
        datum=date.today().strftime("%-d %B %Y"),
        ai_samenvatting=ai_html,
        nieuw_amsterdam=ams_html,
        nieuw_tk=tk_html,
        nieuw_agv=agv_html,
        media_sectie=bouw_media_html(media),
        toezeggingen_sectie=bouw_toezeggingen_html(vandaag["toezeggingen_over"]),
    )

    resend.api_key = os.environ["RESEND_API_KEY"]
    from_email = os.environ.get("FROM_EMAIL", "briefing@raadstracker.nl")

    resend.Emails.send({
        "from": f"Raadstracker <{from_email}>",
        "to": [email],
        "subject": f"Raadstracker {date.today().strftime('%-d %b')} — dagelijkse briefing",
        "html": html,
    })

    logger.info(f"✅ Briefing verstuurd naar {email}")
    return True


def run():
    import yaml
    with open(Path(__file__).parent / "gebruikers.yaml") as f:
        config = yaml.safe_load(f)

    verstuurd = 0
    for g in config.get("gebruikers", []):
        if not g.get("actief", True):
            continue
        try:
            if stuur_briefing(g["naam"], g["email"]):
                verstuurd += 1
        except Exception as e:
            logger.error(f"Fout bij {g['email']}: {e}")

    logger.info(f"Briefings verstuurd: {verstuurd}")


if __name__ == "__main__":
    run()
