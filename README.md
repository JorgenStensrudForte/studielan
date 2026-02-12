# Studielan Rentekalkulator

Dashboard som hjelper deg vurdere om du bor binde renta pa studielanet hos Lanekassen.

**Live:** [studielan.77-42-36-80.sslip.io](https://studielan.77-42-36-80.sslip.io/)

## Hva gjor den?

- Henter gjeldende renter fra **Lanekassen** (flytende + fast 3/5/10 ar)
- Henter live **swap-renter** fra SEB (markedets renteforventning)
- Henter topp-5 **bankrenter** fra Finansportalen per bindingsperiode
- **Estimerer neste Lanekassen-fastrente** (snitt topp-5 bankrenter - 0.15pp)
- Beregner **besparelse** ved a binde na vs vente til neste vindu
- Viser detaljert **NPV-utregning** iht. finansavtaleforskriften § 2-1
- Gir en **anbefaling** per tenor basert pa estimert rateendring + swap-trend

## Screenshots

Dashboardet viser:
- Lanekassen-renter med soknadsvinduer
- Swap-renter med 90-dagers historikk (graf)
- Banker og estimert neste LK-rente
- "Bind na vs vent" med klikkbar detaljert utregning
- Vurdering per tenor (BIND / VENT / USIKKER)

## Kom i gang

### Lokalt

```bash
# Installer avhengigheter
uv sync

# Kjor lokalt
uv run uvicorn app.main:app --reload --port 8000

# Kjor tester
uv run pytest tests/ -v
```

### Docker

```bash
# Kopier og fyll ut miljovaribler
cp .env.example .env
# Rediger .env med ditt passord-hash:
#   docker run --rm caddy caddy hash-password --plaintext "ditt-passord"

# Start
docker compose up --build
```

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

## Hvordan funker det?

1. **Lanekassen setter renta** basert pa snitt topp-5 bankrenter - 0.15pp
2. **Estimert neste rente** = snitt topp-5 bankrenter na - 0.15pp
3. **Besparelse** = NPV av differansen mellom a binde na vs estimert neste rente
4. **Anbefaling** bruker estimert rateendring som primarsignal og swap-trend som bekreftelse

Soknadsvinduer: 10.-17. annenhver maned (feb, apr, jun, aug, okt, des).

## Lisens

MIT - se [LICENSE](LICENSE).

## Disclaimer

Ingen finansiell radgivning. Data fra tredjeparter kan inneholde feil. Bruk eget skjonn.
