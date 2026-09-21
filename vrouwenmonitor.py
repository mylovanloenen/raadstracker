"""
Amsterdamse Vrouwenmonitor — dagelijkse monitor voor Nora Ait Boubker (D66 Amsterdam).

Stappen:
  1. Nieuwe publicaties verzamelen (raad/commissies via Notubiz, bekendmakingen via
     overheid.nl SRU, Onderzoek & Statistiek, lokale media uit media_items).
  2. Elk gemeentestuk toetsen op de positie van vrouwen (Claude, gestructureerde output).
  3. Mediaregister: artikelen waarin vrouwen/meisjes voorkomen, met duiding.
  4. Concept-raadsvragen (max 5), met check op eerdere schriftelijke vragen.
  5. E-mail met bronverantwoording (welke bronnen gecontroleerd, welke niet toegankelijk).

Gebruik:
  python3 vrouwenmonitor.py            # volledige run + mail
  python3 vrouwenmonitor.py --droog    # geen mail, alleen HTML naar vrouwenmonitor_preview.html
"""

import io
import json
import logging
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from pathlib import Path

import anthropic
import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)
import database as db

logger = logging.getLogger(__name__)

ORG_ID = 281
API_BASE = "https://api.notubiz.nl"
HEADERS = {"Accept": "application/json", "User-Agent": "Raadstracker/1.0 (vrouwenmonitor)"}
MODEL = "claude-sonnet-4-6"

ONTVANGERS = [
    e.strip() for e in os.environ.get(
        "VROUWENMONITOR_TO", "Nora.aitboubker@gmail.com,ricardo@equals.nl"
    ).split(",") if e.strip()
]

NOTUBIZ_MODULES = {
    1: ("Ingekomen stuk / raadsbrief", "Berichten%20uit%20het%20college"),
    4: ("Schriftelijke vragen", "Schriftelijke%20vragen"),
    5: ("Collegebericht", "Overige%20dagelijkse%20berichten"),
    6: ("Motie / amendement", "Moties%20en%20amendementen"),
}

# Publicatietypes op officielebekendmakingen.nl die we overslaan (vergunningen e.d.)
BEKENDMAKING_SKIP = re.compile(r"vergunning|verkeersbesluit|productbeschrijving|mededeling", re.I)

VROUWEN_RE = re.compile(
    r"\b(vrouw\w*|meisje\w*|dames|moeder\w*|dochter\w*|emancipat\w*|gender\w*|sekse|"
    r"feminis\w*|zwanger\w*|menstruat\w*|femicide|straatintimidatie|vrouwelijke?|"
    r"huiselijk geweld|seksueel geweld|seksuele intimidatie|loonkloof|kinderopvang)\b",
    re.I,
)

CLASSIFICATIES = [
    "expliciet meegewogen",
    "gedeeltelijk meegewogen",
    "mogelijk relevant maar niet aantoonbaar meegewogen",
    "geen duidelijke relevantie",
    "onvoldoende informatie",
]

NL_MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
              "augustus", "september", "oktober", "november", "december"]


def nl_datum(d: date) -> str:
    return f"{d.day} {NL_MAANDEN[d.month - 1]} {d.year}"


