# Raadstracker v1

Dagelijkse tracker voor toezeggingen, moties en schriftelijke vragen van de Amsterdamse gemeenteraad.

## Wat het doet

Elke ochtend om 07:00:
1. Scrapet de nieuwste moties, schriftelijke vragen en ingekomen stukken van [amsterdam.raadsinformatie.nl](https://amsterdam.raadsinformatie.nl)
2. Laat Claude beoordelen welke items relevant zijn voor jouw dossiers (score 0–10)
3. Berekent welke termijnen naderen of verstreken zijn
4. Stuurt een HTML-mail met drie buckets: termijn nadert / verstreken / ruim over termijn

## Installatie

### Vereisten
- Python 3.11 of hoger
- pip

### Stappen

```bash
# 1. Kopieer de repo of maak een nieuwe map
cd raadstracker

# 2. Installeer afhankelijkheden
pip install -r requirements.txt

# 3. Configureer omgevingsvariabelen
cp .env.example .env
# Bewerk .env en vul je API-key en SMTP-gegevens in

# 4. Pas je dossiers aan in config.yaml (optioneel)
```

### .env invullen

```
ANTHROPIC_API_KEY=sk-ant-...
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=jouw@email.nl
SMTP_PASS=app-wachtwoord      # Gmail: maak een App Password aan
RECIPIENT_EMAIL=raadslid@amsterdam.nl
```

## Gebruik

```bash
# Stap 1: test de scraper (niets opgeslagen)
python main.py demo

# Stap 2: haal items op en sla op in database
python main.py scrape

# Stap 3: match items op jouw dossiers via Claude
python main.py match

# Stap 4: bekijk de mail zonder te versturen
python main.py mail-preview

# Stap 5: verstuur de mail
python main.py mail

# Alles tegelijk (dagelijkse run)
python main.py run

# Start de scheduler (dagelijks 07:00)
python main.py schedule
```

## Configuratie (config.yaml)

- **onderwerpen**: jouw dossiers — Claude matcht elk item hier tegenaan
- **relevantie_drempel**: items met score >= deze waarde worden meegenomen (standaard 6)
- **bronnen**: schakel moties, schriftelijke vragen of ingekomen stukken aan/uit
- **termijnen_weken**: hoe lang een item open mag staan per type

## Technische keuzes

**Waarom HTML-scraping en niet de Notubiz API?**

De publieke Notubiz API (`api.notubiz.nl`) ondersteunt geen sortering op datum of datum-filtering. De HTML-listpagina's van Amsterdam RIS renderen de meest recente 10 items server-side, inclusief alle benodigde velden (titel, datum, uitslag, datum afdoening) in gestructureerde `<tr data-id="...">` rows met CSS-klassen als `field_1`, `field_15`, `field_17`. Dit is stabieler en betrouwbaarder dan API-endpoints zonder documentatie.

**Database**: SQLite — één bestand (`tracker.db`), geen server nodig.

**AI-matching**: Claude Haiku (goedkoop, snel) voor relevantie-check. Claude Haiku voor afdoening-check.

## Cron instellen

```bash
# Voeg toe aan crontab (crontab -e):
0 7 * * * cd /pad/naar/raadstracker && python main.py run >> logs/tracker.log 2>&1
```

Of gebruik de ingebouwde scheduler:

```bash
python main.py schedule  # Blijft draaien op de achtergrond
```

## Versie 2 (mogelijke uitbreidingen)

- Stadsdeelnotulen (7 stadsdelen)
- Tweede Kamer open data API
- Per-vergadering briefing
- Webapp voor markeren als 'rappel gestuurd' of 'afgedaan'
