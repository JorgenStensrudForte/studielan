"""Backfill historical bank fastrente data from Finansportalen's historical API.

API endpoints:
- GET /historical/mortgage/banks?types[0]=standardlån
- GET /historical/mortgage/products?bankIds[0]=X&types[0]=standardlån
- GET /historical/mortgage?loanAmount=1500000&paymentPeriod=30&productIds[0]=X&purchasePrice=3000000

The historical API returns effectiveInterestRate only (not nominal).
We store effective rates and set nominal = 0 for backfilled data.
"""
import logging
import statistics
from collections import defaultdict
from datetime import datetime, timedelta

import httpx

from app.config import effective_to_nominal
from app.models import BankProduct, EstimatedRate

logger = logging.getLogger(__name__)

HISTORICAL_BASE = "https://finans-api.forbrukerradet.no/historical/mortgage"

# Standard fastrente product IDs per bank (no "ung", "LO", "Premium", "SAGA" etc.)
# Matches the banks Finanstilsynet uses for basisrente calculation.
PRODUCT_MAP = {
    "Sbanken (DNB Bank ASA)": {"3": 153084, "5": 153085, "10": 153086},
    "Bien Sparebank ASA": {"3": 156119, "5": 156118, "10": 156117},
    "JBF Bank og Forsikring": {"3": 155782, "5": 155783},
    "Sparebanken Møre": {"3": 154310, "5": 154305, "10": 154306},
    "SpareBank 1 Helgeland": {"3": 153313, "5": 153535},
    "SpareBank 1 Østfold Akershus": {"3": 155675, "5": 155676},
    "SpareBank 1 Nord-Norge": {"3": 156112, "5": 156111, "10": 156113},
    "Sparebanken Øst": {"3": 156133, "5": 156134, "10": 156135},
    "NORDEA BANK ABP, FILIAL I NORGE": {"3": 155955, "5": 155950, "10": 155956},
    "KLP Banken AS": {"3": 156748, "5": 156749, "10": 156750},
    "Storebrand Bank ASA": {"3": 156978, "5": 156977, "10": 156976},
    "SpareBank 1 Sogn og Fjordane": {"3": 16202, "5": 156008, "10": 156007},
    "SpareBank 1 Gudbrandsdal": {"3": 156463, "5": 156464, "10": 156465},
    "SpareBank 1 SMN": {"3": 156682, "5": 156681, "10": 156680},
    "Romerike Sparebank": {"3": 157164, "5": 157163, "10": 156444},
}

ALL_PRODUCT_IDS = []
for tenors in PRODUCT_MAP.values():
    ALL_PRODUCT_IDS.extend(tenors.values())

# Reverse map: product_id → (bank_name, bound_years)
_PRODUCT_ID_MAP: dict[int, tuple[str, int]] = {}
for bank, tenors in PRODUCT_MAP.items():
    for tenor_str, pid in tenors.items():
        _PRODUCT_ID_MAP[pid] = (bank, int(tenor_str))


async def fetch_historical_products() -> list[dict]:
    """Fetch historical data for all tracked products from Finansportalen."""
    params = {"loanAmount": 1_500_000, "paymentPeriod": 30, "purchasePrice": 3_000_000}
    for i, pid in enumerate(ALL_PRODUCT_IDS):
        params[f"productIds[{i}]"] = pid

    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(HISTORICAL_BASE, params=params, timeout=30.0)
        resp.raise_for_status()

    return resp.json()


def _build_timeline(
    raw_products: list[dict],
    since: str = "2024-12-01",
) -> dict[str, dict[int, list[BankProduct]]]:
    """Build a date → {bound_years: [BankProduct]} map from stepped historical data.

    For each date where any bank changed their rate, we compute
    what every bank's rate was at that point (carry forward).
    """
    # Parse all data into per-product timelines
    # product_id → list of (date_str, effective_rate)
    product_timelines: dict[int, list[tuple[str, float]]] = {}
    all_change_dates: set[str] = set()

    for product in raw_products:
        pid = product["id"]
        if pid not in _PRODUCT_ID_MAP:
            continue
        timeline = []
        for point in product.get("data", []):
            date_from = point["dateFrom"][:10]  # "YYYY-MM-DD"
            rate = point["effectiveInterestRate"]
            if date_from >= since:
                timeline.append((date_from, rate))
                all_change_dates.add(date_from)
        product_timelines[pid] = timeline

    if not all_change_dates:
        return {}

    # Sort all dates
    sorted_dates = sorted(all_change_dates)

    # For each date, determine each product's current rate (latest rate <= date)
    # result: date → {bound_years: [BankProduct]}
    result: dict[str, dict[int, list[BankProduct]]] = {}

    # Current rate per product (carry forward)
    current_rates: dict[int, float] = {}
    # Index into each product's timeline
    timeline_idx: dict[int, int] = {pid: 0 for pid in product_timelines}

    for date_str in sorted_dates:
        # Advance each product's timeline up to this date
        for pid, timeline in product_timelines.items():
            idx = timeline_idx[pid]
            while idx < len(timeline) and timeline[idx][0] <= date_str:
                current_rates[pid] = timeline[idx][1]
                idx += 1
            timeline_idx[pid] = idx

        # Build products_by_tenor for this date
        by_tenor: dict[int, list[BankProduct]] = defaultdict(list)
        for pid, rate in current_rates.items():
            if rate < 0.5:  # Skip bogus 0% rates from Finansportalen
                continue
            bank_name, bound_years = _PRODUCT_ID_MAP[pid]
            by_tenor[bound_years].append(BankProduct(
                bank=bank_name,
                product_name=f"fastrente {bound_years} år (historisk)",
                nominal_rate=0.0,  # Historical API doesn't provide nominal
                effective_rate=rate,
                bound_years=bound_years,
                period=f"{bound_years} år",
            ))

        # Sort by effective rate and keep all (we'll take top 5 later)
        for years in by_tenor:
            by_tenor[years].sort(key=lambda p: p.effective_rate)

        result[date_str] = dict(by_tenor)

    return result


def compute_historical_estimates(
    timeline: dict[str, dict[int, list[BankProduct]]],
    top_n: int = 5,
    margin: float = 0.15,
) -> list[tuple[str, dict[int, list[BankProduct]], list[EstimatedRate]]]:
    """For each date in the timeline, compute top-N estimates.

    Returns list of (date, products_by_tenor, estimates) tuples.
    """
    tenor_labels = {3: "3 år", 5: "5 år", 10: "10 år"}
    results = []

    for date_str, products_by_tenor in timeline.items():
        estimates = []
        top_products: dict[int, list[BankProduct]] = {}

        for years in (3, 5, 10):
            products = products_by_tenor.get(years, [])
            top = products[:top_n]
            if not top:
                continue

            top_products[years] = top
            eff_rates = [p.effective_rate for p in top]
            avg_eff = sum(eff_rates) / len(eff_rates)
            lk_eff = avg_eff - margin
            lk_nom = effective_to_nominal(lk_eff)
            std_dev = round(statistics.stdev(eff_rates), 3) if len(eff_rates) >= 2 else 0.0

            estimates.append(EstimatedRate(
                tenor=tenor_labels[years],
                avg_top5_effective=round(avg_eff, 3),
                estimated_lk=round(lk_nom, 3),
                estimated_lk_effective=round(lk_eff, 3),
                current_lk=None,
                diff=None,
                bank_count=len(top),
                std_dev=std_dev,
            ))

        results.append((date_str, top_products, estimates))

    return results