# ── Database ─────────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS vm_stukken (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    extern_id TEXT UNIQUE NOT NULL,
    bron TEXT NOT NULL,
    type TEXT,
    titel TEXT NOT NULL,
    url TEXT,
    doc_url TEXT,
    datum TEXT,
    afzender TEXT,
    tekst TEXT,
    tekst_status TEXT,
    analyse_json TEXT,
    aangemaakt TEXT DEFAULT (datetime('now'))
);
CREATE TABLE IF NOT EXISTS vm_rapporten (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    datum TEXT NOT NULL,
    onderwerp TEXT,
    body_html TEXT NOT NULL,
    data_json TEXT,
    verstuurd_naar TEXT,
    aangemaakt TEXT DEFAULT (datetime('now'))
);
"""


def init_vm_db() -> None:
    with db.get_connection() as conn:
        conn.executescript(SCHEMA)


def is_bekend(extern_id: str) -> bool:
    with db.get_connection() as conn:
        return conn.execute("SELECT 1 FROM vm_stukken WHERE extern_id = ?", (extern_id,)).fetchone() is not None


def bewaar_stuk(stuk: dict) -> None:
    with db.get_connection() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO vm_stukken
               (extern_id, bron, type, titel, url, doc_url, datum, afzender, tekst, tekst_status, analyse_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (stuk["extern_id"], stuk["bron"], stuk.get("type"), stuk["titel"], stuk.get("url"),
             stuk.get("doc_url"), stuk.get("datum"), stuk.get("afzender"), stuk.get("tekst"),
             stuk.get("tekst_status"), json.dumps(stuk.get("analyse"), ensure_ascii=False)),
        )


def get_rapporten(limit: int = 30) -> list[dict]:
    init_vm_db()
    with db.get_connection() as conn:
        rows = conn.execute(
            "SELECT id, datum, onderwerp, verstuurd_naar, data_json, aangemaakt FROM vm_rapporten ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_rapport(rapport_id: int) -> dict | None:
    init_vm_db()
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM vm_rapporten WHERE id = ?", (rapport_id,)).fetchone()
    return dict(row) if row else None


# ── Stap 1: bronnen verzamelen ───────────────────────────────────────────────

class Bronlog:
    """Houdt bij welke bronnen gecontroleerd zijn en welke niet toegankelijk waren."""

    def __init__(self):
        self.regels: list[dict] = []

    def ok(self, naam: str, detail: str):
        self.regels.append({"naam": naam, "status": "gecontroleerd", "detail": detail})

    def beperkt(self, naam: str, detail: str):
        self.regels.append({"naam": naam, "status": "beperkt", "detail": detail})

    def fout(self, naam: str, detail: str):
        self.regels.append({"naam": naam, "status": "niet toegankelijk", "detail": detail})


def _attribs(api_item: dict) -> dict:
    result = {}
    attrs = api_item.get("attributes", {}).get("attribute", [])
    if isinstance(attrs, dict):
        attrs = [attrs]
    for a in attrs:
        result[a.get("label", "")] = a.get("value") if a.get("value") is not None else a.get("values")
    return result


def _vind_doc_url(attrs: dict) -> str | None:
    for v in attrs.values():
        if isinstance(v, dict) and "notubiz.nl/document" in str(v.get("url", "")):
            return v["url"]
        if isinstance(v, dict) and isinstance(v.get("value"), list):
            for x in v["value"]:
                if isinstance(x, dict) and "notubiz.nl/document" in str(x.get("url", "")):
                    return x["url"]
    return None


def _plat(v) -> str | None:
    """Maakt van Notubiz-waarden ({'value': [...]}, lijsten, dicts) een leesbare string."""
    if v is None:
        return None
    if isinstance(v, dict):
        v = v.get("value", v.get("title", v))
    if isinstance(v, list):
        return ", ".join(str(_plat(x)) for x in v if x is not None) or None
    if isinstance(v, dict):
        return str(v.get("title") or v.get("name") or v)
    return str(v)


def _strip_html(s: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", s or "").split())


def haal_pdf_tekst(url: str, max_chars: int = 7000, max_pages: int = 8) -> tuple[str, str, str]:
    """Geeft (tekst, status, paginatitel). Status: 'ok', 'ok (html)', 'geen tekst (gescand document?)', 'niet opgehaald: ...'."""
    try:
        import pypdf
        r = requests.get(url, timeout=40, headers={"User-Agent": "Mozilla/5.0 (Raadstracker)"})
        r.raise_for_status()
        if r.content[:4] != b"%PDF":
            m = re.search(r"<h1[^>]*>(.*?)</h1>|<title>(.*?)</title>", r.text, re.S | re.I)
            titel = _strip_html(m.group(1) or m.group(2)) if m else ""
            return _strip_html(r.text)[:max_chars], "ok (html)", titel
        reader = pypdf.PdfReader(io.BytesIO(r.content))
        tekst = " ".join(" ".join((p.extract_text() or "") for p in reader.pages[:max_pages]).split())
        if len(tekst) < 80:
            return "", "geen tekst (gescand document?)", ""
        return tekst[:max_chars], "ok", ""
    except Exception as e:
        return "", f"niet opgehaald: {type(e).__name__}", ""


def verzamel_notubiz(dagen: int, log: Bronlog) -> list[dict]:
    """Nieuwe stukken uit Raadsinformatie Amsterdam (Notubiz) van de afgelopen N dagen."""
    vanaf = (date.today() - timedelta(days=dagen)).isoformat()
    stukken, fouten = [], []
    for module_id, (label, detail_naam) in NOTUBIZ_MODULES.items():
        url = f"{API_BASE}/organisations/{ORG_ID}/modules/{module_id}/items"
        try:
            tot = requests.get(url, params={"limit": 1, "format": "json"}, headers=HEADERS, timeout=20)\
                .json()["pagination"]["@attributes"]["total_results"]
            resp = requests.get(url, params={"limit": 80, "start": max(0, tot - 80), "format": "json"},
                                headers=HEADERS, timeout=30)
            items = resp.json().get("item", [])
            if isinstance(items, dict):
                items = [items]
        except Exception as e:
            fouten.append(f"{label}: {type(e).__name__}")
            continue

        for it in items:
            item_id = it.get("@attributes", {}).get("id")
            attrs = _attribs(it)
            titel = attrs.get("Titel") or attrs.get("Schriftelijke vraag") or attrs.get("Onderwerp")
            if isinstance(titel, dict):
                titel = titel.get("title")
            datum_val = attrs.get("Datum") or attrs.get("Datum indiening") or attrs.get("Datum vraag") \
                or attrs.get("Aanmaakdatum") or attrs.get("N.V.T.")
            datum = str(datum_val)[:10] if datum_val else None
            if not item_id or not titel or not datum or datum < vanaf:
                continue
            afzender = _plat(attrs.get("Afzender") or attrs.get("Fractie") or attrs.get("Indiener(s)"))
            stukken.append({
                "extern_id": f"notubiz-{module_id}-{item_id}",
                "bron": "Raadsinformatie Amsterdam",
                "type": label,
                "titel": str(titel)[:400],
                "url": f"https://amsterdam.raadsinformatie.nl/modules/{module_id}/{detail_naam}/{item_id}",
                "doc_url": _vind_doc_url(attrs),
                "datum": datum,
                "afzender": str(afzender)[:200] if afzender else None,
                "toelichting": _strip_html(str(attrs.get("Toelichting") or ""))[:1500],
            })
        time.sleep(0.4)

    if fouten:
        log.beperkt("Raadsinformatie Amsterdam (Notubiz API)", "; ".join(fouten))
    else:
        log.ok("Raadsinformatie Amsterdam (Notubiz API)",
               f"moties, schriftelijke vragen, raadsbrieven/ingekomen stukken en collegeberichten; "
               f"{len(stukken)} stukken sinds {vanaf}")
    return stukken


def verzamel_bekendmakingen(dagen: int, log: Bronlog) -> list[dict]:
    """Officiële bekendmakingen van de gemeente Amsterdam via de SRU-koppeling van overheid.nl (vergunningen uitgesloten)."""
    vanaf = (date.today() - timedelta(days=dagen)).isoformat()
    query = f'(dt.creator=="Amsterdam" and dt.modified>={vanaf})'
    stukken = []
    try:
        r = requests.get("https://repository.overheid.nl/sru",
                         params={"operation": "searchRetrieve", "version": "2.0",
                                 "query": query, "maximumRecords": 200}, timeout=40)
        r.raise_for_status()
        root = ET.fromstring(r.content)
        ns = {"dcterms": "http://purl.org/dc/terms/"}
        totaal = 0
        for rec in root.iter("{http://docs.oasis-open.org/ns/search-ws/sruResponse}record"):
            totaal += 1
            ident = rec.findtext(".//dcterms:identifier", namespaces=ns)
            titel = rec.findtext(".//dcterms:title", namespaces=ns)
            typ = rec.findtext(".//dcterms:type", namespaces=ns) or ""
            mod = rec.findtext(".//dcterms:modified", namespaces=ns)
            if not ident or not titel or BEKENDMAKING_SKIP.search(typ) or BEKENDMAKING_SKIP.search(titel):
                continue
            html_url = next((u.text for u in rec.iter("{http://standaarden.overheid.nl/sru}itemUrl")
                             if (u.text or "").endswith(".html")), None)
            stukken.append({
                "extern_id": f"bekendmaking-{ident}",
                "bron": "Officiële bekendmakingen",
                "type": typ or "bekendmaking",
                "titel": titel[:400],
                "url": html_url or f"https://zoek.officielebekendmakingen.nl/{ident}.html",
                "doc_url": html_url,
                "datum": mod,
                "afzender": "Gemeente Amsterdam",
                "toelichting": "",
            })
        log.ok("Officiële bekendmakingen (overheid.nl SRU)",
               f"{totaal} publicaties sinds {vanaf}, {len(stukken)} na uitsluiting van vergunningen en verkeersbesluiten")
    except Exception as e:
        log.fout("Officiële bekendmakingen (overheid.nl SRU)", f"{type(e).__name__}")
    return stukken


def verzamel_os(log: Bronlog) -> list[dict]:
    """Onderzoek & Statistiek: artikelen/publicaties op de homepage (er is geen feed)."""
    stukken = []
    try:
        r = requests.get("https://onderzoek.amsterdam.nl/", timeout=25,
                         headers={"User-Agent": "Mozilla/5.0 (Raadstracker)"})
        r.raise_for_status()
        links = sorted(set(re.findall(r'href="(/(?:artikel|publicatie)/[a-z0-9-]+)"', r.text)))
        for pad in links:
            slug = pad.rsplit("/", 1)[-1]
            if slug in {"over-onderzoek-en-statistiek", "termen-en-categorieen", "toegankelijkheidsverklaring",
                        "veelgestelde-vragen", "privacyverklaring", "contact", "cookies"}:
                continue
            stukken.append({
                "extern_id": f"os-{slug}",
                "bron": "Onderzoek & Statistiek",
                "type": "onderzoek",
                "titel": slug.replace("-", " ").capitalize(),
                "url": f"https://onderzoek.amsterdam.nl{pad}",
                "doc_url": f"https://onderzoek.amsterdam.nl{pad}",
                "datum": date.today().isoformat(),
                "afzender": "Onderzoek & Statistiek",
                "toelichting": "",
                "titel_uit_url": True,
            })
        log.beperkt("Onderzoek & Statistiek (onderzoek.amsterdam.nl)",
                    f"geen feed; {len(links)} artikellinks op de homepage gecontroleerd, alleen nieuwe worden geanalyseerd")
    except Exception as e:
        log.fout("Onderzoek & Statistiek (onderzoek.amsterdam.nl)", f"{type(e).__name__}")
    return stukken


def controleer_overige_bronnen(log: Bronlog) -> None:
    """Bronnen die (nog) niet automatisch uit te lezen zijn — expliciet vermelden in de mail."""
    checks = [
        ("Collegebesluiten en beleidsstukken (amsterdam.nl / Open Amsterdam)",
         "https://www.amsterdam.nl/bestuur-organisatie/college/besluitenlijsten-college/",
         "concept-besluitenlijsten van het college komen wel binnen via Raadsinformatie (ingekomen stukken)"),
        ("Raadzaam (raadzaam.amsterdam.nl)", "https://raadzaam.amsterdam.nl/",
         "JavaScript-applicatie zonder open API; dezelfde stukken komen via Raadsinformatie binnen"),
    ]
    for naam, url, opm in checks:
        try:
            r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0 (Raadstracker)"})
            if r.status_code == 200:
                log.beperkt(naam, f"bereikbaar maar niet automatisch uitgelezen; {opm}")
            else:
                log.fout(naam, f"HTTP {r.status_code} (toegang geblokkeerd); {opm}")
        except Exception as e:
            log.fout(naam, f"{type(e).__name__}; {opm}")


def verzamel_media(log: Bronlog, uren: int = 48) -> list[dict]:
    """Nieuwe media-artikelen (Google News RSS via media_import) met een vermelding van vrouwen/meisjes."""
    db.init_media_db()
    with db.get_connection() as conn:
        rows = conn.execute(
            """SELECT * FROM media_items WHERE aangemaakt >= datetime('now', ?)
               ORDER BY datum DESC LIMIT 300""", (f"-{uren} hours",),
        ).fetchall()
    alle = [dict(r) for r in rows]
    match = [m for m in alle if VROUWEN_RE.search(f"{m['titel']} {m.get('samenvatting') or ''}")]
    bronnen = sorted({m["bron"] for m in alle})
    log.ok("Lokale media (Google News: AT5, Het Parool, NH Nieuws, stadsdeel- en buurtmedia; regionale media op Amsterdamse relevantie)",
           f"{len(alle)} nieuwe artikelen in {uren} uur van {len(bronnen)} media; {len(match)} met vermelding van vrouwen/meisjes. "
           "Alleen koppen en intro's zijn beoordeeld; artikelen achter een betaalmuur (o.a. Het Parool) zijn niet volledig gelezen.")
    return match


# ── Stap 2 t/m 4: analyse met Claude ─────────────────────────────────────────

def _claude() -> anthropic.Anthropic:
    return anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])


def _vraag(prompt: str, max_tokens: int) -> str:
    return _claude().messages.create(
        model=MODEL, max_tokens=max_tokens, messages=[{"role": "user", "content": prompt}],
    ).content[0].text


def _json_uit(tekst: str):
    tekst = re.sub(r"^```(?:json)?\s*|\s*```$", "", tekst.strip())
    try:
        return json.loads(tekst)
    except json.JSONDecodeError:
        m = re.search(r"[\[{].*[\]}]", tekst, re.S)
        return json.loads(m.group(0)) if m else []


def analyseer_stukken(stukken: list[dict]) -> list[dict]:
    """Toetst elk gemeentestuk op de positie van vrouwen. Voegt 'analyse' toe aan elk stuk."""
    for i in range(0, len(stukken), 8):
        batch = stukken[i:i + 8]
        blokken = []
        for n, s in enumerate(batch):
            inhoud = s.get("tekst") or s.get("toelichting") or "(geen documenttekst beschikbaar)"
            blokken.append(
                f"### STUK {n}\nBron: {s['bron']} — {s['type']}\nTitel: {s['titel']}\n"
                f"Afzender: {s.get('afzender') or '?'}\nDatum: {s.get('datum')}\n"
                f"Tekststatus: {s.get('tekst_status') or 'alleen titel/toelichting'}\n"
                f"Inhoud:\n{inhoud[:6000]}\n"
            )
        prompt = f"""Je bent beleidsanalist voor een Amsterdams raadslid. Toets elk onderstaand gemeentestuk op de positie van vrouwen.

