"""
Raadstracker — hoofdscript (multi-user).

Gebruik:
  python3 main.py demo           # Scraper testen, niets opslaan
  python3 main.py scrape         # Nieuwe items ophalen en opslaan
  python3 main.py match          # Items matchen per gebruiker via Claude
  python3 main.py mail-preview   # HTML-mail per gebruiker opslaan als preview
  python3 main.py mail           # Mails versturen via Resend
  python3 main.py run            # Volledige dagelijkse run
  python3 main.py gebruikers     # Toon alle gebruikers in database
  python3 main.py schedule       # Start dagelijkse scheduler (07:00)
"""

import sys
import logging
import os

import yaml
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent / ".env", override=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = "config.yaml"
GEBRUIKERS_PATH = "gebruikers.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_gebruikers_config() -> list[dict]:
    with open(GEBRUIKERS_PATH, encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data.get("gebruikers", [])


def _init() -> None:
    """Initialiseer database en sync gebruikers."""
    import database as db
    db.init_db()
    db.sync_gebruikers(load_gebruikers_config())


# ── Demo ─────────────────────────────────────────────────────────────────────

def cmd_demo() -> None:
    from scraper import scrape_all
    config = load_config()

    print("\n🔍 Amsterdam RIS scraper — demo\n")
    items = scrape_all(config)

    counts: dict[str, int] = {}
    for item in items:
        counts[item["type"]] = counts.get(item["type"], 0) + 1

    print(f"Totaal: {len(items)} items — {', '.join(f'{n} {t}' for t, n in counts.items())}\n")

    for item in items:
        print(f"[{item['type'].upper()[:3]}] {item['titel'] or '?'}")
        print(f"       {item['indiener'] or '—'} | {item['datum_ingediend'] or '?'} | termijn {item['termijn_einde'] or '?'}")
        print(f"       {item['bron_url']}")
        print()

    print("✅ Demo klaar.")


# ── Scrape ────────────────────────────────────────────────────────────────────

def cmd_scrape() -> None:
    from scraper import scrape_all
    import database as db

    _init()
    config = load_config()
    items = scrape_all(config)
    nieuw, bijgewerkt = 0, 0

    for item in items:
        is_new, _ = db.upsert_item(item)
        if is_new:
            nieuw += 1
        else:
            bijgewerkt += 1

    print(f"\n✅ {nieuw} nieuw, {bijgewerkt} bijgewerkt ({len(items)} totaal).")


# ── Match (per gebruiker) ─────────────────────────────────────────────────────

def cmd_match() -> None:
    import anthropic
    import database as db
    from matcher import check_relevantie

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("❌ ANTHROPIC_API_KEY niet gevonden in .env")
        sys.exit(1)

    _init()
    config = load_config()
    drempel = config.get("relevantie_drempel", 6)
    client = anthropic.Anthropic(api_key=api_key)
    gebruikers = db.get_active_gebruikers()

    if not gebruikers:
        print("Geen actieve gebruikers in gebruikers.yaml")
        return

    for gebruiker in gebruikers:
        onderwerpen = db.get_onderwerpen(gebruiker["id"])
        ongematched = db.get_unmatched_items(gebruiker["id"], gebruiker["gemeente_slug"])

        if not ongematched:
            print(f"[{gebruiker['naam']}] Niets te matchen.")
            continue

        print(f"\n[{gebruiker['naam']}] {len(ongematched)} items matchen op: {', '.join(onderwerpen)}\n")
        relevant = 0

        for item in ongematched:
            score, tags = check_relevantie(client, item["titel"] or "", item["type"], onderwerpen)
            db.upsert_gebruiker_item(gebruiker["id"], item["id"], score, tags)

            marker = "✓" if score >= drempel else "✗"
            print(f"  {marker} [{score}/10] {(item['titel'] or '?')[:65]}")
            if tags:
                print(f"           → {tags}")
            if score >= drempel:
                relevant += 1

        print(f"\n  → {relevant} relevant (≥{drempel}) voor {gebruiker['naam']}")


# ── Mail preview ──────────────────────────────────────────────────────────────

def cmd_mail_preview() -> None:
    import database as db
    from mailer import render_mail

    _init()
    config = load_config()
    drempel = config.get("relevantie_drempel", 6)
    gebruikers = db.get_active_gebruikers()

    for gebruiker in gebruikers:
        buckets = db.get_dashboard_buckets(gebruiker["id"], drempel)
        html = render_mail(buckets, naam=gebruiker["naam"])
        pad = f"mail_preview_{gebruiker['id']}.html"
        with open(pad, "w", encoding="utf-8") as f:
            f.write(html)
        totaal = sum(len(v) for v in buckets.values())
        print(f"📧 {gebruiker['naam']} ({gebruiker['email']}) → {pad} ({totaal} items)")
        print(f"   open {pad}")


# ── Mail versturen ────────────────────────────────────────────────────────────

def cmd_mail() -> None:
    import database as db
    from mailer import render_mail, send_mail

    if not os.environ.get("RESEND_API_KEY"):
        print("❌ RESEND_API_KEY niet gevonden in .env")
        sys.exit(1)

    _init()
    config = load_config()
    drempel = config.get("relevantie_drempel", 6)
    gebruikers = db.get_active_gebruikers()

    for gebruiker in gebruikers:
        buckets = db.get_dashboard_buckets(gebruiker["id"], drempel)
        html = render_mail(buckets, naam=gebruiker["naam"])
        send_mail(html, recipient=gebruiker["email"], naam=gebruiker["naam"])
        print(f"✅ Mail verstuurd → {gebruiker['email']} ({gebruiker['naam']})")


# ── Gebruikers overzicht ──────────────────────────────────────────────────────

def cmd_gebruikers() -> None:
    import database as db
    _init()
    gebruikers = db.get_active_gebruikers()
    if not gebruikers:
        print("Geen gebruikers. Voeg ze toe in gebruikers.yaml")
        return
    print(f"\n{len(gebruikers)} actieve gebruiker(s):\n")
    for g in gebruikers:
        onderwerpen = db.get_onderwerpen(g["id"])
        print(f"  #{g['id']} {g['naam']} <{g['email']}> ({g['gemeente_slug']})")
        for o in onderwerpen:
            print(f"      • {o}")
        print()


# ── Volledige run ─────────────────────────────────────────────────────────────

def cmd_run() -> None:
    logger.info("=== Dagelijkse run gestart ===")
    cmd_scrape()
    cmd_match()
    cmd_mail()
    logger.info("=== Dagelijkse run klaar ===")


# ── Scheduler ─────────────────────────────────────────────────────────────────

def cmd_schedule() -> None:
    from apscheduler.schedulers.blocking import BlockingScheduler

    config = load_config()
    tijd = config.get("mail", {}).get("verstuur_tijd", "07:00")
    uur, minuut = map(int, tijd.split(":"))

    scheduler = BlockingScheduler(timezone="Europe/Amsterdam")
    scheduler.add_job(cmd_run, "cron", hour=uur, minute=minuut)

    print(f"⏰ Scheduler gestart — dagelijkse run om {tijd} (Amsterdam)")
    print("   Ctrl+C om te stoppen.")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("\nScheduler gestopt.")


# ── Entry point ───────────────────────────────────────────────────────────────

COMMANDS = {
    "demo": cmd_demo,
    "scrape": cmd_scrape,
    "match": cmd_match,
    "mail-preview": cmd_mail_preview,
    "mail": cmd_mail,
    "gebruikers": cmd_gebruikers,
    "run": cmd_run,
    "schedule": cmd_schedule,
}

if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "demo"
    if cmd not in COMMANDS:
        print(f"Onbekend commando '{cmd}'. Keuzes: {', '.join(COMMANDS)}")
        sys.exit(1)
    COMMANDS[cmd]()
