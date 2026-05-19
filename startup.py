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

    if count == 0:
        logger.info("Lege database gevonden — bulk import starten...")
        from bulk_import import importeer_module, MODULES
        totaal = 0
        for module_type in MODULES:
            nieuw = importeer_module(module_type)
            totaal += nieuw
            logger.info(f"{module_type}: {nieuw} items geïmporteerd")
        db.rebuild_fts()
        logger.info(f"✅ Bulk import klaar: {totaal} items")
    else:
        logger.info(f"Database bevat {count} items — geen import nodig")


if __name__ == "__main__":
    main()
