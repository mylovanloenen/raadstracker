import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DB_PATH = Path(os.environ.get("DB_PATH", Path(__file__).parent / "tracker.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS toezeggingen (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extern_id TEXT UNIQUE NOT NULL,
    nummer TEXT,
    bron TEXT NOT NULL DEFAULT 'tweedekamer',
    tekst TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Openstaand',
    naam TEXT,
    functie TEXT,
    ministerie TEXT,
    activiteit_nummer TEXT,
    aanmaakdatum TEXT,
    datum_nakoming TEXT,
    aangemaakt TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_toezeggingen_status ON toezeggingen(status);
CREATE INDEX IF NOT EXISTS idx_toezeggingen_ministerie ON toezeggingen(ministerie);

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
    gemeente_slug: str = "",
    page: int = 1,
    per_page: int = 30,
) -> dict:
    """Zoek items. gemeente_slug='' = alle bronnen."""
    offset = (page - 1) * per_page
    with get_connection() as conn:
        if q:
            fts_q = _fts_query(q)
            rows = conn.execute(
                """SELECT i.* FROM items i
                   JOIN items_fts f ON i.id = f.rowid
                   WHERE items_fts MATCH ?
                   AND (? = '' OR i.gemeente_slug = ?)
                   AND (? = '' OR i.type = ?)
                   ORDER BY rank
                   LIMIT ? OFFSET ?""",
                (fts_q, gemeente_slug, gemeente_slug, type_filter, type_filter, per_page, offset),
            ).fetchall()
            totaal = conn.execute(
                """SELECT COUNT(*) FROM items i
                   JOIN items_fts f ON i.id = f.rowid
                   WHERE items_fts MATCH ?
                   AND (? = '' OR i.gemeente_slug = ?)
                   AND (? = '' OR i.type = ?)""",
                (fts_q, gemeente_slug, gemeente_slug, type_filter, type_filter),
            ).fetchone()[0]
        else:
            rows = conn.execute(
                """SELECT * FROM items
                   WHERE (? = '' OR gemeente_slug = ?)
                   AND (? = '' OR type = ?)
                   ORDER BY datum_ingediend DESC, aangemaakt DESC
                   LIMIT ? OFFSET ?""",
                (gemeente_slug, gemeente_slug, type_filter, type_filter, per_page, offset),
            ).fetchall()
            totaal = conn.execute(
                "SELECT COUNT(*) FROM items WHERE (? = '' OR gemeente_slug = ?) AND (? = '' OR type = ?)",
                (gemeente_slug, gemeente_slug, type_filter, type_filter),
            ).fetchone()[0]

    return {
        "items": [dict(r) for r in rows],
        "totaal": totaal,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (totaal + per_page - 1) // per_page),
    }


def zoek_voor_briefing(termen: list[str], gemeente_slug: str = "", limit: int = 20) -> list[dict]:
    """FTS search: eerst AND (precies), daarna OR (breed). gemeente_slug='' = alle bronnen."""
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
                       WHERE items_fts MATCH ?
                       AND (? = '' OR i.gemeente_slug = ?)
                       ORDER BY rank LIMIT ?""",
                    (query, gemeente_slug, gemeente_slug, limit),
                ).fetchall()
            except Exception:
                return []

        if len(schone) > 1:
            and_query = " ".join(f'"{t}"*' for t in schone)
            for r in zoek(and_query):
                if r["id"] not in gezien:
                    resultaten.append(dict(r))
                    gezien.add(r["id"])

        or_query = " OR ".join(f'"{t}"*' for t in schone)
        for r in zoek(or_query):
            if r["id"] not in gezien:
                resultaten.append(dict(r))
                gezien.add(r["id"])
            if len(resultaten) >= limit:
                break

    return resultaten[:limit]


def get_nieuw_vandaag(uren: int = 24) -> dict:
    """Items en toezeggingen toegevoegd in de afgelopen N uur."""
    with get_connection() as conn:
        items_ams = conn.execute(
            """SELECT * FROM items WHERE gemeente_slug = 'amsterdam'
               AND aangemaakt >= datetime('now', ?) ORDER BY aangemaakt DESC LIMIT 20""",
            (f"-{uren} hours",),
        ).fetchall()
        items_tk = conn.execute(
            """SELECT * FROM items WHERE gemeente_slug = 'tweedekamer'
               AND aangemaakt >= datetime('now', ?) ORDER BY aangemaakt DESC LIMIT 20""",
            (f"-{uren} hours",),
        ).fetchall()
        items_agv = conn.execute(
            """SELECT * FROM items WHERE gemeente_slug = 'waterschap_agv'
               AND aangemaakt >= datetime('now', ?) ORDER BY aangemaakt DESC LIMIT 10""",
            (f"-{uren} hours",),
        ).fetchall()
        tz_nieuw = conn.execute(
            """SELECT * FROM toezeggingen WHERE status = 'Openstaand'
               AND aangemaakt >= datetime('now', ?) ORDER BY aangemaakt DESC LIMIT 10""",
            (f"-{uren} hours",),
        ).fetchall()
        tz_over = conn.execute(
            """SELECT * FROM toezeggingen WHERE status = 'Openstaand'
               AND datum_nakoming IS NOT NULL AND datum_nakoming < date('now')
               ORDER BY datum_nakoming ASC LIMIT 10""",
        ).fetchall()
    return {
        "amsterdam": [dict(r) for r in items_ams],
        "tweedekamer": [dict(r) for r in items_tk],
        "agv": [dict(r) for r in items_agv],
        "toezeggingen_nieuw": [dict(r) for r in tz_nieuw],
        "toezeggingen_over": [dict(r) for r in tz_over],
    }


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


def upsert_toezegging(t: dict) -> bool:
    """Insert or update een toezegging. Geeft True terug als nieuw."""
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM toezeggingen WHERE extern_id = ?", (t["extern_id"],)
        ).fetchone()
        if existing:
            conn.execute(
                """UPDATE toezeggingen SET status=?, datum_nakoming=?, tekst=?
                   WHERE extern_id=?""",
                (t["status"], t.get("datum_nakoming"), t["tekst"], t["extern_id"]),
            )
            return False
        conn.execute(
            """INSERT INTO toezeggingen
               (extern_id, nummer, bron, tekst, status, naam, functie, ministerie,
                activiteit_nummer, aanmaakdatum, datum_nakoming)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                t["extern_id"], t.get("nummer"), t.get("bron", "tweedekamer"),
                t["tekst"], t["status"], t.get("naam"), t.get("functie"),
                t.get("ministerie"), t.get("activiteit_nummer"),
                t.get("aanmaakdatum"), t.get("datum_nakoming"),
            ),
        )
        return True


