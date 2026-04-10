"""Compute weekly (Wednesday) observations and monthly basisrente averages.

Finanstilsynet observes bank rates once per week (Wednesdays) and computes
the basisrente as the average of the weekly observations in the observation month.

We store daily snapshots and pick the closest data point to each Wednesday.
"""
from collections import defaultdict
from datetime import date, timedelta

from app.config import effective_to_nominal


def wednesdays_in_month(year: int, month: int) -> list[date]:
    """Return all Wednesdays in a given month."""
    d = date(year, month, 1)
    while d.weekday() != 2:  # 2 = Wednesday
        d += timedelta(days=1)
    result = []
    while d.month == month:
        result.append(d)
        d += timedelta(days=7)
    return result


def _pick_closest(target: date, rows: list[dict], max_days: int = 2) -> dict | None:
    """Find the daily snapshot closest to target date (prefer same day or day before)."""
    best = None
    best_dist = max_days + 1
    for row in rows:
        try:
            obs = date.fromisoformat(row["observed_date"])
        except (ValueError, TypeError, KeyError):
            continue
        dist = abs((obs - target).days)
        if dist < best_dist:
            best = row
            best_dist = dist
        elif dist == best_dist and best is not None:
            # Prefer the observation on or before the target
            if obs <= target:
                best = row
    return best if best_dist <= max_days else None


def compute_weekly_observations(
    year: int,
    month: int,
    daily_rows: list[dict],
) -> dict:
    """Compute Wednesday observations and monthly average from daily data.

    Args:
        year, month: The observation month.
        daily_rows: All rows from bank_rate_estimates for this month,
                    sorted by observed_date ASC.

    Returns:
        {
            "year": 2026, "month": 4, "month_label": "april 2026",
            "wednesdays_total": 4,
            "tenors": {
                "3 år": {
                    "weeks": [
                        {"week": 1, "date": "2026-04-01", "actual_date": "2026-04-01",
                         "avg_top5_eff": 5.594, "lk_eff": 5.444, "lk_nom": 5.313},
                        ...
                    ],
                    "monthly_avg": {
                        "avg_top5_eff": 5.590, "lk_eff": 5.440, "lk_nom": 5.310,
                        "week_count": 2,
                    },
                },
                ...
            },
        }
    """
    weds = wednesdays_in_month(year, month)
    today = date.today()

    MONTHS_NO = {
        1: "januar", 2: "februar", 3: "mars", 4: "april",
        5: "mai", 6: "juni", 7: "juli", 8: "august",
        9: "september", 10: "oktober", 11: "november", 12: "desember",
    }

    # Group daily rows by tenor
    by_tenor: dict[str, list[dict]] = defaultdict(list)
    for row in daily_rows:
        by_tenor[row["tenor"]].append(row)

    tenors = {}
    for tenor in ("3 år", "5 år", "10 år"):
        tenor_rows = by_tenor.get(tenor, [])
        weeks = []
        for i, wed in enumerate(weds, 1):
            if wed > today:
                break
            row = _pick_closest(wed, tenor_rows)
            if row is None:
                continue
            weeks.append({
                "week": i,
                "date": wed.isoformat(),
                "actual_date": row["observed_date"],
                "avg_top5_eff": row["avg_top5_effective"],
                "lk_eff": row["estimated_lk_effective"],
                "lk_nom": row["estimated_lk_nominal"],
            })

        monthly_avg = None
        if weeks:
            n = len(weeks)
            avg_eff = sum(w["avg_top5_eff"] for w in weeks) / n
            lk_eff = avg_eff - 0.15
            lk_nom = effective_to_nominal(lk_eff)
            monthly_avg = {
                "avg_top5_eff": round(avg_eff, 3),
                "lk_eff": round(lk_eff, 3),
                "lk_nom": round(lk_nom, 3),
                "week_count": n,
            }

        tenors[tenor] = {"weeks": weeks, "monthly_avg": monthly_avg}

    return {
        "year": year,
        "month": month,
        "month_label": f"{MONTHS_NO[month]} {year}",
        "wednesdays_total": len(weds),
        "wednesdays_observed": min(len(weds), max(
            (len(t["weeks"]) for t in tenors.values()), default=0
        )),
        "tenors": tenors,
    }
