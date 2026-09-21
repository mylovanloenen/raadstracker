"""Onderwerpen, synoniemen en relevantiescoring — gedeeld door briefingpagina, mail en alerts."""

import re
import unicodedata

SYNONIEMEN = {
    "democratisering":  ["democratisering", "participatie", "inspraak", "burgerberaad", "bewonersinitiatieven"],
    "digitale stad":    ["digitale stad", "digitalisering", "ICT", "technologie", "data", "smart city", "algoritme"],
    "opvang":           ["opvang", "daklozen", "asiel", "vluchtelingen", "maatschappelijke opvang", "noodopvang"],
    "jongerenwerk":     ["jongerenwerk", "jongeren", "jeugd", "jongerencentrum", "straatwerk"],
    "masterplan nieuw-west": ["nieuw-west", "masterplan nieuw-west", "osdorp", "geuzenveld", "slotervaart"],
    "masterplan zuidoost":   ["zuidoost", "masterplan zuidoost", "bijlmer", "amsterdam-zuidoost", "gaasperdam"],
    "stadsdeel zuidoost":    ["zuidoost", "stadsdeel zuidoost", "bijlmer", "amsterdam-zuidoost"],
    "wonen":            ["wonen", "woning", "woningen", "huur", "huurders", "woningbouw", "sociale huur", "woningcorporatie"],
    "volkshuisvesting": ["volkshuisvesting", "woningbouw", "sociale huur", "corporatie", "betaalbaar wonen"],
    "openbaar vervoer": ["openbaar vervoer", "ov", "gvb", "tram", "metro", "bus", "nachtnet"],
    "jeugdzorg":        ["jeugdzorg", "jeugdhulp", "jeugdbescherming", "levvel", "jeugd"],
    "klimaat":          ["klimaat", "co2", "energietransitie", "aardgasvrij", "hitte", "klimaatadaptatie"],
    "duurzaamheid":     ["duurzaamheid", "duurzaam", "circulair", "zonnepanelen", "energie"],
    "veiligheid":       ["veiligheid", "politie", "handhaving", "overlast", "criminaliteit", "wapens", "geweld"],
    "financiën":        ["financiën", "financien", "begroting", "bezuiniging", "jaarrekening", "voorjaarsnota", "budget"],
}


def normaliseer(tekst: str) -> str:
    """Kleine letters, accenten weg, koppeltekens naar spaties."""
    t = unicodedata.normalize("NFKD", tekst or "").encode("ascii", "ignore").decode()
    return re.sub(r"[-_/]", " ", t.lower())


def expandeer_termen(onderwerpen: list[str]) -> list[str]:
    """Onderwerpen plus synoniemen, uniek en in volgorde."""
    termen: list[str] = []
    for o in onderwerpen or []:
        k = o.strip().lower()
        termen.append(k)
        for sleutel, syns in SYNONIEMEN.items():
            if k == sleutel or k in (s.lower() for s in syns):
                termen.extend(s.lower() for s in syns)
    return list(dict.fromkeys(t for t in termen if t))


def _woordgrens(term: str) -> re.Pattern:
    return re.compile(r"(?<![a-z0-9])" + re.escape(normaliseer(term)) + r"(?![a-z0-9])")


def score_item(titel: str, onderwerpen: list[str]) -> tuple[int, list[str]]:
    """Gewogen score: exact onderwerp 3, synoniem 2. Geeft (score, gematchte onderwerpen)."""
    if not onderwerpen:
        return 0, []
    tekst = normaliseer(titel)
    score, hits = 0, []
    for o in onderwerpen:
        k = o.strip().lower()
        if not k:
            continue
        if _woordgrens(k).search(tekst):
            score += 3
            hits.append(o)
            continue
        syns = SYNONIEMEN.get(k, [])
        if any(_woordgrens(s).search(tekst) for s in syns if s.lower() != k):
            score += 2
            hits.append(o)
    return score, hits


def sorteer_op_relevantie(items: list[dict], onderwerpen: list[str], alleen_relevant: bool = False) -> list[dict]:
    gescoord = []
    for it in items:
        s, hits = score_item(it.get("titel") or "", onderwerpen)
        if alleen_relevant and s == 0:
            continue
        it = dict(it)
        it["relevantie"] = s
        it["relevantie_hits"] = hits
        gescoord.append(it)
    return sorted(gescoord, key=lambda x: (-x["relevantie"], x.get("datum_ingediend") or ""), )