def get_toezeggingen(
    status: str = "Openstaand",
    ministerie: str = "",
    q: str = "",
    page: int = 1,
    per_page: int = 40,
) -> dict:
    offset = (page - 1) * per_page
    with get_connection() as conn:
        filters = ["(? = '' OR status = ?)", "(? = '' OR ministerie LIKE ?)"]
        params_base = [status, status, ministerie, f"%{ministerie}%"]

        if q:
            filters.append("tekst LIKE ?")
            params_base.append(f"%{q}%")

        where = " AND ".join(filters)
        rows = conn.execute(
            f"SELECT * FROM toezeggingen WHERE {where} ORDER BY aanmaakdatum DESC LIMIT ? OFFSET ?",
            params_base + [per_page, offset],
        ).fetchall()
        totaal = conn.execute(
            f"SELECT COUNT(*) FROM toezeggingen WHERE {where}",
            params_base,
        ).fetchone()[0]

        ministeries = conn.execute(
            "SELECT DISTINCT ministerie FROM toezeggingen WHERE ministerie IS NOT NULL ORDER BY ministerie"
        ).fetchall()

    return {
        "items": [dict(r) for r in rows],
        "totaal": totaal,
        "page": page,
        "per_page": per_page,
        "pages": max(1, (totaal + per_page - 1) // per_page),
        "ministeries": [r["ministerie"] for r in ministeries],
    }


def get_toezegging_stats() -> dict:
    with get_connection() as conn:
        per_status = conn.execute(
            "SELECT status, COUNT(*) as n FROM toezeggingen GROUP BY status"
        ).fetchall()
        top_ministeries = conn.execute(
            """SELECT ministerie, COUNT(*) as n FROM toezeggingen
               WHERE status = 'Openstaand' AND ministerie IS NOT NULL
               GROUP BY ministerie ORDER BY n DESC LIMIT 10"""
        ).fetchall()
    return {
        "per_status": {r["status"]: r["n"] for r in per_status},
        "top_ministeries": [(r["ministerie"], r["n"]) for r in top_ministeries],
    }


def get_stats(gemeente_slug: str = "") -> dict:
    """Stats voor één bron of alle bronnen (gemeente_slug='')."""
    with get_connection() as conn:
        totaal = conn.execute(
            "SELECT COUNT(*) FROM items WHERE (? = '' OR gemeente_slug = ?)",
            (gemeente_slug, gemeente_slug),
        ).fetchone()[0]
        per_type = conn.execute(
            "SELECT type, COUNT(*) as n FROM items WHERE (? = '' OR gemeente_slug = ?) GROUP BY type",
            (gemeente_slug, gemeente_slug),
        ).fetchall()
        laatste = conn.execute(
            "SELECT MAX(aangemaakt) FROM items WHERE (? = '' OR gemeente_slug = ?)",
            (gemeente_slug, gemeente_slug),
        ).fetchone()[0]
        per_bron = conn.execute(
            "SELECT g.naam, COUNT(i.id) as n FROM items i JOIN gemeenten g ON g.slug = i.gemeente_slug GROUP BY i.gemeente_slug",
        ).fetchall()
    return {
        "totaal": totaal,
        "per_type": {r["type"]: r["n"] for r in per_type},
        "laatste_update": laatste,
        "per_bron": {r["naam"]: r["n"] for r in per_bron},
    }


# ── Media items ───────────────────────────────────────────────────────────────

MEDIA_SCHEMA = """
CREATE TABLE IF NOT EXISTS media_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extern_id TEXT UNIQUE NOT NULL,
    bron TEXT NOT NULL,
    titel TEXT NOT NULL,
    url TEXT,
    samenvatting TEXT,
    datum TEXT,
    query_tag TEXT,
    aangemaakt TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_media_datum ON media_items(datum);
CREATE INDEX IF NOT EXISTS idx_media_bron ON media_items(bron);
"""


def init_media_db() -> None:
    with get_connection() as conn:
        conn.executescript(MEDIA_SCHEMA)


def upsert_media_item(item: dict) -> bool:
    init_media_db()
    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM media_items WHERE extern_id = ?", (item["extern_id"],)
        ).fetchone()
        if existing:
            return False
        conn.execute(
            """INSERT INTO media_items (extern_id, bron, titel, url, samenvatting, datum, query_tag)
               VALUES (?,?,?,?,?,?,?)""",
            (item["extern_id"], item["bron"], item["titel"], item.get("url"),
             item.get("samenvatting"), item.get("datum"), item.get("query_tag")),
        )
        return True


