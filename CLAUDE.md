# Raadstracker — handoff voor Claude Code

Dit bestand geeft Claude Code volledige context over het Raadstracker-project zodat je direct verder kunt werken zonder de eerdere gesprekken te hoeven lezen.

---

## Wat is Raadstracker?

Een AI-aangedreven politiek informatieplatform voor Amsterdamse raadsleden en betrokkenen. Het haalt automatisch raadsstukken, moties en nieuws op, biedt een AI-chatbot, genereert dagelijkse briefings en stuurt die per email.

**Live URL:** https://raadstracker.fly.dev  
**GitHub:** https://github.com/mylovanloenen/raadstracker  
**Deploy:** Fly.io (regio: Amsterdam), automatisch via GitHub Actions bij push naar `main`

---

## Tech stack

| Onderdeel | Technologie |
|-----------|-------------|
| Backend | FastAPI + Jinja2 |
| Database | SQLite met FTS5, persistent volume op Fly.io (`/data/tracker.db`) |
| AI | Claude claude-sonnet-4-6 (Anthropic), streaming via SSE |
| Email | Resend, from: `briefing@d66-connect.com` |
| Hosting | Fly.io, 512MB RAM, shared CPU |
| Cron | GitHub Actions: dagelijks 06:00 UTC (= 08:00 CEST) |
| Nieuws | Google News RSS (21 queries) |

---

## Secrets / environment variables

Beheer via `flyctl secrets`:

| Naam | Waarde / doel |
|------|--------------|
| `ANTHROPIC_API_KEY` | Claude API key |
| `RESEND_API_KEY` | Resend email API key |
| `SCRAPE_TOKEN` | `raadstracker2024` — authoriseert `/api/dagelijkse-update` endpoint |

GitHub Actions heeft dezelfde secrets nodig als repository secrets (Settings → Secrets).

**Let op:** bij het instellen via zsh geen `<` of `>` gebruiken rond de waarde — dat interpreteert zsh als redirection. Gebruik:
```bash
flyctl secrets set SCRAPE_TOKEN=raadstracker2024
```

---

## Databronnen

### Amsterdam (hoofdbron)
- **API:** `api.notubiz.nl`, org ID `281`
- **Typen:** moties, schriftelijke vragen, toezeggingen, raadsbesluiten
- **Importscript:** `scraper.py`, `bulk_import.py`

### SDC (Stadsregio/deelgemeenten)
- **API:** zelfde Notubiz API, 7 organisaties
- **Importscript:** `sdc_import.py`
- **gemeente_slug:** bijv. `sdc_west`, `sdc_oost`, etc.

### Waterschap AGV (Amstel, Gooi en Vecht)
- **API:** Notubiz org ID `1707`
- **Modules:** 6 = motie, 4 = schriftelijke vraag
- **Importscript:** `agv_import.py`
- **gemeente_slug:** `waterschap_agv`
- **Veld-IDs:** 1=titel, 15=datum, 37=partijen, 62=uitslag, 2=document-URL
- **Let op:** agv.notubiz.nl is achter Cloudflare, gebruik `api.notubiz.nl/document/{id}/{version}` als bron_url

### Nieuws (Google News RSS)
- **Importscript:** `media_import.py`
- **Queries:** 21 totaal, inclusief 7 gericht op Ricardo's thema's (democratisering, digitale stad, opvang, Nieuw-West, Zuidoost, jongerenwerk, stadsdeel Zuidoost)

---

## Dagelijkse cron (GitHub Actions)

Twee workflows in `.github/workflows/`:

1. **deploy.yml** — deployt naar Fly.io bij elke push naar `main`
2. **dagelijks.yml** — draait elke dag 06:00 UTC:
   - POST naar `/api/dagelijkse-update?token=raadstracker2024` → haalt nieuwe raadsstukken op
   - POST naar `/api/media-update?token=raadstracker2024` → haalt nieuws op
   - POST naar `/api/briefing-sturen?token=raadstracker2024` → stuurt emails

---

## Emaillijst

Beheerd in `gebruikers.yaml`. Velden per gebruiker:
- `naam` — wordt gebruikt in aanhef email
- `email` — ontvanger
- `gemeente` — momenteel altijd `amsterdam`
- `actief` — `true`/`false`
- `onderwerpen` — lijst met thema's voor de briefing

**Huidige ontvangers:**
- Mylo van Loenen — mylovanloenen@gmail.com
- R.B.F.A. van Loenen — rbfavanloenen@gmail.com
- Sidney Cruickshank — sidney.cruickshank@gmail.com

Om iemand toe te voegen: bewerk `gebruikers.yaml`, commit en push.

---

## Features

### Hoofdpagina (`/`)
- Tabs: Amsterdam, SDC-gemeenten, Waterschap AGV
- Badge-kleuren: TK=oranje, AGV=groen, SDC=paars
- Zoekbalk met FTS5 full-text search

