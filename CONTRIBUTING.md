# Bidra til Studielan Rentekalkulator

Takk for at du vurderer a bidra!

## Kom i gang

```bash
git clone https://github.com/JorgenStensrudForte/studielan.git
cd studielan
uv sync --dev
uv run uvicorn app.main:app --reload --port 8000
```

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
7. Apne en Pull Request

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