def get_media_items(q: str = "", bron: str = "", limit: int = 30, dagen: int = 7) -> list[dict]:
    init_media_db()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM media_items
               WHERE (? = '' OR bron = ?)
               AND (? = '' OR titel LIKE ? OR samenvatting LIKE ?)
               AND datum >= date('now', ?)
               ORDER BY datum DESC LIMIT ?""",
            (bron, bron, q, f"%{q}%", f"%{q}%", f"-{dagen} days", limit),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_media(uren: int = 24) -> list[dict]:
    init_media_db()
    with get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM media_items
               WHERE aangemaakt >= datetime('now', ?)
               ORDER BY datum DESC LIMIT 20""",
            (f"-{uren} hours",),
        ).fetchall()
    return [dict(r) for r in rows]

# ── Statistieken ─────────────────────────────────────────────────────────────

def get_statistieken() -> dict:
    """Statistieken voor het dashboard."""
    with get_connection() as conn:
        # Items per maand (laatste 24 maanden), Amsterdam
        per_maand = conn.execute(
            """SELECT strftime('%Y-%m', datum_ingediend) as maand, COUNT(*) as n
               FROM items WHERE gemeente_slug = 'amsterdam'
               AND datum_ingediend IS NOT NULL
               AND datum_ingediend >= date('now', '-24 months')
               GROUP BY maand ORDER BY maand ASC"""
        ).fetchall()

        # Items per type, Amsterdam
        per_type = conn.execute(
            """SELECT type, COUNT(*) as n FROM items
               WHERE gemeente_slug = 'amsterdam'
               GROUP BY type ORDER BY n DESC"""
        ).fetchall()

        # Top fracties (enkelvoudig ingediend), Amsterdam
        top_fracties = conn.execute(
            """SELECT indiener, COUNT(*) as n FROM items
               WHERE gemeente_slug = 'amsterdam'
               AND indiener IS NOT NULL AND indiener != ''
               AND indiener NOT LIKE '%,%'
               GROUP BY indiener ORDER BY n DESC LIMIT 12"""
        ).fetchall()

        # Aangenomen vs verworpen, Amsterdam moties
        uitslag_stats = conn.execute(
            """SELECT
               SUM(CASE WHEN lower(uitslag) LIKE '%aangenomen%' THEN 1 ELSE 0 END) as aangenomen,
               SUM(CASE WHEN lower(uitslag) LIKE '%verworpen%' THEN 1 ELSE 0 END) as verworpen,
               SUM(CASE WHEN uitslag IS NOT NULL AND uitslag != '' THEN 1 ELSE 0 END) as totaal_met_uitslag
               FROM items WHERE gemeente_slug = 'amsterdam' AND type = 'motie'"""
        ).fetchone()

        # Totalen per bron
        totalen = conn.execute(
            """SELECT gemeente_slug, COUNT(*) as n FROM items GROUP BY gemeente_slug ORDER BY n DESC"""
        ).fetchall()

    return {
        "per_maand": [(r["maand"], r["n"]) for r in per_maand],
        "per_type": [(r["type"], r["n"]) for r in per_type],
        "top_fracties": [(r["indiener"], r["n"]) for r in top_fracties],
        "uitslag": dict(uitslag_stats) if uitslag_stats else {},
        "totalen": [(r["gemeente_slug"], r["n"]) for r in totalen],
    }