Kijk niet alleen naar het woord "vrouwen", maar ook naar onderwerpen waarbij verschillende effecten voor vrouwen en mannen relevant kunnen zijn: veiligheid, wonen, werk, inkomen, onderwijs, zorg, mobiliteit, sport en ondernemerschap.

Beoordeel per stuk:
- vrouwen_expliciet: worden vrouwen/meisjes expliciet besproken? (ja/nee/deels)
- cijfers_uitgesplitst: zijn cijfers naar geslacht uitgesplitst? (ja/nee/nvt/onbekend)
- effecten_onderzocht: zijn mogelijke verschillende effecten onderzocht? (ja/nee/onbekend)
- betrokkenheid: waren vrouwen(organisaties) betrokken bij de voorbereiding? (ja/nee/onbekend)
- maatregelen_budget_doelen: zijn maatregelen, budget en meetbare doelen voor vrouwen opgenomen? (ja/nee/deels/nvt)
- classificatie: precies één van: {json.dumps(CLASSIFICATIES, ensure_ascii=False)}
- thema: kort thema (bv. veiligheid, wonen, zorg, sport, financiën, overig)
- toelichting: 2-3 zinnen, feitelijk, verwijs naar wat er wél/niet in het stuk staat
- informatievraag: één zin die aangeeft welke informatie ontbreekt, of leeg als niet van toepassing

