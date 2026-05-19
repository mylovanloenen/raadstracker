import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "tracker.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS gemeenten (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    slug TEXT UNIQUE NOT NULL,
    naam TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(
    titel, indiener, type, uitslag, content='items', content_rowid='id'
);

CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extern_id TEXT NOT NULL,
    gemeente_slug TEXT NOT NULL DEFAULT 'amsterdam',
    type TEXT NOT NULL,
    titel TEXT,
    indiener TEXT,
    datum_ingediend TEXT,
    termijn_einde TEXT,
    datum_afdoening TEXT,
    uitslag TEXT,
    gekoppeld_evenement TEXT,
    bron_url TEXT,
    laatste_check TEXT,
    aangemaakt TEXT DEFAULT (datetime('now')),
    UNIQUE(extern_id, gemeente_slug)
);

CREATE TABLE IF NOT EXISTS gebruikers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    naam TEXT NOT NULL,
    email TEXT UNIQUE NOT NULL,
    gemeente_slug TEXT NOT NULL DEFAULT 'amsterdam',
    actief INTEGER DEFAULT 1,
    aangemaakt TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS gebruiker_onderwerpen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gebruiker_id INTEGER NOT NULL REFERENCES gebruikers(id) ON DELETE CASCADE,
    onderwerp TEXT NOT NULL,
    UNIQUE(gebruiker_id, onderwerp)
);