### Archief (`/archief`)
- Alle raadsstukken doorzoekbaar en filterbaar

### Agenda (`/agenda`)
- Live fetch van Notubiz events API: `api.notubiz.nl/organisations/281/events`
- Toont aankomende vergaderingen

### Statistieken (`/statistieken`)
- Per maand, per type, top fracties, uitslag-verdeling

### Fracties (`/fracties`)
- Overzicht alle fracties met aantal ingediende stukken

### Briefing (`/briefing`)
- Onderwerpen invoeren → Claude genereert samenvatting
- **Presets:**
  - Mylo: wonen, volkshuisvesting, OV, jeugdzorg, klimaat, duurzaamheid, veiligheid, financiën
  - Ricardo: democratisering, opvang, digitale stad, Masterplan Nieuw-West, Masterplan Zuidoost, jongerenwerk, Stadsdeel Zuidoost
  - Nieuw-West & Zuidoost
  - Digitaal & Democratie
- Bronverwijzingen [n] zijn klikbare blauwe links naar het originele document
- Streaming via SSE

### AI Chat (`/chat`)
- Chatbot over het raadsarchief
- Gespreksgeheugen: client-side array, max 10 berichten meegegeven per request
- Bronverwijzingen [n] klikbaar
- Streaming via SSE

### Dagelijkse email briefing
- **Alleen Amsterdam** (geen Tweede Kamer, geen AGV, geen toezeggingen)
- Altijd recente raadsstukken (laatste 15, ongeacht scrape-timing)
- Recente moties (laatste 14 dagen)
- Nieuws-sectie met AT5/Parool/NH Nieuws artikelen
- Onderwerpen worden uitgebreid met synoniemen (zie `SYNONIEMEN` dict in `app.py`)

---

## Synoniemen voor dunne thema's

In `app.py` staat een `SYNONIEMEN` dict die smalle zoektermen uitbreidt:

```python
SYNONIEMEN = {
    "democratisering":       ["democratisering", "participatie", "inspraak", "burgerberaad", "bewonersinitiatieven"],
    "digitale stad":         ["digitale stad", "digitalisering", "ICT", "technologie", "data", "smart city", "algoritme"],
    "opvang":                ["opvang", "daklozen", "asiel", "vluchtelingen", "maatschappelijke opvang", "noodopvang"],
    "jongerenwerk":          ["jongerenwerk", "jongeren", "jeugd", "jongerencentrum", "straatwerk"],
    "masterplan nieuw-west": ["nieuw-west", "masterplan nieuw-west", "osdorp", "geuzenveld", "slotervaart"],
    "masterplan zuidoost":   ["zuidoost", "masterplan zuidoost", "bijlmer", "amsterdam-zuidoost", "gaasperdam"],
    "stadsdeel zuidoost":    ["zuidoost", "stadsdeel zuidoost", "bijlmer", "amsterdam-zuidoost"],
}
```

---

## Belangrijke bestanden

| Bestand | Doel |
|---------|------|
| `app.py` | FastAPI app, alle routes, AI-logica, briefing-endpoint |
| `database.py` | SQLite queries, FTS5 search, statistieken |
| `dagelijkse_briefing.py` | Email genereren en versturen via Resend |
| `dagelijkse_update.py` | Dagelijkse scrape + alerting |
| `media_import.py` | Google News RSS import |
| `scraper.py` | Amsterdam Notubiz scraper |
| `agv_import.py` | Waterschap AGV scraper |
| `sdc_import.py` | SDC-gemeenten scraper |
| `gebruikers.yaml` | Emaillijst + onderwerpen per gebruiker |
| `templates/` | Jinja2 HTML templates |
| `static/style.css` | Globale CSS incl. mobiel/dark mode |
| `fly.toml` | Fly.io configuratie |

---

## Mobiel

De site is volledig responsief. Op mobiel is er een bottom navigation bar met 5 items: Nieuws, Archief, Agenda, Briefing, AI Chat. Gedefinieerd in `templates/base.html` als `.mobiel-nav`.

---

## Deploy

```bash
# Handmatig deployen
flyctl deploy

# Secrets beheren
flyctl secrets set NAAM=waarde
flyctl secrets list

# Logs bekijken
flyctl logs

# Handmatige dagelijkse update triggeren
curl -X POST "https://raadstracker.fly.dev/api/dagelijkse-update?token=raadstracker2024"
```

---

## Bekende beperkingen / aandachtspunten

- **AGV bron-URLs** zijn Notubiz API-links (PDF), niet de publieke agv.notubiz.nl pagina's (die zit achter Cloudflare)
- **Dunne thema's** (democratisering, digitale stad): weinig raadsstukken, synoniemen + nieuws compenseren dit
- **Emailafzender** heeft geen profielfoto (Gravatar/Google-profiel aanmaken op briefing@d66-connect.com zou dit oplossen, maar vereist handmatige verificatie)
- **SQLite** op Fly.io volume: geen automatische backup — overweeg Litestream voor productie