BELANGRIJK: "niet genoemd in het document" betekent NIET automatisch "niet meegenomen bij de voorbereiding". Formuleer dat als informatievraag, niet als bewezen tekortkoming. Als de tekststatus aangeeft dat het document niet is opgehaald, gebruik dan "onvoldoende informatie" tenzij de titel/toelichting al duidelijk is. Gebruik "geen duidelijke relevantie" ruimhartig voor puur procedurele stukken.

Antwoord met UITSLUITEND een JSON-lijst met per stuk een object met de velden: stuk (nummer), classificatie, thema, vrouwen_expliciet, cijfers_uitgesplitst, effecten_onderzocht, betrokkenheid, maatregelen_budget_doelen, toelichting, informatievraag.

{chr(10).join(blokken)}"""
        try:
            for obj in _json_uit(_vraag(prompt, 4000)):
                idx = int(obj.get("stuk", -1))
                if 0 <= idx < len(batch):
                    if obj.get("classificatie") not in CLASSIFICATIES:
                        obj["classificatie"] = "onvoldoende informatie"
                    batch[idx]["analyse"] = obj
        except Exception as e:
            logger.error(f"Analyse batch mislukt: {e}")
        for s in batch:
            s.setdefault("analyse", {"classificatie": "onvoldoende informatie", "thema": "onbekend",
                                     "toelichting": "Automatische analyse niet gelukt.", "informatievraag": ""})
    return stukken


def analyseer_media(artikelen: list[dict]) -> list[dict]:
    """Duiding per artikel: inhoudelijk over de positie van vrouwen, of alleen een vermelding."""
    if not artikelen:
        return []
    artikelen = artikelen[:40]
    regels = [f"{n}. [{a['bron']}, {a.get('datum')}] {a['titel']}\n   {a.get('samenvatting') or ''}"
              for n, a in enumerate(artikelen)]
    prompt = f"""Hieronder staan nieuwsartikelen (kop + intro) waarin vrouwen of meisjes worden genoemd, als groep of als persoon.