CREATE TABLE IF NOT EXISTS gebruiker_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gebruiker_id INTEGER NOT NULL REFERENCES gebruikers(id) ON DELETE CASCADE,
    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
    relevantiescore INTEGER,
    relevantie_tags TEXT,
    status TEXT DEFAULT 'open',
    afdoening_notitie TEXT,
    aangemaakt TEXT DEFAULT (datetime('now')),
    UNIQUE(gebruiker_id, item_id)
);
"""


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    with get_connection() as conn:
        conn.executescript(SCHEMA)
        conn.execute(
            "INSERT OR IGNORE INTO gemeenten (slug, naam) VALUES ('amsterdam', 'Amsterdam')"
        )


def sync_gebruikers(gebruikers_config: list[dict]) -> None:
    """Sync gebruikers.yaml into the database."""
    with get_connection() as conn:
        for g in gebruikers_config:
            conn.execute(
                """INSERT INTO gebruikers (naam, email, gemeente_slug, actief)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(email) DO UPDATE SET
                     naam = excluded.naam,
                     gemeente_slug = excluded.gemeente_slug,
                     actief = excluded.actief""",
                (g["naam"], g["email"], g.get("gemeente", "amsterdam"),
                 1 if g.get("actief", True) else 0),
            )
            gebruiker_id = conn.execute(
                "SELECT id FROM gebruikers WHERE email = ?", (g["email"],)
            ).fetchone()["id"]

            conn.execute(
                "DELETE FROM gebruiker_onderwerpen WHERE gebruiker_id = ?", (gebruiker_id,)
            )
            for onderwerp in g.get("onderwerpen", []):
                conn.execute(
                    "INSERT OR IGNORE INTO gebruiker_onderwerpen (gebruiker_id, onderwerp) VALUES (?, ?)",
                    (gebruiker_id, onderwerp),
                )


def upsert_item(item: dict) -> tuple[bool, int]:
    """Insert or update a scraped item. Returns (is_new, row_id)."""
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM items WHERE extern_id = ? AND gemeente_slug = ?",
            (item["extern_id"], item.get("gemeente_slug", "amsterdam")),
        ).fetchone()

        if existing:
            conn.execute(
                """UPDATE items SET
                    titel = ?, indiener = ?, datum_ingediend = ?,
                    termijn_einde = ?, datum_afdoening = ?, uitslag = ?,
                    gekoppeld_evenement = ?, bron_url = ?,
                    laatste_check = datetime('now')
                WHERE id = ?""",
                (
                    item["titel"], item["indiener"], item["datum_ingediend"],
                    item["termijn_einde"], item["datum_afdoening"], item["uitslag"],
                    item["gekoppeld_evenement"], item["bron_url"], existing["id"],
                ),
            )
            return False, existing["id"]
        else:
            cursor = conn.execute(
                """INSERT INTO items
                    (extern_id, gemeente_slug, type, titel, indiener, datum_ingediend,
                     termijn_einde, datum_afdoening, uitslag, gekoppeld_evenement,
                     bron_url, laatste_check)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))""",
                (
                    item["extern_id"], item.get("gemeente_slug", "amsterdam"),
                    item["type"], item["titel"], item["indiener"],
                    item["datum_ingediend"], item["termijn_einde"],
                    item["datum_afdoening"], item["uitslag"],
                    item["gekoppeld_evenement"], item["bron_url"],
                ),
            )
            return True, cursor.lastrowid


def get_active_gebruikers() -> list[sqlite3.Row]:
    with get_connection() as conn:
        return conn.execute(
            "SELECT * FROM gebruikers WHERE actief = 1"
        ).fetchall()


def get_onderwerpen(gebruiker_id: int) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT onderwerp FROM gebruiker_onderwerpen WHERE gebruiker_id = ?",
            (gebruiker_id,),
        ).fetchall()
        return [r["onderwerp"] for r in rows]


def get_unmatched_items(gebruiker_id: int, gemeente_slug: str) -> list[sqlite3.Row]:
    """Items not yet matched for this user."""
    with get_connection() as conn:
        return conn.execute(
            """SELECT i.* FROM items i
               WHERE i.gemeente_slug = ?
               AND i.id NOT IN (
                   SELECT item_id FROM gebruiker_items WHERE gebruiker_id = ?
               )""",
            (gemeente_slug, gebruiker_id),
        ).fetchall()


def upsert_gebruiker_item(gebruiker_id: int, item_id: int, score: int, tags: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """INSERT INTO gebruiker_items (gebruiker_id, item_id, relevantiescore, relevantie_tags)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(gebruiker_id, item_id) DO UPDATE SET
                 relevantiescore = excluded.relevantiescore,
                 relevantie_tags = excluded.relevantie_tags""",
            (gebruiker_id, item_id, score, tags),
        )


def update_afdoening(gebruiker_id: int, item_id: int, status: str, notitie: str) -> None:
    with get_connection() as conn:
        conn.execute(
            """UPDATE gebruiker_items SET status = ?, afdoening_notitie = ?
               WHERE gebruiker_id = ? AND item_id = ?""",
            (status, notitie, gebruiker_id, item_id),
        )


def get_dashboard_buckets(gebruiker_id: int, min_score: int = 6) -> dict:
    today = date.today().isoformat()
    week_from_now = (date.today() + timedelta(days=7)).isoformat()

    with get_connection() as conn:
        base = """
            SELECT i.*, gi.relevantiescore, gi.relevantie_tags,
                   gi.status as gi_status, gi.afdoening_notitie
            FROM items i
            JOIN gebruiker_items gi ON gi.item_id = i.id
            WHERE gi.gebruiker_id = ?
              AND gi.relevantiescore >= ?
              AND gi.status = 'open'
        """
        nadert = conn.execute(
            base + "AND i.termijn_einde BETWEEN ? AND ? ORDER BY i.termijn_einde ASC",
            (gebruiker_id, min_score, today, week_from_now),
        ).fetchall()

        verstreken = conn.execute(
            base + "AND i.termijn_einde < ? AND i.termijn_einde >= date(?, '-14 days') ORDER BY i.termijn_einde DESC",
            (gebruiker_id, min_score, today, today),
        ).fetchall()

        ruim_over = conn.execute(
            base + "AND i.termijn_einde < date(?, '-14 days') ORDER BY i.termijn_einde DESC",
            (gebruiker_id, min_score, today),
        ).fetchall()

        nieuw = conn.execute(
            """SELECT i.*, gi.relevantiescore, gi.relevantie_tags,
                      gi.status as gi_status, gi.afdoening_notitie
               FROM items i
               JOIN gebruiker_items gi ON gi.item_id = i.id
               WHERE gi.gebruiker_id = ?
                 AND gi.relevantiescore >= ?
                 AND gi.aangemaakt >= date('now', '-1 day')
               ORDER BY gi.aangemaakt DESC
               LIMIT 3""",
            (gebruiker_id, min_score),
        ).fetchall()

    return {"nadert": nadert, "verstreken": verstreken, "ruim_over": ruim_over, "nieuw": nieuw}


# ── Web app queries ──────────────────────────────────────────────────────────

def rebuild_fts() -> None:
    """Rebuild the FTS index from scratch."""
    with get_connection() as conn:
        conn.execute("INSERT INTO items_fts(items_fts) VALUES('rebuild')")


def _fts_query(q: str) -> str:
    """Zet zoekterm om naar FTS5 query met prefix-matching per woord."""
    woorden = [w.strip() for w in q.split() if w.strip()]
    return " ".join(f'"{w}"*' for w in woorden)


def zoek_items(
    q: str = "",
    type_filter: str = "",
    gemeente_slug: str = "amsterdam",
    page: int = 1,
    per_page: int = 30,
) -> dict:
    offset = (page - 1) * per_page
    with get_connection() as conn:
        if q:
            fts_q = _fts_query(q)
            rows = conn.execute(
                """SELECT i.* FROM items i
                   JOIN items_fts f ON i.id = f.rowid
                   WHERE items_fts MATCH ?
                   AND i.gemeente_slug = ?
                   AND (? = '' OR i.type = ?)
                   ORDER BY rank
                   LIMIT ? OFFSET ?""",
                (fts_q, gemeente_slug, type_filter, type_filter, per_page, offset),
            ).fetchall()
            totaal = conn.execute(
                """SELECT COUNT(*) FROM items i
                   JOIN items_fts f ON i.id = f.rowid
                   WHERE items_fts MATCH ? AND i.gemeente_slug = ?
                   AND (? = '' OR i.type = ?)""",
                (fts_q, gemeente_slug, type_filter, type_filter),
            ).fetchone()[0]
        else:
            rows = conn.execute(
                """SELECT * FROM items
                   WHERE gemeente_slug = ?
                   AND (? = '' OR type = ?)
                   ORDER BY datum_ingediend DESC, aangemaakt DESC
                   LIMIT ? OFFSET ?""",
                (gemeente_slug, type_filter, type_filter, per_page, offset),
            ).fetchall()
            totaal = conn.execute(
                "SELECT COUNT(*) FROM items WHERE gemeente_slug = ? AND (? = '' OR type = ?)",
                (gemeente_slug, type_filter, type_filter),
            ).fetchone()[0]

    return {
        "items": [dict(r) for r in rows],
        "totaal": totaal,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (totaal + per_page - 1) // per_page),
    }


def zoek_voor_briefing(termen: list[str], gemeente_slug: str = "amsterdam", limit: int = 20) -> list[dict]:
    """FTS search: eerst AND (precies), daarna OR (breed), resultaten gecombineerd."""
    if not termen:
        return []
    schone = [t.strip() for t in termen if t.strip()]
    resultaten: list[dict] = []
    gezien: set[int] = set()

    with get_connection() as conn:
        def zoek(query: str) -> list[sqlite3.Row]:
            try:
                return conn.execute(
                    """SELECT i.* FROM items i
                       JOIN items_fts f ON i.id = f.rowid
                       WHERE items_fts MATCH ? AND i.gemeente_slug = ?
                       ORDER BY rank LIMIT ?""",
                    (query, gemeente_slug, limit),
                ).fetchall()
            except Exception:
                return []

        # Strategie 1: alle termen moeten voorkomen (AND)
        if len(schone) > 1:
            and_query = " ".join(f'"{t}"*' for t in schone)
            for r in zoek(and_query):
                if r["id"] not in gezien:
                    resultaten.append(dict(r))
                    gezien.add(r["id"])

        # Strategie 2: minstens één term (OR) — vult aan tot limit
        or_query = " OR ".join(f'"{t}"*' for t in schone)
        for r in zoek(or_query):
            if r["id"] not in gezien:
                resultaten.append(dict(r))
                gezien.add(r["id"])
            if len(resultaten) >= limit:
                break

    return resultaten[:limit]


def get_recent_items(gemeente_slug: str = "amsterdam", limit: int = 10) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM items WHERE gemeente_slug = ?
               ORDER BY datum_ingediend DESC, aangemaakt DESC LIMIT ?""",
            (gemeente_slug, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_item(item_id: int) -> dict | None:
    with get_connection() as conn:
        row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return dict(row) if row else None


def get_stats(gemeente_slug: str = "amsterdam") -> dict:
    with get_connection() as conn:
        totaal = conn.execute(
            "SELECT COUNT(*) FROM items WHERE gemeente_slug = ?", (gemeente_slug,)
        ).fetchone()[0]
        per_type = conn.execute(
            "SELECT type, COUNT(*) as n FROM items WHERE gemeente_slug = ? GROUP BY type",
            (gemeente_slug,),
        ).fetchall()
        laatste = conn.execute(
            "SELECT MAX(aangemaakt) FROM items WHERE gemeente_slug = ?", (gemeente_slug,)
        ).fetchone()[0]
    return {"totaal": totaal, "per_type": {r["type"]: r["n"] for r in per_type}, "laatste_update": laatste}
