from datetime import date
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    port: int = 8000
    db_path: str = "data/studielan.db"
    default_loan_amount: int = 500_000
    default_remaining_years: int = 20
    lanekassen_margin: float = 0.15  # Lånekassen trekker fra 0.15pp

    # Lånekassen
    lanekassen_url: str = "https://lanekassen.no/nb-NO/gjeld-og-betaling/renter-og-gebyrer/historisk-renteutvikling2/"

    # Finansportalen
    finansportalen_url: str = "https://finans-api.forbrukerradet.no/bankprodukt/boliglan"

    # SEB swap rates
    seb_swap_url: str = "https://sebgroup.com/ssc/trading/fx-rates-bff/api/rates/swap"

    # Cbonds
    cbonds_base_url: str = "https://cbonds.com/api/indexes"
    cbonds_index_3y: int = 20273
    cbonds_index_5y: int = 20277
    cbonds_index_10y: int = 20287

    model_config = {"env_prefix": "STUDIELAN_"}


settings = Settings()


def effective_to_nominal(eff_pct: float) -> float:
    """Convert effective annual rate to nominal rate (monthly compounding).

    Finanstilsynet formula: nominal = 12 × ((1 + eff/100)^(1/12) - 1) × 100
    """
    return 12 * ((1 + eff_pct / 100) ** (1 / 12) - 1) * 100


# Søknadsvindu-måneder (annenhver: feb, apr, jun, aug, okt, des)
WINDOW_MONTHS = [2, 4, 6, 8, 10, 12]

TENOR_MAP = {
    "3 Yr": 3,
    "5 Yr": 5,
    "10 Yr": 10,
}

TENOR_LABELS = {
    "3 Yr": "3 år",
    "5 Yr": "5 år",
    "10 Yr": "10 år",
}

# Maps Lånekassen model attributes to swap tenor keys
TENOR_ATTRS = [
    ("fixed_3y", "3 Yr"),
    ("fixed_5y", "5 Yr"),
    ("fixed_10y", "10 Yr"),
]


def _window_for(year: int, month: int) -> tuple[date, date]:
    return (date(year, month, 10), date(year, month, 17))


def all_windows(year: int) -> list[tuple[date, date]]:
    return [_window_for(year, m) for m in WINDOW_MONTHS]


def current_window() -> tuple[date, date] | None:
    today = date.today()
    for start, end in all_windows(today.year):
        if start <= today <= end:
            return (start, end)
    return None


def next_window() -> tuple[date, date]:
    today = date.today()
    for start, end in all_windows(today.year):
        if today <= end:
            return (start, end)
    # Past last window this year → first window next year
    return _window_for(today.year + 1, WINDOW_MONTHS[0])


def days_until_next_window() -> int:
    cw = current_window()
    if cw:
        return 0
    nw = next_window()
    return (nw[0] - date.today()).days


OBSERVATION_MONTHS = [1, 3, 5, 7, 9, 11]

MONTHS_NO = {
    1: "januar", 2: "februar", 3: "mars", 4: "april",
    5: "mai", 6: "juni", 7: "juli", 8: "august",
    9: "september", 10: "oktober", 11: "november", 12: "desember",
}


def observation_schedule(today: date) -> dict:
    """Determine current, previous, and next observation periods.

    Returns dict with 'current' (if in obs month), 'previous', and 'next',
    each being a dict with obs/rate year/month/label, or None.
    """
    def _make(oy, om):
        rm = om + 2 if om + 2 <= 12 else om + 2 - 12
        ry = oy if om + 2 <= 12 else oy + 1
        label = f"{MONTHS_NO[rm]} {ry}"
        return {"obs_year": oy, "obs_month": om, "rate_year": ry, "rate_month": rm,
                "rate_label": label, "obs_label": f"{MONTHS_NO[om]} {oy}"}

    current = None
    previous = None
    nxt = None

    if today.month in OBSERVATION_MONTHS:
        current = _make(today.year, today.month)
        idx = OBSERVATION_MONTHS.index(today.month)
        # previous
        if idx > 0:
            previous = _make(today.year, OBSERVATION_MONTHS[idx - 1])
        else:
            previous = _make(today.year - 1, 11)
        # next
        if idx < len(OBSERVATION_MONTHS) - 1:
            nxt = _make(today.year, OBSERVATION_MONTHS[idx + 1])
        else:
            nxt = _make(today.year + 1, 1)
    else:
        # Between observation months
        found = False
        for i, m in enumerate(OBSERVATION_MONTHS):
            if m > today.month:
                nxt = _make(today.year, m)
                if i > 0:
                    previous = _make(today.year, OBSERVATION_MONTHS[i - 1])
                else:
                    previous = _make(today.year - 1, 11)
                found = True
                break
        if not found:
            previous = _make(today.year, 11)
            nxt = _make(today.year + 1, 1)

    return {"current": current, "previous": previous, "next": nxt}
