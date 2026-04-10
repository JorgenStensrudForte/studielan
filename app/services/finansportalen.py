import statistics

import httpx

from app.config import settings, effective_to_nominal
from app.models import BankProduct, EstimatedRate

# Finanstilsynets parametere for basisrente-beregning:
# 1,5M lån, 3M bolig (50% belåning), 30 år nedbetalingstid, alder 45
BASE_PARAMS = {
    "interestType": "Fast",
    "loanAmount": 1_500_000,
    "purchasePrice": 3_000_000,
    "paymentPeriod": 30,
    "age": 45,
    "loanType[0]": "standardlån",
    "isSalaryRequired": "false",
    "membershipType[0]": "None",
}


async def _fetch_all_fixed() -> list[BankProduct]:
    async with httpx.AsyncClient(follow_redirects=True) as client:
        resp = await client.get(
            settings.finansportalen_url,
            params=BASE_PARAMS,
            timeout=15.0,
        )
        resp.raise_for_status()

    products = []
    for item in resp.json():
        nominal = item.get("nominalInterestRate", 0)
        effective = item.get("effectiveInterestRate", 0)
        product = item.get("product", {})
        bound_years = (
            item.get("interestRateBoundNumberOfYears")
            or product.get("interestRateBoundNumberOfYears")
            or 0
        )
        if not nominal or not bound_years:
            continue

        products.append(BankProduct(
            bank=item.get("companyName", ""),
            product_name=item.get("name", ""),
            nominal_rate=float(nominal),
            effective_rate=float(effective),
            bound_years=int(bound_years),
            period=f"{bound_years} år",
        ))

    return products


async def fetch_products_by_tenor(top_n: int = 5) -> dict[int, list[BankProduct]]:
    """Returns top_n products per binding period {3: [...], 5: [...], 10: [...]}."""
    all_products = await _fetch_all_fixed()

    by_tenor: dict[int, list[BankProduct]] = {}
    for p in all_products:
        by_tenor.setdefault(p.bound_years, []).append(p)

    result = {}
    for years in (3, 5, 10):
        tenor_products = by_tenor.get(years, [])
        # Finanstilsynet rangerer etter effektiv rente
        tenor_products.sort(key=lambda p: p.effective_rate)
        result[years] = tenor_products[:top_n]

    return result


def estimate_next_lk_rates(
    products_by_tenor: dict[int, list[BankProduct]],
    current_lk: "LanekassenRate | None" = None,
) -> list[EstimatedRate]:
    """Estimate next Lånekassen rates using Finanstilsynets methodology.

    1. Average top-5 effective rates
    2. Subtract 0.15pp → LK effective rate
    3. Convert effective → nominal for comparison with current LK rates
    """
    from app.models import LanekassenRate  # avoid circular

    lk_attr_map = {3: "fixed_3y", 5: "fixed_5y", 10: "fixed_10y"}
    tenor_labels = {3: "3 år", 5: "5 år", 10: "10 år"}
    estimates = []

    for years in (3, 5, 10):
        products = products_by_tenor.get(years, [])
        top5 = products[:5]
        if not top5:
            continue

        eff_rates = [p.effective_rate for p in top5]
        avg_eff = sum(eff_rates) / len(eff_rates)
        lk_eff = avg_eff - settings.lanekassen_margin
        lk_nom = effective_to_nominal(lk_eff)
        std_dev = round(statistics.stdev(eff_rates), 3) if len(eff_rates) >= 2 else 0.0

        current = None
        if current_lk:
            current = getattr(current_lk, lk_attr_map[years], None)

        diff = round(round(lk_nom, 3) - current, 3) if current is not None else None

        estimates.append(EstimatedRate(
            tenor=tenor_labels[years],
            avg_top5_effective=round(avg_eff, 3),
            estimated_lk=round(lk_nom, 3),
            estimated_lk_effective=round(lk_eff, 3),
            current_lk=current,
            diff=diff,
            bank_count=len(top5),
            std_dev=std_dev,
        ))

    return estimates
