"""Build Finanstilsynet-style weekly observation data.

Observations happen on Wednesdays during observation months (odd months).
For each Wednesday, we pick the top 5 banks by effective rate from the
closest available daily snapshot. The basisrente is the mean of weekly averages.
"""
from collections import defaultdict
from datetime import date, timedelta

from app.config import effective_to_nominal, MONTHS_NO, observation_schedule
from app import db


def _wednesdays_in_month(year: int, month: int) -> list[date]:
    d = date(year, month, 1)
    while d.weekday() != 2:
        d += timedelta(days=1)
    result = []
    while d.month == month:
        result.append(d)
        d += timedelta(days=7)
    return result


def _pick_closest_date(target: date, available_dates: list[str], max_days: int = 3) -> str | None:
    """Find the closest available date string to the target."""
    best = None
    best_dist = max_days + 1
    for ds in available_dates:
        try:
            d = date.fromisoformat(ds)
        except (ValueError, TypeError):
            continue
        dist = abs((d - target).days)
        if dist < best_dist or (dist == best_dist and d <= target):
            best = ds
            best_dist = dist
    return best if best_dist <= max_days else None


async def build_observation(period: dict, top_n: int = 5) -> dict:
    """Build full observation data for a period (like the Finanstilsynet PDF).

    Args:
        period: dict with obs_year, obs_month, rate_label, obs_label
        top_n: number of banks per week

    Returns structured data for template rendering.
    """
    oy = period["obs_year"]
    om = period["obs_month"]
    today = date.today()
    weds = _wednesdays_in_month(oy, om)

    tenors_data = {}
    for years in (3, 5, 10):
        # Get all bank products for this tenor in the observation month
        products_by_date = await db.get_bank_products_for_month(years, oy, om)
        available_dates = sorted(products_by_date.keys())

        weeks = []
        weekly_avgs = []
        for i, wed in enumerate(weds, 1):
            if wed > today:
                break
            closest = _pick_closest_date(wed, available_dates)
            if closest is None:
                continue

            # Sort by effective rate and take top N
            prods = sorted(products_by_date[closest], key=lambda p: p["effective_rate"])[:top_n]
            if not prods:
                continue

            avg_eff = sum(p["effective_rate"] for p in prods) / len(prods)
            weekly_avgs.append(avg_eff)

            weeks.append({
                "week": i,
                "target_date": wed.strftime("%d.%m.%Y"),
                "actual_date": closest,
                "banks": [
                    {
                        "bank": p["bank"],
                        "product": p["product_name"],
                        "effective_rate": p["effective_rate"],
                    }
                    for p in prods
                ],
                "avg_eff": round(avg_eff, 3),
            })

        basisrente = None
        lk_eff = None
        lk_nom = None
        if weekly_avgs:
            basisrente = round(sum(weekly_avgs) / len(weekly_avgs), 3)
            lk_eff = round(basisrente - 0.15, 3)
            lk_nom = round(effective_to_nominal(lk_eff), 3)

        tenors_data[years] = {
            "title": f"Fast rente {years} år {period['rate_label']}",
            "weeks": weeks,
            "basisrente": basisrente,
            "lk_eff": lk_eff,
            "lk_nom": lk_nom,
            "weeks_observed": len(weeks),
        }

    return {
        "rate_label": period["rate_label"],
        "obs_label": period["obs_label"],
        "obs_year": oy,
        "obs_month": om,
        "wednesdays_total": len(weds),
        "wednesdays_past": sum(1 for w in weds if w <= today),
        "tenors": tenors_data,
        "has_data": any(t["weeks"] for t in tenors_data.values()),
    }


async def get_observations_for_dashboard() -> dict:
    """Get observation data for the dashboard.

    Returns current (if in obs month), previous completed, and next upcoming.
    """
    today = date.today()
    schedule = observation_schedule(today)

    result = {
        "schedule": schedule,
        "current_obs": None,
        "previous_obs": None,
    }

    # If we're in an observation month, build current
    if schedule["current"]:
        result["current_obs"] = await build_observation(schedule["current"])

    # Always build previous (the completed one)
    if schedule["previous"]:
        result["previous_obs"] = await build_observation(schedule["previous"])

    return result
