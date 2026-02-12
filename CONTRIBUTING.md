# Bidra til Studielan Rentekalkulator

Takk for at du vurderer a bidra!

## PR-policy

Alle endringer til `main` ma ga via Pull Request. PRs krever **minst 1 godkjenning** fra maintainer for de kan merges. Direkte push til `main` er ikke tillatt.

## Kom i gang

```bash
git clone https://github.com/JorgenStensrudForte/studielan.git
cd studielan
uv sync --dev
uv run uvicorn app.main:app --reload --port 8000
```

Apne [http://localhost:8000](http://localhost:8000) — dashboardet fungerer umiddelbart.

### Backfill av databasen

Swap-historikk lagres i `data/studielan.db` (SQLite, opprettes automatisk). Uten historikk mangler swap-graf og trendanalyse.

For a fylle databasen med ~365 dager historikk:

```bash
curl -X POST http://localhost:8000/api/bootstrap
```

Eller klikk "Last ned historikk fra Cbonds"-knappen pa dashboardet.

## Kjor tester

```bash
uv run pytest tests/ -v
```

Alle tester ma bestaes for en PR kan merges.

## Kodestil

- **Python**: Async FastAPI, dataclasses for modeller, httpx for HTTP
- **Frontend**: Jinja2 + HTMX + Tailwind CDN + Chart.js (ingen build step)
- **Database**: SQLite med WAL mode via aiosqlite
- Hold koden enkel. Ingen unodvendige abstraksjoner.

## Slik bidrar du

1. Fork repoet
2. Lag en branch (`git checkout -b min-feature`)
3. Gjor endringene dine
4. Kjor testene (`uv run pytest tests/ -v`)
5. Commit (`git commit -m "kort beskrivelse"`)
6. Push (`git push origin min-feature`)
7. Apne en Pull Request — den vil bli reviewed og godkjent for merge

## Hva kan du bidra med?

- Nye datakilder for renter
- Bedre estimeringsmodeller
- UI-forbedringer
- Tester
- Feilrettinger
- Dokumentasjon

## Retningslinjer

- Hold PRer sma og fokuserte
- Beskriv hva og hvorfor i PR-beskrivelsen
- Ikke commit `.env` eller database-filer
- Bruk norske variabelnavn der det gir mening (rente, belop, etc.)
