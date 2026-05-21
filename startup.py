"""
Startup script: initialiseert de database en importeert alle items als de DB leeg is.
Draait eenmalig bij elke deploy, vóór de webserver start.
"""

import logging
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

import database as db

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s", datefmt="%H:%M:%S")
logger = logging.getLogger(__name__)


def main():
    db.init_db()

    with db.get_connection() as conn:
        count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    tk_count = 0
    with db.get_connection() as conn:
        tk_count = conn.execute(
            "SELECT COUNT(*) FROM items WHERE gemeente_slug = 'tweedekamer'"
        ).fetchone()[0]

    if count == 0:
        logger.info("Lege database gevonden — Amsterdam bulk import starten...")
        from bulk_import import importeer_module, MODULES
        totaal = 0
        for module_type in MODULES:
            nieuw = importeer_module(module_type)
            totaal += nieuw
            logger.info(f"{module_type}: {nieuw} items geïmporteerd")
        db.rebuild_fts()
        logger.info(f"✅ Amsterdam import klaar: {totaal} items")

    if tk_count == 0:
        logger.info("Geen Tweede Kamer items — TK import starten (afgelopen 2 jaar)...")
        from tk_import import importeer
        from datetime import date, timedelta
        vanaf = (date.today() - timedelta(days=730)).isoformat()
        nieuw = importeer(vanaf=vanaf)
        logger.info(f"✅ TK import klaar: {nieuw} items")

    with db.get_connection() as conn:
        tz_count = conn.execute("SELECT COUNT(*) FROM toezeggingen").fetchone()[0]

    if tz_count == 0:
        logger.info("Geen toezeggingen — TK toezeggingen importeren...")
        from toezeggingen_import import importeer as importeer_tz
        nieuw = importeer_tz()
        logger.info(f"✅ Toezeggingen import klaar: {nieuw} items")

    if count > 0 and tk_count > 0:
        logger.info(f"Database: {count} items (incl. {tk_count} TK), {tz_count} toezeggingen")


if __name__ == "__main__":
    main()
