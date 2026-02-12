import httpx
from datetime import date, datetime, timedelta

from app.config import settings
from app.models import SwapRate

INDEX_IDS = {
    "3 Yr": settings.cbonds_index_3y,
    "5 Yr": settings.cbonds_index_5y,
    "10 Yr": settings.cbonds_index_10y,
}

GRAPH_URL = settings.cbonds_base_url + "/{id}/getGraphicData/"
VALUE_URL = settings.cbonds_base_url + "/{id}/{date}/getValue/"


async def _fetch_true_value(client: httpx.AsyncClient, index_id: int, ref_date: date) -> float:
    url = VALUE_URL.format(id=index_id, date=ref_date.isoformat())
    resp = await client.get(url, timeout=10.0)
    resp.raise_for_status()
    data = resp.json()
    val = data.get("value", data)
    if isinstance(val, str) and val == "***":
        raise ValueError(f"Paywalled value for {ref_date}")
    return float(val)


async def _fetch_chart_data(
    client: httpx.AsyncClient, index_id: int, from_date: date, to_date: date,
) -> list[dict]:
    url = GRAPH_URL.format(id=index_id)
    resp = await client.post(
        url,
        data={"from": from_date.isoformat(), "to": to_date.isoformat()},
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=15.0,
    )
    resp.raise_for_status()
    return resp.json()


def _deobfuscate(chart_data: list[dict], true_value: float) -> list[dict]:
    if not chart_data:
        return []

    # Use last data point to calibrate (closest to today)
    ref_point = chart_data[-1]
    obfuscated = ref_point["value"]
    offset = round(obfuscated - true_value)

    return [
        {**p, "value": round(p["value"] - offset, 3)}
        for p in chart_data
    ]


def _parse_date(point: dict) -> date:
    if "date" in point and isinstance(point["date"], str):
        return date.fromisoformat(point["date"][:10])
    if "date" in point and isinstance(point["date"], dict):
        numeric = point["date"].get("numeric", 0)
        return datetime.fromtimestamp(numeric).date()
    # Fallback: date.numeric at top level
    numeric = point.get("date.numeric", point.get("dateNumeric", 0))
    return datetime.fromtimestamp(numeric).date()


async def fetch_history(days_back: int = 365) -> list[SwapRate]:
    to_date = date.today()
    from_date = to_date - timedelta(days=days_back)

    all_rates = []
    async with httpx.AsyncClient() as client:
        for tenor, index_id in INDEX_IDS.items():
            try:
                true_val = await _fetch_true_value(client, index_id, to_date)
            except (ValueError, httpx.HTTPError):
                # Try yesterday if today not available
                try:
                    true_val = await _fetch_true_value(client, index_id, to_date - timedelta(days=1))
                except (ValueError, httpx.HTTPError):
                    continue

            try:
                chart_data = await _fetch_chart_data(client, index_id, from_date, to_date)
            except httpx.HTTPError:
                continue

            clean_data = _deobfuscate(chart_data, true_val)

            for point in clean_data:
                try:
                    point_date = _parse_date(point)
                except (ValueError, KeyError, TypeError):
                    continue

                all_rates.append(SwapRate(
                    tenor=tenor,
                    rate=point["value"],
                    change_today=0.0,
                    observed_at=datetime.combine(point_date, datetime.min.time()),
                    source="cbonds",
                ))

    return all_rates
