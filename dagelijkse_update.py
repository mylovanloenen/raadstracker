"""
Dagelijkse update: haalt nieuwe items op van Amsterdam RIS en Tweede Kamer,
en ververst de status van openstaande toezeggingen.

Gebruik:
  python3 dagelijkse_update.py          # standaard (afgelopen 3 dagen)
  python3 dagelijkse_update.py --dagen 7
"""

import argparse
import logging
import time
from datetime import date, timedelta

import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def update_amsterdam() -> int:
    """Haalt recente Amsterdam-items op via de Notubiz API (update-modus)."""
    from bulk_import import importeer_module, get_max_extern_id, MODULES
    nieuw = 0
    for module_type in MODULES:
        try:
            start_id = get_max_extern_id(module_type)
            n = importeer_module(module_type, start_id=start_id, update_mode=True)
            logger.info(f"Amsterdam {module_type}: {n} nieuw")
            nieuw += n
        except Exception as e:
            logger.error(f"Amsterdam {module_type} fout: {e}")
    return nieuw


def update_tweedekamer(dagen: int = 3) -> int:
    """Haalt recente TK-items op voor de afgelopen N dagen."""
    from tk_import import importeer
    vanaf = (date.today() - timedelta(days=dagen)).isoformat()
    try:
        n = importeer(vanaf=vanaf)
        logger.info(f"Tweede Kamer: {n} nieuw")
        return n
    except Exception as e:
        logger.error(f"TK import fout: {e}")
        return 0


def update_media() -> int:
    """Haalt nieuw nieuws op via Google News RSS."""
    from media_import import importeer
    try:
        n = importeer()
        logger.info(f"Media: {n} nieuw")
        return n
    except Exception as e:
        logger.error(f"Media import fout: {e}")
        return 0


def update_rijksoverheid() -> int:
    """Haalt nieuwe Rijksoverheid-documenten op."""
    from rijksoverheid_import import importeer
    try:
        n = importeer()
        logger.info(f"Rijksoverheid: {n} nieuw")
        return n
    except Exception as e:
        logger.error(f"Rijksoverheid import fout: {e}")
        return 0


def update_agv() -> int:
    """Haalt nieuwe AGV (Waterschap Amstel, Gooi en Vecht) items op."""
    from agv_import import importeer
    try:
        n = importeer()
        logger.info(f"AGV: {n} nieuw/bijgewerkt")
        return n
    except Exception as e:
        logger.error(f"AGV import fout: {e}")
        return 0


def update_toezeggingen() -> int:
    """Ververst de status van openstaande toezeggingen."""
    from toezeggingen_import import importeer as importeer_tz
    try:
        n = importeer_tz()
        logger.info(f"Toezeggingen: {n} nieuw/bijgewerkt")
        return n
    except Exception as e:
        logger.error(f"Toezeggingen update fout: {e}")
        return 0


def stuur_alert(naam: str, email: str, matches: list) -> None:
    """Stuur een direct alert-mailtje als er nieuwe relevante stukken zijn."""
    try:
        import resend
        import os
        resend.api_key = os.environ.get("RESEND_API_KEY", "")
        from_email = os.environ.get("FROM_EMAIL", "briefing@d66-connect.com")

        items_html = "".join(
            f'<li><a href="{it.get("bron_url","#")}">{it.get("titel","")}</a> '
            f'<span style="color:#999">({it.get("indiener") or "—"}, {it.get("datum_ingediend") or "?"})</span></li>'
            for it in matches[:8]
        )
        html = f"""<html><body style="font-family:sans-serif;max-width:600px;margin:0 auto">
<div style="background:#003082;color:white;padding:20px 24px;border-radius:8px 8px 0 0">
  <h2 style="margin:0;font-size:18px">🔔 Nieuwe relevante stukken</h2>
  <p style="margin:4px 0 0;opacity:.8;font-size:13px">{naam}</p>
</div>
<div style="background:white;padding:20px 24px;border:1px solid #e2ddd8;border-top:none;border-radius:0 0 8px 8px">
  <p>Er zijn {len(matches)} nieuwe stukken die aansluiten bij jouw onderwerpen:</p>
  <ul style="line-height:1.8">{items_html}</ul>
  <p><a href="https://raadstracker.fly.dev/nieuws" style="color:#003082">Bekijk alle nieuws →</a></p>
</div></body></html>"""

        resend.Emails.send({
            "from": f"Raadstracker <{from_email}>",
            "to": [email],
            "subject": f"🔔 {len(matches)} nieuwe stukken over jouw onderwerpen",
            "html": html,
        })
        logger.info(f"Alert verstuurd naar {email}: {len(matches)} matches")
    except Exception as e:
        logger.error(f"Alert versturen mislukt: {e}")


def controle_alerts(nieuw_items: list) -> None:
    """Check nieuwe items tegen gebruikersonderwerpen en stuur alerts."""
    if not nieuw_items:
        return
    try:
        import yaml
        from pathlib import Path
        from dotenv import load_dotenv
        load_dotenv(Path(__file__).parent / ".env", override=True)

        with open(Path(__file__).parent / "gebruikers.yaml") as f:
            config = yaml.safe_load(f)

        for g in config.get("gebruikers", []):
            if not g.get("actief", True):
                continue
            onderwerpen = g.get("onderwerpen", [])
            if not onderwerpen:
                continue

            termen = [o.lower() for o in onderwerpen]
            matches = []
            for item in nieuw_items:
                tekst = (item.get("titel") or "").lower()
                if any(t in tekst or any(w in tekst for w in t.split()) for t in termen):
                    matches.append(item)

            if matches:
                stuur_alert(g["naam"], g["email"], matches)
    except Exception as e:
        logger.error(f"Alert controle mislukt: {e}")


def run(dagen: int = 3) -> dict:
    db.init_db()
    logger.info(f"Dagelijkse update gestart — afgelopen {dagen} dagen")

    ams = update_amsterdam()
    time.sleep(1)
    tk = update_tweedekamer(dagen)
    time.sleep(1)
    agv = update_agv()
    time.sleep(1)
    tz = update_toezeggingen()
    time.sleep(1)
    media = update_media()
    time.sleep(1)
    rgo = update_rijksoverheid()

    try:
        db.rebuild_fts()
        logger.info("FTS herbouwd")
    except Exception as e:
        logger.warning(f"FTS rebuild: {e}")

    # Alert: check nieuwe Amsterdam-items tegen gebruikersonderwerpen
    try:
        vandaag = db.get_nieuw_vandaag(uren=6)
        nieuw_lokaal = vandaag.get("amsterdam", []) + vandaag.get("agv", [])
        if nieuw_lokaal:
            controle_alerts(nieuw_lokaal)
    except Exception as e:
        logger.error(f"Alert check fout: {e}")

    resultaat = {
        "amsterdam": ams, "tweedekamer": tk, "agv": agv, "toezeggingen": tz,
        "media": media, "rijksoverheid": rgo, "totaal": ams + tk + agv + media + rgo,
    }
    logger.info(f"Update klaar: {resultaat}")
    return resultaat


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dagen", type=int, default=3)
    args = parser.parse_args()
    run(dagen=args.dagen)
