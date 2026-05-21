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


def run(dagen: int = 3) -> dict:
    db.init_db()
    logger.info(f"Dagelijkse update gestart — afgelopen {dagen} dagen")

    ams = update_amsterdam()
    time.sleep(1)
    tk = update_tweedekamer(dagen)
    time.sleep(1)
    tz = update_toezeggingen()

    try:
        db.rebuild_fts()
        logger.info("FTS herbouwd")
    except Exception as e:
        logger.warning(f"FTS rebuild: {e}")

    resultaat = {"amsterdam": ams, "tweedekamer": tk, "toezeggingen": tz, "totaal": ams + tk}
    logger.info(f"Update klaar: {resultaat}")
    return resultaat


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dagen", type=int, default=3)
    args = parser.parse_args()
    run(dagen=args.dagen)
