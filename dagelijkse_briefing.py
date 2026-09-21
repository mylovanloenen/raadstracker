"""
Dagelijkse briefing via e-mail — per gebruiker, alleen wat nieuw is sinds de vorige mail.

Opbouw: samenvatting (AI) · vergaderingen deze week · nieuw sinds vorige mail · uitslagen ·
termijnen · voor jouw onderwerpen · in de media. Lege secties vallen weg; is alles leeg,
dan gaat er geen mail.

Gebruik:
  python3 dagelijkse_briefing.py                       # alle actieve gebruikers
  python3 dagelijkse_briefing.py --test naam@mail.nl   # alleen dit adres, markeert niets als gemaild
  python3 dagelijkse_briefing.py --droog [adres]       # geen mail, schrijft mail_preview.html
"""

import html as html_mod
import json
import logging
import os
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import anthropic
import resend
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)
import database as db
from agenda import haal_agenda
from onderwerpen import score_item, sorteer_op_relevantie
from samenvattingen import vul_samenvattingen

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

SITE = "https://raadstracker.fly.dev"
MODEL = "claude-sonnet-4-6"
NL_MAANDEN = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
              "augustus", "september", "oktober", "november", "december"]
NL_DAGEN = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
TYPE_LABEL = {"motie": "Motie", "schriftelijke_vraag": "Schriftelijke vragen", "ingekomen_stuk": "Ingekomen stuk"}

# Media: journalistieke bronnen die voorrang krijgen; max per bron in de mail
MEDIA_VOORKEUR = ["het parool", "at5", "nh nieuws", "nul20", "binnenlands bestuur", "folia", "nrc", "de volkskrant", "trouw", "ad"]
MEDIA_MAX_PER_BRON = 3
AMSTERDAM_RE = re.compile(r"amsterdam|stadsdeel|zuidoost|nieuw-west|bijlmer|ijburg|weesp|gemeenteraad|wethouder|raadslid", re.I)

_agenda_cache: list | None = None
_ai_cache: dict[str, dict] = {}


# ── Hulpfuncties ─────────────────────────────────────────────────────────────

def e(s) -> str:
    return html_mod.escape(str(s) if s is not None else "", quote=True)


def nl_datum(d: date, met_dag: bool = False) -> str:
    kern = f"{d.day} {NL_MAANDEN[d.month - 1]} {d.year}"
    return f"{NL_DAGEN[d.weekday()]} {kern}" if met_dag else kern


def kort_datum(s: str | None) -> str:
    if not s:
        return "?"
    try:
        d = date.fromisoformat(s[:10])
        return f"{d.day} {NL_MAANDEN[d.month - 1][:3]}"
    except ValueError:
        return s


