# Studielån Rentekalkulator

## Prosjektinfo
Server-rendret FastAPI-app som viser om det lønner seg å binde renta på studielånet.
Ingen Supabase, ingen auth, ingen frontend build step.

## Kommandoer
- `uv sync` - installer avhengigheter
- `uv run uvicorn app.main:app --reload --port 8000` - kjør lokalt
- `uv run pytest tests/ -v` - kjør tester
- `docker compose up --build` - Docker

## Kodestil
- FastAPI async, Pydantic, tynne ruter → services
- Dataclasses for modeller (ikke Pydantic BaseModel for interne data)
- SQLite med WAL mode for swap rate-historikk
- Jinja2 + HTMX + Tailwind CDN for frontend
- httpx for HTTP-kall (async)

## Coolify (deploy)
- API: `http://77.42.36.80:8000`, app UUID `kswgg8ckcgo444gsowocg00k`
- Deploy: `source infra/coolify/.env && bash infra/coolify/coolify.sh deploy`
- Logs: `source infra/coolify/.env && bash infra/coolify/coolify.sh logs`
- Status: `source infra/coolify/.env && bash infra/coolify/coolify.sh status`

## Mappestruktur
- `app/main.py` - FastAPI app, routes, templates
- `app/config.py` - Settings, konstanter
- `app/models.py` - Dataclasses
- `app/db.py` - SQLite
- `app/services/` - Datakilder (lanekassen, seb, finansportalen, cbonds)
- `app/templates/` - Jinja2 templates
- `infra/coolify/` - Deploy script + .env (gitignored)
- `data/` - SQLite database (gitignored)
