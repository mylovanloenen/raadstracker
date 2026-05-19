"""
Relevantiecheck via Claude: geeft een score 0-10 en tags terug.
"""

import json
import logging

import anthropic

logger = logging.getLogger(__name__)


def check_relevantie(
    client: anthropic.Anthropic,
    titel: str,
    type_item: str,
    onderwerpen: list[str],
) -> tuple[int, str]:
    """
    Vraag Claude een relevantiescore 0-10 en onderwerp-tags voor een raadsitem.
    Returns (score, comma-separated tags).
    """
    onderwerpen_str = "\n".join(f"- {o}" for o in onderwerpen)

    prompt = f"""Je analyseert raadsitems voor een Amsterdams raadslid.

Dossiers van dit raadslid:
{onderwerpen_str}

Raadsitem:
Type: {type_item}
Titel: {titel}

Geef:
1. Relevantiescore (0-10) voor dit item op basis van de dossiers. 0 = geen relatie, 10 = direct relevant.
2. Welke dossiers zijn van toepassing (alleen als score >= 4).

Antwoord ALLEEN als JSON, geen uitleg:
{{"score": <getal>, "tags": "<dossier1, dossier2 of leeg>"}}"""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=100,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        # Strip markdown code blocks if present
        raw = raw.strip("`").strip()
        if raw.startswith("json"):
            raw = raw[4:].strip()
        data = json.loads(raw)
        score = int(data.get("score", 0))
        tags = str(data.get("tags", ""))
        return max(0, min(10, score)), tags
    except Exception as e:
        logger.warning(f"Claude matching mislukt voor '{titel[:50]}': {e}")
        return 0, ""


def check_afdoening(
    client: anthropic.Anthropic,
    origineel_titel: str,
    type_item: str,
    afdoening_context: str,
) -> str:
    """
    Vraag Claude of een toezegging/motie inhoudelijk is afgedaan.
    Returns een korte beoordeling als tekst.
    """
    prompt = f"""Je beoordeelt of een raadsitem inhoudelijk is afgedaan.

Oorspronkelijk item:
Type: {type_item}
Titel: {origineel_titel}

Afdoeningsinformatie:
{afdoening_context}

Is dit item:
A) Inhoudelijk afgedaan (de gevraagde actie is daadwerkelijk uitgevoerd)
B) Formeel afgemeld (administratief gesloten zonder concrete uitvoering)
C) Nog onduidelijk

Geef een beknopte beoordeling in 1-2 zinnen, begin met A/B/C."""

    try:
        message = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as e:
        logger.warning(f"Afdoening check mislukt: {e}")
        return "Kon niet automatisch beoordelen."