def utc_str(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def uitslag_label(u: str | None) -> str:
    t = (u or "").strip().lower()
    if not t:
        return ""
    for woord, label in (("aangenomen", "Aangenomen"), ("verworpen", "Verworpen"),
                         ("ingetrokken", "Ingetrokken"), ("aangehouden", "Aangehouden")):
        if woord in t:
            return label
    return (u or "").strip()[:30]


UITSLAG_KLEUR = {"Aangenomen": ("#e8f5e9", "#2e7d32"), "Verworpen": ("#fdecea", "#c0392b")}


def agenda_deze_week() -> list[dict]:
    global _agenda_cache
    if _agenda_cache is None:
        _agenda_cache = haal_agenda(dagen=7)
    return _agenda_cache


# ── Dataselectie per gebruiker ───────────────────────────────────────────────

def selecteer(email: str, onderwerpen: list[str]) -> dict:
    sinds = db.laatste_verzending(email) or utc_str(datetime.now(timezone.utc) - timedelta(hours=36))
    al_items = db.gemailde_ids(email, "item")
    al_uitslag = db.gemailde_ids(email, "uitslag")
    al_media = db.gemailde_ids(email, "media")

    # Nieuw sinds de vorige mail (of 36 uur bij een eerste mail)
    nieuw_alle = sorteer_op_relevantie([it for it in db.get_items_sinds(sinds, limit=80) if it["id"] not in al_items], onderwerpen)
    nieuw: dict[str, list] = {"schriftelijke_vraag": [], "motie": [], "ingekomen_stuk": []}
    for it in nieuw_alle:
        nieuw.setdefault(it["type"], []).append(it)
    ingekomen = nieuw["ingekomen_stuk"]
    nieuw["ingekomen_stuk"] = [i for i in ingekomen if i["relevantie"] > 0][:6] + [i for i in ingekomen if i["relevantie"] == 0][:3]
    nieuw["motie"] = nieuw["motie"][:8]
    nieuw["schriftelijke_vraag"] = nieuw["schriftelijke_vraag"][:8]
    nieuw_ids = {i["id"] for lst in nieuw.values() for i in lst}

    uitslagen = [it for it in db.get_uitslagen_sinds(sinds) if it["id"] not in al_uitslag][:8]

    termijnen = sorteer_op_relevantie(db.get_termijnen(dagen=7), onderwerpen)
    termijnen = ([t for t in termijnen if t["relevantie"] > 0] + [t for t in termijnen if t["relevantie"] == 0])[:5]

    relevant = sorteer_op_relevantie(db.get_items_laatste_dagen(7), onderwerpen, alleen_relevant=True)
    relevant = [r for r in relevant if r["id"] not in nieuw_ids and r["id"] not in al_items][:5]

    media = []
    for m in db.get_media_op_datum(dagen=3):
        if m["id"] in al_media:
            continue
        if not AMSTERDAM_RE.search(f"{m['titel']} {m.get('samenvatting') or ''}") and (m.get("query_tag") or "") not in ("gemeenteraad", "raad"):
            continue
        m = dict(m)
        s, _ = score_item(m["titel"], onderwerpen)
        m["relevantie"] = s + (2 if (m.get("bron") or "").strip().lower() in MEDIA_VOORKEUR else 0)
        media.append(m)
    media.sort(key=lambda x: (-x["relevantie"], x.get("datum") or ""))
    gekozen, per_bron = [], {}
    for m in media:
        b = (m.get("bron") or "").strip().lower()
        if per_bron.get(b, 0) >= MEDIA_MAX_PER_BRON:
            continue
        per_bron[b] = per_bron.get(b, 0) + 1
        gekozen.append(m)
        if len(gekozen) >= 5:
            break

    return {"sinds": sinds, "nieuw": nieuw, "uitslagen": uitslagen, "termijnen": termijnen,
            "relevant": relevant, "media": gekozen, "agenda": agenda_deze_week()}


def heeft_inhoud(d: dict) -> bool:
    return any([any(d["nieuw"].values()), d["uitslagen"], d["termijnen"], d["relevant"], d["media"]])


# ── AI-samenvatting ──────────────────────────────────────────────────────────

def _regel(it: dict, met_uitslag: bool = False) -> str:
    extra = f", uitslag: {uitslag_label(it.get('uitslag'))}" if met_uitslag and it.get("uitslag") else ""
    sam = f"\n  Inhoud: {it['samenvatting']}" if it.get("samenvatting") else ""
    return f"- [{TYPE_LABEL.get(it['type'], it['type'])}] {it['titel']} (indiener: {it.get('indiener') or '?'}, {it.get('datum_ingediend') or '?'}{extra}){sam}"


def genereer_samenvatting(naam: str, onderwerpen: list[str], d: dict) -> dict:
    """Geeft {'kop': str, 'alineas': [str, ...]}. Gecachet op inhoud: gebruikers met dezelfde selectie delen één call."""
    ids = sorted(i["id"] for lst in d["nieuw"].values() for i in lst) + [u["id"] for u in d["uitslagen"]] \
        + [t["id"] for t in d["termijnen"]] + [r["id"] for r in d["relevant"]] + [m["id"] for m in d["media"]]
    sleutel = json.dumps([sorted(o.lower() for o in onderwerpen), ids])
    if sleutel in _ai_cache:
        return _ai_cache[sleutel]

    agenda = "\n".join(f"- {v['datum']} {v['tijd']} {v['titel']} ({v['agenda_items']} agendapunten)" for v in d["agenda"][:8]) or "(geen)"
    nieuw = "\n".join(_regel(i) for lst in d["nieuw"].values() for i in lst) or "(geen)"
    uitslagen = "\n".join(_regel(u, met_uitslag=True) for u in d["uitslagen"]) or "(geen)"
    termijnen = "\n".join(f"- {t['titel']} (termijn {t['termijn_einde']}, indiener {t.get('indiener') or '?'})" for t in d["termijnen"]) or "(geen)"
    relevant = "\n".join(_regel(r, met_uitslag=True) for r in d["relevant"]) or "(geen)"
    media = "\n".join(f"- [{m['bron']}, {m.get('datum')}] {m['titel']}" for m in d["media"]) or "(geen)"

    prompt = f"""Je bent fractiemedewerker van {naam}, gemeenteraadslid in Amsterdam. Portefeuille: {', '.join(onderwerpen) or 'algemeen'}.
Vandaag is {nl_datum(date.today(), met_dag=True)}. Schrijf de ochtendbriefing die {naam.split()[0]} in twee minuten leest.

BRONNEN (alleen dit gebruiken; niets verzinnen, geen aannames over inhoud die niet in titel of 'Inhoud' staat):
Vergaderingen komende 7 dagen:
{agenda}
Nieuw sinds de vorige mail (raadsstukken, met indiener):
{nieuw}
Moties met nieuwe uitslag:
{uitslagen}
Termijnen schriftelijke vragen die binnen 7 dagen verlopen of verstreken zijn:
{termijnen}
Stukken van de afgelopen week die de portefeuille raken:
{relevant}
Lokaal nieuws:
{media}

SCHRIJF:
- Maximaal 120 woorden, drie korte alinea's, geen opsommingstekens, geen aanhef.
- Alinea 1: de één of twee ontwikkelingen die de portefeuille direct raken; noem indiener/partij en bij moties de uitslag.
- Alinea 2: wat vandaag of deze week op de agenda staat en welke termijn aandacht vraagt.
- Alinea 3: één concreet voorstel voor actie vandaag, afgeleid uit de bronnen.
- Als een bron leeg is, sla dat onderdeel over; zeg nooit "geen nieuws".
- Toon: zakelijk, direct, Nederlands, geen bijvoeglijke opsmuk. Geen landelijke politiek.
- Verwijs naar stukken met hun korte titel zodat ze terug te vinden zijn in de lijsten onder deze tekst.

Antwoord UITSLUITEND met JSON: {{"kop": "<het belangrijkste item in max 45 tekens, als krantenkop, zonder punt>", "alineas": ["...", "...", "..."]}}"""
    try:
        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"], max_retries=2, timeout=60)
        tekst = client.messages.create(model=MODEL, max_tokens=700,
                                       messages=[{"role": "user", "content": prompt}]).content[0].text
        data = json.loads(re.sub(r"^```(?:json)?\s*|\s*```$", "", tekst.strip()))
        res = {"kop": str(data.get("kop") or "")[:60].strip(),
               "alineas": [str(a).strip() for a in data.get("alineas", []) if str(a).strip()][:3]}
        if not res["alineas"]:
            raise ValueError("lege samenvatting")
    except Exception as ex:
        logger.error(f"AI-samenvatting mislukt, statische fallback: {ex}")
        n = sum(len(v) for v in d["nieuw"].values())
        delen = []
        if n:
            delen.append(f"{n} nieuwe raadsstukken sinds je vorige briefing.")
        if d["uitslagen"]:
            delen.append(f"{len(d['uitslagen'])} moties hebben een uitslag gekregen.")
        if d["termijnen"]:
            delen.append(f"{len(d['termijnen'])} schriftelijke vragen naderen of overschrijden hun termijn.")
        if d["agenda"]:
            delen.append(f"Deze week staan {len(d['agenda'])} vergaderingen gepland.")
        res = {"kop": "", "alineas": [" ".join(delen) or "Overzicht van vandaag."]}
    _ai_cache[sleutel] = res
    return res


# ── HTML ─────────────────────────────────────────────────────────────────────

FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"
KLEUR_BLAUW, KLEUR_TEKST, KLEUR_GRIJS, KLEUR_LIJN = "#003082", "#1a1a2e", "#6b6963", "#e2ddd8"


def kop_html(tekst: str) -> str:
    return (f'<tr><td style="padding:26px 0 10px;font:700 13px {FONT};color:{KLEUR_GRIJS};text-transform:uppercase;'
            f'letter-spacing:.5px;border-bottom:1px solid {KLEUR_LIJN}">{e(tekst)}</td></tr>')


def badge(tekst: str, achtergrond: str = "#e8edf6", kleur: str = KLEUR_BLAUW) -> str:
    return (f'<span style="display:inline-block;padding:2px 8px;border-radius:12px;font:600 12px {FONT};'
            f'background:{achtergrond};color:{kleur};margin-right:6px;white-space:nowrap">{e(tekst)}</span>')


def uitslag_badge(it: dict, standaard: str = "Motie") -> str:
    lab = uitslag_label(it.get("uitslag")) or standaard
    ach, kl = UITSLAG_KLEUR.get(lab, ("#e8edf6", KLEUR_BLAUW))
    return badge(lab, ach, kl)


def rij_html(titel: str, url: str, meta: str, badge_html: str = "", beschrijving: str = "") -> str:
    besch = (f'<div style="font:14px/1.5 {FONT};color:{KLEUR_TEKST};margin-top:5px">{e(beschrijving)}</div>'
             if beschrijving else "")
    return (f'<tr><td style="padding:12px 0;border-bottom:1px solid #f0eeeb">'
            f'<div style="font:600 15px/1.45 {FONT}">{badge_html}<a href="{e(url)}" style="color:{KLEUR_BLAUW};text-decoration:none">{e(titel)}</a></div>'
            f'{besch}'
            f'<div style="font:13px/1.4 {FONT};color:{KLEUR_GRIJS};margin-top:4px">{meta}</div></td></tr>')


def item_meta(it: dict) -> str:
    delen = [e(it.get("indiener") or "—"), kort_datum(it.get("datum_ingediend"))]
    if it.get("relevantie_hits"):
        delen.append("voor: " + e(", ".join(it["relevantie_hits"])))
    return " &bull; ".join(delen)


def sectie_items(kop: str, items: list[dict], label_fn=None, meta_fn=None) -> str:
    if not items:
        return ""
    out = kop_html(kop)
    for it in items:
        lab = label_fn(it) if label_fn else badge(TYPE_LABEL.get(it["type"], it["type"]))
        meta = meta_fn(it) if meta_fn else item_meta(it)
        out += rij_html(it.get("titel") or "(geen titel)", it.get("bron_url") or SITE, meta, lab, it.get("samenvatting") or "")
    return out


def termijn_meta(t: dict) -> str:
    try:
        dagen = (date.fromisoformat(t["termijn_einde"]) - date.today()).days
    except (ValueError, TypeError):
        dagen = None
    if dagen is None:
        status = "termijn onbekend"
    elif dagen < 0:
        status = f"termijn {-dagen} dagen verstreken"
    elif dagen == 0:
        status = "termijn verloopt vandaag"
    else:
        status = f"beantwoording verwacht binnen {dagen} dagen"
    return f"{e(t.get('indiener') or '—')} &bull; gesteld {kort_datum(t.get('datum_ingediend'))} &bull; <strong>{e(status)}</strong>"


def termijn_badge(t: dict) -> str:
    if (t.get("termijn_einde") or "9999") < date.today().isoformat():
        return badge("Verstreken", "#fdecea", "#c0392b")
    return badge("Deze week", "#fff3e0", "#b45309")


def bouw_body(ai: dict, d: dict) -> str:
    delen = []
    kop = f'<p style="margin:0 0 10px;font:700 16px/1.4 {FONT};color:{KLEUR_TEKST}">{e(ai["kop"])}</p>' if ai.get("kop") else ""
    alineas = "".join(f'<p style="margin:0 0 10px;font:15px/1.6 {FONT};color:{KLEUR_TEKST}">{e(a)}</p>' for a in ai["alineas"])
    delen.append(f'<tr><td style="background:#f0eeeb;border-left:4px solid {KLEUR_BLAUW};padding:16px 18px;border-radius:0 8px 8px 0">{kop}{alineas}</td></tr>')

    if d["agenda"]:
        delen.append(kop_html("Vergaderingen deze week"))
        for v in d["agenda"][:8]:
            try:
                dag = nl_datum(date.fromisoformat(v["datum"]), met_dag=True).rsplit(" ", 1)[0]
            except (ValueError, TypeError):
                dag = v["datum"] or "?"
            meta = f"{e(dag)} {e(v['tijd'])} &bull; {e(v['categorie'] or v['categorie_type'])} &bull; {v['agenda_items']} agendapunten"
            delen.append(rij_html(v["titel"], v["url"] or f"{SITE}/agenda", meta))

    n = d["nieuw"]
    delen.append(sectie_items("Nieuw: schriftelijke vragen", n["schriftelijke_vraag"]))
    delen.append(sectie_items("Nieuw: moties en amendementen", n["motie"], label_fn=uitslag_badge))
    delen.append(sectie_items("Nieuw: ingekomen stukken en raadsbrieven", n["ingekomen_stuk"]))
    delen.append(sectie_items("Uitslagen", d["uitslagen"], label_fn=lambda it: uitslag_badge(it, "Uitslag")))
    delen.append(sectie_items("Termijnen schriftelijke vragen", d["termijnen"], label_fn=termijn_badge, meta_fn=termijn_meta))
    delen.append(sectie_items("Voor jouw onderwerpen (afgelopen week)", d["relevant"]))

    if d["media"]:
        delen.append(kop_html("In de media"))
        for m in d["media"]:
            delen.append(rij_html(m["titel"], m.get("url") or SITE, f"{e(m.get('bron') or 'Nieuws')} &bull; {kort_datum(m.get('datum'))}"))
    return "".join(x for x in delen if x)


def bouw_mail(naam: str, ai: dict, d: dict, preheader: str) -> str:
    body = bouw_body(ai, d)
    return f"""<!DOCTYPE html>
<html lang="nl"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="color-scheme" content="light dark"><meta name="supported-color-schemes" content="light dark">
<title>Raadstracker briefing</title>
<style>
  @media (prefers-color-scheme: dark) {{
    .wrap {{ background:#1c1c1e !important; }} .kaart {{ background:#26262a !important; }}
    .kaart td, .kaart p, .kaart div {{ color:#f2f2f2 !important; }} .kaart a {{ color:#8ab4f8 !important; }}
  }}
</style></head>
<body style="margin:0;padding:0;background:#f9f9f8">
<div style="display:none;max-height:0;overflow:hidden;font-size:1px;line-height:1px;color:#f9f9f8;mso-hide:all">{e(preheader)}&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;&nbsp;&zwnj;</div>
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" class="wrap" style="background:#f9f9f8"><tr><td align="center" style="padding:16px 12px">
<table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%">
<tr><td style="background:{KLEUR_BLAUW};padding:20px 24px;border-radius:12px 12px 0 0">
  <h1 style="margin:0;font:700 19px/1.3 {FONT};color:#ffffff">Raadstracker &middot; ochtendbriefing</h1>
  <p style="margin:4px 0 0;font:13px {FONT};color:#c9d3e8">{e(naam)} &bull; {e(nl_datum(date.today(), met_dag=True))}</p>
</td></tr>
<tr><td class="kaart" style="background:#ffffff;padding:22px 24px 8px">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{body}</table>
</td></tr>
<tr><td style="padding:16px 24px;font:12px/1.6 {FONT};color:{KLEUR_GRIJS};border-top:1px solid {KLEUR_LIJN};background:#f9f9f8;border-radius:0 0 12px 12px">
  <a href="{SITE}" style="color:{KLEUR_BLAUW}">Raadstracker</a> &bull; <a href="{SITE}/nieuws" style="color:{KLEUR_BLAUW}">Nieuws vandaag</a> &bull;
  <a href="{SITE}/agenda" style="color:{KLEUR_BLAUW}">Agenda</a> &bull; <a href="{SITE}/briefing" style="color:{KLEUR_BLAUW}">Eigen briefing maken</a><br>
  Je ontvangt deze mail omdat je in de gebruikerslijst van Raadstracker staat. Afmelden: antwoord met "uitschrijven".
</td></tr>
</table></td></tr></table>
</body></html>"""


def bouw_tekst(naam: str, ai: dict, d: dict) -> str:
    r = [f"Raadstracker ochtendbriefing — {naam}, {nl_datum(date.today(), met_dag=True)}", ""]
    if ai.get("kop"):
        r.append(ai["kop"].upper())
    r += ai["alineas"] + [""]
    if d["agenda"]:
        r += ["VERGADERINGEN DEZE WEEK"] + [f"- {v['datum']} {v['tijd']} {v['titel']} {v['url']}" for v in d["agenda"][:8]] + [""]
    for kop, lst in (("NIEUWE SCHRIFTELIJKE VRAGEN", d["nieuw"]["schriftelijke_vraag"]),
                     ("NIEUWE MOTIES", d["nieuw"]["motie"]),
                     ("NIEUWE INGEKOMEN STUKKEN", d["nieuw"]["ingekomen_stuk"]),
                     ("UITSLAGEN", d["uitslagen"]), ("TERMIJNEN", d["termijnen"]), ("VOOR JOUW ONDERWERPEN", d["relevant"])):
        if lst:
            r.append(kop)
            for i in lst:
                r.append(f"- {i['titel']} ({i.get('indiener') or '—'}) {i.get('bron_url') or ''}")
                if i.get("samenvatting"):
                    r.append(f"  {i['samenvatting']}")
            r.append("")
    if d["media"]:
        r += ["IN DE MEDIA"] + [f"- [{m['bron']}] {m['titel']} {m.get('url') or ''}" for m in d["media"]] + [""]
    r.append(SITE)
    return "\n".join(r)


def onderwerpregel(ai: dict, d: dict) -> str:
    n_nieuw = sum(len(v) for v in d["nieuw"].values())
    delen = []
    if n_nieuw:
        delen.append(f"{n_nieuw} nieuw")
    if d["uitslagen"]:
        delen.append(f"{len(d['uitslagen'])} uitslag" + ("en" if len(d["uitslagen"]) > 1 else ""))
    if d["termijnen"]:
        delen.append(f"{len(d['termijnen'])} termijn" + ("en" if len(d["termijnen"]) > 1 else ""))
    tellingen = ", ".join(delen)
    if ai.get("kop"):
        return f"{ai['kop']} · {tellingen}" if tellingen else ai["kop"]
    return f"Raadsbriefing {nl_datum(date.today(), met_dag=True)}" + (f" · {tellingen}" if tellingen else "")


# ── Versturen ────────────────────────────────────────────────────────────────

def stuur_briefing(naam: str, email: str, onderwerpen: list | None = None, test: bool = False, droog: bool = False) -> bool:
    db.init_db()
    db.init_media_db()
    onderwerpen = onderwerpen or []

    d = selecteer(email, onderwerpen)
    if not heeft_inhoud(d):
        logger.info(f"Niets nieuws voor {email} — mail overgeslagen")
        return False

    logger.info(f"Briefing voor {naam}: {sum(len(v) for v in d['nieuw'].values())} nieuw, {len(d['uitslagen'])} uitslagen, "
                f"{len(d['termijnen'])} termijnen, {len(d['relevant'])} relevant, {len(d['media'])} media")
    try:
        vul_samenvattingen([i for lst in d["nieuw"].values() for i in lst] + d["uitslagen"] + d["relevant"] + d["termijnen"])
    except Exception as ex:
        logger.error(f"Samenvattingen mislukt, mail gaat zonder: {ex}")
    ai = genereer_samenvatting(naam, onderwerpen, d)
    preheader = (ai["alineas"][0] if ai["alineas"] else "")[:140]
    onderwerp = onderwerpregel(ai, d)
    html = bouw_mail(naam, ai, d, preheader)
    tekst = bouw_tekst(naam, ai, d)

    if droog:
        Path(__file__).parent.joinpath("mail_preview.html").write_text(html)
        logger.info(f"Droge run: mail_preview.html geschreven — onderwerp: {onderwerp}")
        return True

    resend.api_key = os.environ["RESEND_API_KEY"]
    from_email = os.environ.get("FROM_EMAIL", "briefing@d66-connect.com")
    resend.Emails.send({
        "from": f"Raadstracker <{from_email}>",
        "to": [email],
        "reply_to": from_email,
        "subject": onderwerp,
        "html": html,
        "text": tekst,
        "headers": {"List-Unsubscribe": f"<mailto:{from_email}?subject=uitschrijven>"},
    })
    logger.info(f"✅ Briefing verstuurd naar {email}: {onderwerp}")

    if not test:
        db.markeer_gemaild(email, "item", [i["id"] for lst in d["nieuw"].values() for i in lst] + [r["id"] for r in d["relevant"]])
        db.markeer_gemaild(email, "uitslag", [u["id"] for u in d["uitslagen"]])
        db.markeer_gemaild(email, "media", [m["id"] for m in d["media"]])
    return True


def run(alleen_email: str | None = None, test: bool = False, droog: bool = False) -> int:
    import yaml
    global _agenda_cache
    _agenda_cache = None
    _ai_cache.clear()
    with open(Path(__file__).parent / "gebruikers.yaml") as f:
        config = yaml.safe_load(f)

    verstuurd = 0
    for g in config.get("gebruikers", []):
        if not g.get("actief", True):
            continue
        if alleen_email and g["email"].lower() != alleen_email.lower():
            continue
        try:
            if stuur_briefing(g["naam"], g["email"], g.get("onderwerpen", []), test=test, droog=droog):
                verstuurd += 1
        except Exception as ex:
            logger.error(f"Fout bij {g['email']}: {ex}")
    logger.info(f"Briefings verstuurd: {verstuurd}")
    return verstuurd


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--droog" in args:
        i = args.index("--droog")
        run(alleen_email=args[i + 1] if len(args) > i + 1 else None, droog=True)
    elif "--test" in args:
        run(alleen_email=args[args.index("--test") + 1], test=True)
    else:
        run()
