import httpx

from app.config import settings
from app.models import BankProduct, EstimatedRate

BASE_PARAMS = {
    "interestType": "Fast",
    "loanAmount": 3_000_000,
    "purchasePrice": 4_000_000,
    "paymentPeriod": 25,
    "age": 30,
    "loanType[0]": "standardlån",
    "isSalaryRequired": "false",
    "membershipType[0]": "None",
    "requiredProductTypes[0]": "None",
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
        tenor_products.sort(key=lambda p: p.effective_rate)
        result[years] = tenor_products[:top_n]

    return result


def estimate_next_lk_rates(
    products_by_tenor: dict[int, list[BankProduct]],
    current_lk: "LanekassenRate | None" = None,
) -> list[EstimatedRate]:
    """Estimate next Lånekassen rates: avg top-5 effective rate - 0.15pp."""
    from app.models import LanekassenRate  # avoid circular

    lk_attr_map = {3: "fixed_3y", 5: "fixed_5y", 10: "fixed_10y"}
    tenor_labels = {3: "3 år", 5: "5 år", 10: "10 år"}
    estimates = []

    for years in (3, 5, 10):
        products = products_by_tenor.get(years, [])
        top5 = products[:5]
        if not top5:
            continue

        avg = sum(p.effective_rate for p in top5) / len(top5)
        estimated_lk = round(avg - settings.lanekassen_margin, 3)

        current = None
        if current_lk:
            current = getattr(current_lk, lk_attr_map[years], None)

        diff = round(estimated_lk - current, 3) if current is not None else None

        estimates.append(EstimatedRate(
            tenor=tenor_labels[years],
            avg_top5=round(avg, 3),
            estimated_lk=estimated_lk,
            current_lk=current,
            diff=diff,
            bank_count=len(top5),
        ))

    return estimates