Geef per artikel:
- samenvatting: één zin, feitelijk
- duiding: "inhoudelijk" (het artikel gaat inhoudelijk over de positie van vrouwen/meisjes) of "vermelding" (vrouw komt alleen voor als persoon, bv. ondernemer, sporter, bewoner, zonder dat de positie van vrouwen het onderwerp is)
- amsterdam: "ja" als het over Amsterdam gaat of Amsterdamse relevantie heeft, anders "nee"
- toelichting: één zin waarom

Antwoord UITSLUITEND met een JSON-lijst van objecten met velden: nr, samenvatting, duiding, amsterdam, toelichting.

{chr(10).join(regels)}"""
    try:
        for obj in _json_uit(_vraag(prompt, 3500)):
            idx = int(obj.get("nr", -1))
            if 0 <= idx < len(artikelen):
                artikelen[idx]["duiding"] = obj
    except Exception as e:
        logger.error(f"Media-duiding mislukt: {e}")
    return [a for a in artikelen if a.get("duiding") and a["duiding"].get("amsterdam", "ja") != "nee"]


def eerdere_raadsvragen(limit: int = 40) -> list[dict]:
    """Eerdere schriftelijke vragen en moties over vrouwen/meisjes uit het archief (tegen herhaling)."""
    with db.get_connection() as conn:
        rows = conn.execute(
            """SELECT titel, indiener, datum_ingediend, bron_url, type FROM items
               WHERE gemeente_slug = 'amsterdam' AND type IN ('schriftelijke_vraag', 'motie')
               AND datum_ingediend >= date('now', '-3 years')
               AND (titel LIKE '%vrouw%' OR titel LIKE '%meisje%' OR titel LIKE '%emancipatie%'
                    OR titel LIKE '%gender%' OR titel LIKE '%straatintimidatie%' OR titel LIKE '%femicide%'
                    OR titel LIKE '%huiselijk geweld%' OR titel LIKE '%seksue%')
               ORDER BY datum_ingediend DESC LIMIT ?""", (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def genereer_raadsvragen(stukken: list[dict], media: list[dict]) -> list[dict]:
    """Selecteert max 5 kansrijke onderwerpen en formuleert concept-raadsvragen. Kan leeg zijn."""
    kandidaten = [s for s in stukken if s.get("analyse", {}).get("classificatie") != "geen duidelijke relevantie"]
    inhoudelijk = [m for m in media if m.get("duiding", {}).get("duiding") == "inhoudelijk"]
    if not kandidaten and not inhoudelijk:
        return []
    eerder = eerdere_raadsvragen()

    st = []
    for n, s in enumerate(kandidaten[:25]):
        a = s["analyse"]
        st.append(f"S{n}. [{s['bron']} — {s['type']}] {s['titel']} ({s.get('datum')})\n"
                  f"   URL: {s['url']}\n   Classificatie: {a['classificatie']} | thema: {a.get('thema')}\n"
                  f"   Toelichting: {a.get('toelichting')}\n   Informatievraag: {a.get('informatievraag') or '-'}")
    md = [f"M{n}. [{m['bron']}, {m.get('datum')}] {m['titel']}\n   URL: {m['url']}\n   {m['duiding'].get('samenvatting')}"
          for n, m in enumerate(inhoudelijk[:15])]
    ev = [f"- {e['datum_ingediend']} [{e['type']}] {e['titel']} ({e.get('indiener')})" for e in eerder]

    prompt = f"""Je adviseert Nora Ait Boubker, raadslid D66 in de gemeenteraad van Amsterdam. Zij wil concept-raadsvragen over de positie van vrouwen in Amsterdams beleid. Zij beoordeelt en dient de vragen zelf in.

