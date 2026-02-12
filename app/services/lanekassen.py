import httpx
from bs4 import BeautifulSoup

from app.config import settings
from app.models import LanekassenRate

MONTH_MAP = {
    "jan": 1, "feb": 2, "mars": 3, "mar": 3, "apr": 4,
    "mai": 5, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "okt": 10, "nov": 11, "des": 12,
}


def _parse_rate(text: str) -> float | None:
    text = text.strip().replace("%", "").replace(",", ".").strip()
    if not text or text == "-":
        return None
    return float(text)


async def fetch_rates() -> list[LanekassenRate]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(settings.lanekassen_url, timeout=15.0)
        resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table")
    if not table:
        return []

    tbody = table.find("tbody")
    if not tbody:
        rows = table.find_all("tr")[1:]  # skip header
    else:
        rows = tbody.find_all("tr")

    rates = []
    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        period_text = cells[0].get_text().strip()
        floating = _parse_rate(cells[1].get_text())
        if floating is None:
            continue

        rates.append(LanekassenRate(
            period=period_text,
            floating=floating,
            fixed_3y=_parse_rate(cells[2].get_text()),
            fixed_5y=_parse_rate(cells[3].get_text()),
            fixed_10y=_parse_rate(cells[4].get_text()),
        ))

    return rates
