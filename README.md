# Studielan Rentekalkulator

Dashboard som hjelper deg vurdere om du bor binde renta pa studielanet hos Lanekassen.

**Live:** [studielan.77-42-36-80.sslip.io](https://studielan.77-42-36-80.sslip.io/)

## Hva gjor den?

- Henter gjeldende renter fra **Lanekassen** (flytende + fast 3/5/10 ar)
- Henter live **swap-renter** fra SEB (markedets renteforventning)
- Henter topp-5 **bankrenter** fra Finansportalen per bindingsperiode
- **Estimerer neste Lanekassen-fastrente** (snitt topp-5 nominelle bankrenter - 0.15pp)
- Beregner **besparelse** ved a binde na vs vente til neste vindu
- Viser detaljert **NPV-utregning** iht. finansavtaleforskriften § 2-1
- Gir en **anbefaling** per tenor basert pa estimert rateendring + swap-trend

## Kom i gang lokalt

Appen kjorer lokalt uten Docker, auth eller ekstern database. Alt du trenger er Python 3.11+ og [uv](https://docs.astral.sh/uv/).

```bash
# Klon repoet
git clone https://github.com/JorgenStensrudForte/studielan.git
cd studielan

# Installer avhengigheter
uv sync

# Start appen
uv run uvicorn app.main:app --reload --port 8000
```

Apne [http://localhost:8000](http://localhost:8000). Dashboardet fungerer umiddelbart — Lanekassen-renter, bankrenter og swap-renter hentes live.

### Backfill av swap-historikk

Appen lagrer swap-renter i en lokal SQLite-database (`data/studielan.db`) som opprettes automatisk ved oppstart. Hvert minutt hentes nye swap-renter fra SEB og lagres.

Problemet er at **swap-grafen og trendanalysen trenger historikk** — uten historikk vises bare dagens rate uten kontekst.

Du har to mater a fylle databasen:

**1. Automatisk fra Cbonds (anbefalt)**

Klikk "Last ned historikk fra Cbonds"-knappen i det gule banneret pa dashboardet, eller kjor:

```bash
curl -X POST http://localhost:8000/api/bootstrap
```

Dette henter ~365 dager med historiske swap-renter for 3, 5 og 10 ar.

**2. Vent pa at data samles**

Appen henter swap-renter fra SEB hvert minutt. Etter noen uker har du nok datapunkter til en meningsfull trendanalyse. Grafen viser siste 90 dager.

### Kjor tester

```bash
uv run pytest tests/ -v
```

## Docker (produksjon)

```bash
# Kopier og fyll ut miljovaribler
cp .env.example .env
# Rediger .env — generer passord-hash:
#   docker run --rm caddy caddy hash-password --plaintext "ditt-passord"

# Start
docker compose up --build
```

Caddy haandterer basic auth foran FastAPI. I produksjon kjorer appen bak Traefik med TLS.

## Arkitektur

```
app/
  main.py          FastAPI app, routes, beregningslogikk
  config.py        Innstillinger, konstanter, soknadsvinduer
  models.py        Dataclasses (LanekassenRate, SwapRate, Savings, etc.)
  db.py            SQLite med WAL mode (swap-historikk)
  services/
    lanekassen.py      Scraper lanekassen.no for gjeldende renter
    seb.py             Henter live swap-renter fra SEB API
    finansportalen.py  Henter bankrenter, estimerer neste LK-rente
    cbonds.py          Bootstrap swap-historikk fra Cbonds
  templates/
    base.html          Layout (Tailwind CDN + HTMX + Chart.js)
    dashboard.html     Hovedside
    partials/          HTMX-partials (auto-refresh)
data/
  studielan.db       SQLite database (gitignored, opprettes automatisk)
```

**Stack:** FastAPI + Jinja2 + HTMX + Tailwind CDN + Chart.js + SQLite

Ingen frontend build step. Ingen JavaScript-rammeverk. Server-rendret med HTMX for live-oppdatering.

## Datakilder

| Kilde | Hva | Frekvens |
|-------|-----|----------|
| [Lanekassen](https://lanekassen.no) | Gjeldende renter (flytende + fast) | Hvert 5. min |
| [SEB](https://sfrapp.sfrprod.net) | Live NOK swap-renter (3/5/10 ar) | Hvert minutt |
| [Finansportalen](https://finansportalen.no) | Topp-5 bankrenter per bindingsperiode | Hvert 5. min |
| [Cbonds](https://cbonds.com) | Historiske swap-renter (bootstrap) | Manuelt |

Alle datakilder er offentlige APIer. Ingen API-nokler eller autentisering kreves.

## Hvordan funker det?

1. **Lanekassen setter renta** basert pa snitt topp-5 bankrenter - 0.15pp
2. **Estimert neste rente** = snitt topp-5 nominelle bankrenter na - 0.15pp
3. **Besparelse** = NPV av differansen mellom a binde na vs estimert neste rente
4. **Anbefaling** bruker estimert rateendring som primarsignal og swap-trend som bekreftelse

Soknadsvinduer: 10.-17. annenhver maned (feb, apr, jun, aug, okt, des).

## Bidra

PRs er velkomne! Alle PRs krever godkjenning for de merges til `main`.

Se [CONTRIBUTING.md](CONTRIBUTING.md) for detaljer.

## Lisens

MIT - se [LICENSE](LICENSE).

## Disclaimer

Ingen finansiell radgivning. Data fra tredjeparter kan inneholde feil. Bruk eget skjonn.