Selecteer UITSLUITEND onderwerpen waarvoor de bronnen hieronder voldoende aanleiding geven. Maximaal 5, minimaal 0. Liever 0 goede dan 3 gezochte. Vragen richten zich op: ontbrekende cijfers (uitsplitsing naar geslacht), verschillen in effecten, uitvoering, budget en verantwoording. Controleer tegen de lijst met eerdere raadsvragen: geen herhaling van wat al gevraagd is (verwijs er zo nodig naar als vervolgvraag).

BRONNEN — gemeentestukken:
{chr(10).join(st) or '(geen)'}

BRONNEN — media (inhoudelijk over vrouwen):
{chr(10).join(md) or '(geen)'}

EERDERE RAADSVRAGEN EN MOTIES OVER VROUWEN (afgelopen 3 jaar, uit het archief):
{chr(10).join(ev) or '(geen gevonden)'}

Antwoord UITSLUITEND met een JSON-lijst (leeg als niets kansrijk is). Per onderwerp een object met:
- onderwerp: korte titel
- bronnen: lijst van objecten {{"titel": ..., "url": ...}} (alleen bronnen uit de lijsten hierboven, URL letterlijk overgenomen)
- bronsamenvatting: 2-3 zinnen wat de bronnen zeggen
- analyse: 2-4 zinnen waarom dit kansrijk is en wat ontbreekt; benoem onzekerheid eerlijk
- eerdere_vragen: één zin over de check op herhaling (bv. "Geen eerdere vragen gevonden" of verwijzing)
- conceptvragen: lijst van 3-5 vragen, geformuleerd zoals schriftelijke vragen aan het college ("Is het college bekend met…", "Kan het college aangeven…")"""
    try:
        data = _json_uit(_vraag(prompt, 5000))
        return data[:5] if isinstance(data, list) else []
    except Exception as e:
        logger.error(f"Raadsvragen genereren mislukt: {e}")
        return []


def schrijf_hoofdpunten(stukken: list[dict], media: list[dict], vragen: list[dict]) -> str:
    """Korte samenvatting van de belangrijkste bronbevindingen (bovenaan de mail)."""
    if not stukken and not media:
        return "Er zijn vandaag geen nieuwe gemeentestukken of relevante media-artikelen gevonden."
    telling: dict[str, int] = {}
    for s in stukken:
        c = s.get("analyse", {}).get("classificatie", "onvoldoende informatie")
        telling[c] = telling.get(c, 0) + 1
    relevant = [s for s in stukken if s.get("analyse", {}).get("classificatie") in CLASSIFICATIES[:3]]
    prompt = f"""Schrijf in maximaal 120 woorden, in vloeiend Nederlands en zonder opsommingstekens, de belangrijkste bevindingen van vandaag voor de Amsterdamse Vrouwenmonitor.

Cijfers: {len(stukken)} nieuwe gemeentestukken beoordeeld; classificaties: {json.dumps(telling, ensure_ascii=False)}. {len(media)} media-artikelen met vermelding van vrouwen/meisjes, waarvan {sum(1 for m in media if m.get('duiding', {}).get('duiding') == 'inhoudelijk')} inhoudelijk. {len(vragen)} onderwerpen met concept-raadsvragen.

Meest relevante stukken:
{chr(10).join(f"- {s['titel']} → {s['analyse']['classificatie']}: {s['analyse'].get('toelichting')}" for s in relevant)[:3000] or '- geen'}

Onderwerpen raadsvragen: {', '.join(v.get('onderwerp', '') for v in vragen) or 'geen'}

