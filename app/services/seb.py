import httpx
from datetime import datetime

from app.config import settings
from app.models import SwapRate

RELEVANT_TENORS = {"3 Yr", "5 Yr", "10 Yr"}


async def fetch_swap_rates() -> list[SwapRate]:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            settings.seb_swap_url,
            params={"currency": "NOK"},
            timeout=10.0,
        )
        resp.raise_for_status()

    data = resp.json()
    rates = []
    for row in data["rows"]:
        cells = row["data"]
        tenor = cells[0]["value"]
        if tenor not in RELEVANT_TENORS:
            continue

        price = float(cells[1]["value"])
        change = float(cells[2]["value"])
        time_str = cells[3]["value"]
        date_str = cells[4]["value"]

        ts = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M")
        rates.append(SwapRate(
            tenor=tenor,
            rate=price,
            change_today=change,
            observed_at=ts,
            source="seb",
        ))

    return rates