Wees feitelijk. Suggereer geen volledige dekking. Als er weinig is, zeg dat gewoon."""
    try:
        return _vraag(prompt, 400).strip()
    except Exception as e:
        logger.error(f"Hoofdpunten mislukt: {e}")
        return f"{len(stukken)} gemeentestukken beoordeeld en {len(media)} media-artikelen gevonden."


# ── Stap 5: e-mail ───────────────────────────────────────────────────────────

KLEUR = {
    "expliciet meegewogen": "#1b7f4d",
    "gedeeltelijk meegewogen": "#8a6d00",
    "mogelijk relevant maar niet aantoonbaar meegewogen": "#b3441e",
    "geen duidelijke relevantie": "#6b6963",
    "onvoldoende informatie": "#4a5c8a",
}

CSS = """
  body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; font-size: 15px;
         color: #1a1a2e; max-width: 720px; margin: 0 auto; background: #f9f9f8; }
  .header { background: #003082; color: white; padding: 24px 32px; }
  .header h1 { margin: 0; font-size: 20px; } .header p { margin: 4px 0 0; font-size: 13px; opacity: .8; }
  .body { background: white; padding: 28px 32px; }
  h2 { font-size: 14px; font-weight: 700; color: #6b6963; text-transform: uppercase; letter-spacing: .5px;
       margin: 30px 0 12px; border-bottom: 1px solid #e2ddd8; padding-bottom: 6px; }
  h3 { font-size: 15px; margin: 18px 0 6px; }
  .intro { background: #f0eeeb; border-left: 4px solid #003082; padding: 14px 18px; border-radius: 0 8px 8px 0; line-height: 1.6; }
  .stuk { padding: 12px 0; border-bottom: 1px solid #f0eeeb; }
  .stuk a { color: #003082; text-decoration: none; font-weight: 600; }
  .meta { font-size: 12px; color: #9b9790; margin: 3px 0 6px; }
  .cls { display: inline-block; font-size: 11px; font-weight: 700; color: white; padding: 2px 8px; border-radius: 20px; margin-right: 6px; }
  .toel { font-size: 14px; line-height: 1.55; margin: 6px 0 0; }
  .vraag { background: #f7f9fc; border: 1px solid #e0e6f0; border-radius: 8px; padding: 14px 16px; margin: 12px 0; }
  .vraag ol { margin: 8px 0 0 18px; padding: 0; } .vraag li { margin: 4px 0; line-height: 1.5; }
  .bron { font-size: 13px; } .bron li { margin: 3px 0; }
  .status-ok { color: #1b7f4d; } .status-beperkt { color: #8a6d00; } .status-fout { color: #b3441e; }
  .footer { font-size: 12px; color: #9b9790; padding: 18px 32px; }
"""


def _e(s) -> str:
    return (str(s) if s is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def bouw_body_html(hoofdpunten: str, stukken: list[dict], media: list[dict],
                   vragen: list[dict], log: Bronlog) -> str:
    alinea = "".join(f"<p>{_e(p.strip())}</p>" for p in hoofdpunten.split("\n\n") if p.strip())
    h = [f'<div class="intro">{alinea}</div>']

    # Analyse gemeentestukken
    h.append(f"<h2>Analyse gemeentestukken ({len(stukken)})</h2>")
    if not stukken:
        h.append("<p>Geen nieuwe gemeentestukken gevonden sinds de vorige monitor.</p>")
    volgorde = {c: i for i, c in enumerate(CLASSIFICATIES)}
    for s in sorted(stukken, key=lambda x: volgorde.get(x.get("analyse", {}).get("classificatie"), 9)):
        a = s.get("analyse", {})
        c = a.get("classificatie", "onvoldoende informatie")
        info = f'<p class="toel"><em>Informatievraag: {_e(a.get("informatievraag"))}</em></p>' if a.get("informatievraag") else ""
        h.append(f'''<div class="stuk">
  <a href="{_e(s['url'])}">{_e(s['titel'])}</a>
  <div class="meta">{_e(s['bron'])} &bull; {_e(s['type'])} &bull; {_e(s.get('afzender') or '—')} &bull; {_e(s.get('datum'))}
    &bull; document: {_e(s.get('tekst_status') or 'alleen titel/toelichting')}</div>
  <span class="cls" style="background:{KLEUR.get(c, '#6b6963')}">{_e(c)}</span><span style="font-size:12px;color:#6b6963">thema: {_e(a.get('thema') or '-')}</span>
  <p class="toel">{_e(a.get('toelichting'))}</p>{info}
</div>''')

    # Mediaregister
    h.append(f"<h2>Vrouwen in de media ({len(media)})</h2>")
    if not media:
        h.append("<p>Geen nieuwe artikelen gevonden waarin vrouwen of meisjes worden genoemd.</p>")
    for m in sorted(media, key=lambda x: (x.get("duiding", {}).get("duiding") != "inhoudelijk", x.get("datum") or ""), reverse=False):
        d = m.get("duiding", {})
        inhoudelijk = d.get("duiding") == "inhoudelijk"
        lab = "Inhoudelijk over de positie van vrouwen" if inhoudelijk else "Uitsluitend vermelding"
        h.append(f'''<div class="stuk">
  <p class="toel" style="margin:0 0 6px">{_e(d.get('samenvatting'))}</p>
  <a href="{_e(m['url'])}">{_e(m['titel'])}</a>
  <div class="meta">{_e(m['bron'])} &bull; {_e(m.get('datum') or '?')}</div>
  <span class="cls" style="background:{'#1b7f4d' if inhoudelijk else '#6b6963'}">{lab}</span><span style="font-size:13px;color:#4a4a4a">{_e(d.get('toelichting'))}</span>
</div>''')

    # Concept-raadsvragen
    h.append(f"<h2>Concept-raadsvragen ({len(vragen)})</h2>")
    if not vragen:
        h.append("<p>De bronnen van vandaag geven onvoldoende aanleiding voor nieuwe raadsvragen.</p>")
    for v in vragen:
        bronnen = "".join(f'<li><a href="{_e(b.get("url"))}">{_e(b.get("titel"))}</a></li>' for b in v.get("bronnen", []))
        vr = "".join(f"<li>{_e(q)}</li>" for q in v.get("conceptvragen", []))
        h.append(f'''<div class="vraag">
  <h3>{_e(v.get('onderwerp'))}</h3>
  <p class="toel"><strong>Bronnen:</strong> {_e(v.get('bronsamenvatting'))}</p>
  <ul class="bron">{bronnen}</ul>
  <p class="toel"><strong>Analyse:</strong> {_e(v.get('analyse'))}</p>
  <p class="toel"><strong>Eerdere vragen:</strong> {_e(v.get('eerdere_vragen'))}</p>
  <p class="toel"><strong>Conceptvragen:</strong></p><ol>{vr}</ol>
</div>''')
    if vragen:
        h.append('<p style="font-size:13px;color:#6b6963">Conceptvragen zijn suggesties op basis van de bronnen. '
                 'Beoordeling en indiening gebeuren altijd door het raadslid zelf.</p>')

    # Bronverantwoording
    h.append('<h2>Gecontroleerde bronnen</h2><ul class="bron">')
    for r in log.regels:
        cls = {"gecontroleerd": "status-ok", "beperkt": "status-beperkt"}.get(r["status"], "status-fout")
        h.append(f'<li><strong class="{cls}">{_e(r["status"])}</strong> — {_e(r["naam"])}: {_e(r["detail"])}</li>')
    h.append('</ul><p style="font-size:13px;color:#6b6963">Betaalmuren en ontbrekende documenten zijn hierboven vermeld. '
             'Volledige dekking van alle gemeentelijke publicaties is niet vastgesteld.</p>')
    return "\n".join(h)


def bouw_mail_html(vandaag: date, body: str) -> str:
    return f"""<!DOCTYPE html><html lang="nl"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><style>{CSS}</style></head>
<body><div class="header"><h1>Amsterdamse Vrouwenmonitor</h1><p>{nl_datum(vandaag)} &bull; positie van vrouwen in Amsterdams beleid en lokale berichtgeving</p></div>
<div class="body">{body}</div>
<div class="footer">Automatisch samengesteld door Raadstracker. Analyses zijn AI-ondersteund en bedoeld als startpunt, niet als oordeel.</div>
</body></html>"""


# ── Orkestratie ──────────────────────────────────────────────────────────────

def run(dagen: int = 2, verstuur: bool = True) -> dict:
    db.init_db()
    init_vm_db()
    vandaag = date.today()
    log = Bronlog()

    # 1. Verzamelen
    kandidaten = verzamel_notubiz(dagen, log) + verzamel_bekendmakingen(dagen, log) + verzamel_os(log)
    controleer_overige_bronnen(log)
    media = verzamel_media(log)

    # Alleen niet eerder geziene stukken analyseren; documenttekst ophalen
    nieuw = []
    for s in kandidaten:
        if is_bekend(s["extern_id"]):
            continue
        if s.get("doc_url"):
            s["tekst"], s["tekst_status"], paginatitel = haal_pdf_tekst(s["doc_url"])
            if s.get("titel_uit_url") and paginatitel:
                s["titel"] = paginatitel[:400].replace(" | Onderzoek en Statistiek", "").strip()
            time.sleep(0.3)
        else:
            s["tekst"], s["tekst_status"] = "", "geen document gekoppeld"
        if not s["tekst"] and s.get("toelichting"):
            s["tekst"] = s["toelichting"]
        nieuw.append(s)
    logger.info(f"Vrouwenmonitor: {len(kandidaten)} kandidaten, {len(nieuw)} nieuw, {len(media)} media-artikelen")

    # 2-4. Analyse
    stukken = analyseer_stukken(nieuw)
    media = analyseer_media(media)
    vragen = genereer_raadsvragen(stukken, media)
    hoofdpunten = schrijf_hoofdpunten(stukken, media, vragen)

    for s in stukken:
        bewaar_stuk(s)

    body = bouw_body_html(hoofdpunten, stukken, media, vragen, log)
    html = bouw_mail_html(vandaag, body)
    onderwerp = f"Amsterdamse Vrouwenmonitor | {nl_datum(vandaag)}"

    verstuurd_naar = ""
    if verstuur:
        import resend
        resend.api_key = os.environ["RESEND_API_KEY"]
        from_email = os.environ.get("FROM_EMAIL", "briefing@raadstracker.nl")
        resend.Emails.send({
            "from": f"Amsterdamse Vrouwenmonitor <{from_email}>",
            "to": ONTVANGERS,
            "subject": onderwerp,
            "html": html,
        })
        verstuurd_naar = ", ".join(ONTVANGERS)
        logger.info(f"✅ Vrouwenmonitor verstuurd naar {verstuurd_naar}")
    else:
        Path(__file__).parent.joinpath("vrouwenmonitor_preview.html").write_text(html)
        logger.info("Droge run: vrouwenmonitor_preview.html geschreven")

    with db.get_connection() as conn:
        conn.execute(
            "INSERT INTO vm_rapporten (datum, onderwerp, body_html, data_json, verstuurd_naar) VALUES (?,?,?,?,?)",
            (vandaag.isoformat(), onderwerp, body,
             json.dumps({"stukken": len(stukken), "media": len(media), "vragen": len(vragen),
                         "bronnen": log.regels}, ensure_ascii=False),
             verstuurd_naar),
        )
    return {"stukken": len(stukken), "media": len(media), "vragen": len(vragen), "verstuurd": bool(verstuurd_naar)}


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    droog = "--droog" in sys.argv
    print(run(dagen=3 if droog else 2, verstuur=not droog))
