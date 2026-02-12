import logging
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import FastAPI, Request, Query
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

from app.config import (
    settings, current_window, next_window, days_until_next_window,
    TENOR_LABELS, TENOR_MAP, TENOR_ATTRS,
)
from app.models import LanekassenRate, Savings, Signal, TenorSignal, EstimatedRate
from app import db
from app.services import seb, lanekassen, finansportalen, cbonds

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_BASE_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(title="Studielån Rentekalkulator", lifespan=lifespan)


# --- Helpers ---

def _estimate_risk(est: EstimatedRate, years: int) -> str:
    """Data-driven risk: bank rate spread + sample size + tenor length."""
    score = 0
    # Spredning i bankrenter (høy std_dev = usikkert estimat)
    if est.std_dev > 0.25:
        score += 2
    elif est.std_dev > 0.10:
        score += 1
    # Få banker i grunnlaget = mindre pålitelig
    if est.bank_count < 3:
        score += 2
    elif est.bank_count < 5:
        score += 1
    # Lengre binding = mer kan endre seg
    if years >= 10:
        score += 1
    return "lav" if score <= 1 else "middels" if score <= 3 else "høy"


def _compute_savings(
    lk: LanekassenRate,
    loan_amount: int,
    estimates: list[EstimatedRate],
) -> list[Savings]:
    """Compare current LK fixed rate vs estimated next LK fixed rate.

    Positive diff = next rate HIGHER → bind now saves money.
    Negative diff = next rate LOWER → waiting saves money.
    """
    est_by_label = {e.tenor: e for e in estimates}
    results = []
    for attr, tenor_key in TENOR_ATTRS:
        fixed = getattr(lk, attr)
        if fixed is None:
            continue
        label = TENOR_LABELS[tenor_key]
        est = est_by_label.get(label)
        if est is None:
            continue
        years = TENOR_MAP[tenor_key]
        annual_diff = (est.estimated_lk - fixed) / 100 * loan_amount
        results.append(Savings(
            tenor=label,
            fixed_rate=fixed,
            estimated_next_rate=est.estimated_lk,
            loan_amount=loan_amount,
            annual_diff=round(annual_diff),
            total_diff=round(annual_diff * years),
            years=years,
            bind_now=annual_diff > 0,
            risk=_estimate_risk(est, years),
        ))
    return results


def _tenor_signal(
    tenor_key: str,
    lk: LanekassenRate | None,
    lk_attr: str,
    swap_history: list[dict],
    estimated: EstimatedRate | None,
) -> TenorSignal:
    """Analyze a single tenor and produce a signal.

    Primary signal: est_diff = estimated next LK fastrente - current LK fastrente.
      Positive → next period more expensive → bind now to lock in cheaper rate.
      Negative → next period cheaper → wait for next window.

    Confirmation: swap trend shows market rate direction over last ~90 days.
      Rising swap → market expects higher rates → confirms bind.
      Falling swap → market expects lower rates → confirms wait.
    """
    label = TENOR_LABELS[tenor_key]
    reasons = []

    # Current fixed rate
    current_rate = None
    if lk:
        current_rate = getattr(lk, lk_attr)

    # Estimated next LK fastrente
    est_next = estimated.estimated_lk if estimated else None
    est_diff = estimated.diff if estimated else None

    # Swap trend from historical data
    swap_trend = None
    swap_days = 0
    has_trend = False
    if len(swap_history) >= 2:
        newest_rate = swap_history[-1]["rate"]
        oldest_rate = swap_history[0]["rate"]
        swap_trend = round(newest_rate - oldest_rate, 3)
        try:
            newest_dt = datetime.fromisoformat(swap_history[-1]["observed_at"])
            oldest_dt = datetime.fromisoformat(swap_history[0]["observed_at"])
            swap_days = (newest_dt - oldest_dt).days
        except (ValueError, KeyError):
            swap_days = len(swap_history)
        has_trend = swap_days >= 14

    # --- Build reasons (observations) ---

    # Rate comparison: current vs estimated next
    if current_rate is not None and est_next is not None and est_diff is not None:
        if est_diff > 0.05:
            reasons.append(
                f"Nå: {current_rate:.3f}% → Est. neste: {est_next:.3f}% "
                f"(+{est_diff:.3f}pp) — bind nå er billigere enn å vente"
            )
        elif est_diff < -0.05:
            reasons.append(
                f"Nå: {current_rate:.3f}% → Est. neste: {est_next:.3f}% "
                f"({est_diff:.3f}pp) — neste periode forventes billigere"
            )
        else:
            reasons.append(
                f"Nå: {current_rate:.3f}% → Est. neste: {est_next:.3f}% "
                f"({est_diff:+.3f}pp) — omtrent uendret"
            )
    elif current_rate is not None:
        reasons.append(f"Nåværende fastrente: {current_rate:.3f}% (mangler estimat for neste)")

    # Swap trend observation
    if has_trend:
        period_label = f"siste {swap_days}d" if swap_days < 80 else "siste 90d"
        if swap_trend > 0.10:
            reasons.append(f"Swap {label} steg {swap_trend:+.2f}pp {period_label} — markedet forventer høyere renter")
        elif swap_trend < -0.10:
            reasons.append(f"Swap {label} falt {swap_trend:+.2f}pp {period_label} — markedet forventer lavere renter")
        else:
            reasons.append(f"Swap {label} stabil ({swap_trend:+.2f}pp {period_label})")
    elif len(swap_history) < 2:
        reasons.append(f"Mangler swap-historikk for {label} — kan ikke vurdere trend")

    # --- Decision logic ---
    # Primary: estimated rate diff
    est_bind = est_diff is not None and est_diff > 0.05  # next period dearer
    est_wait = est_diff is not None and est_diff < -0.05  # next period cheaper
    est_neutral = est_diff is not None and not est_bind and not est_wait

    # Confirmation: swap trend
    swap_rising = has_trend and swap_trend > 0.10
    swap_falling = has_trend and swap_trend < -0.10

    def _make(rec, color):
        return TenorSignal(
            tenor=label, recommendation=rec, color=color,
            current_rate=current_rate, estimated_next=est_next,
            est_diff=est_diff, swap_trend=swap_trend,
            swap_trend_days=swap_days, reasons=reasons,
        )

    # No estimate data at all → can't recommend
    if est_diff is None:
        if has_trend and swap_rising:
            reasons.append("Swap stiger, men mangler rateestimat — vurder å binde")
            return _make("USIKKER", "yellow")
        reasons.append("Utilstrekkelig data for anbefaling")
        return _make("USIKKER", "yellow")

    # 1. Est diff says BIND + swap confirms or is neutral → BIND
    if est_bind and (swap_rising or not swap_falling):
        return _make("BIND", "green")

    # 2. Est diff says BIND but swap contradicts → USIKKER
    if est_bind and swap_falling:
        reasons.append("Estimat peker opp, men swap-trend peker ned — motstridende signaler")
        return _make("USIKKER", "yellow")

    # 3. Est diff says WAIT + swap confirms or is neutral → VENT
    if est_wait and (swap_falling or not swap_rising):
        return _make("VENT", "red")

    # 4. Est diff says WAIT but swap contradicts → USIKKER
    if est_wait and swap_rising:
        reasons.append("Estimat peker ned, men swap-trend peker opp — motstridende signaler")
        return _make("USIKKER", "yellow")

    # 5. Neutral est diff — use swap as tiebreaker
    if est_neutral:
        if swap_rising:
            reasons.append("Rateestimat omtrent uendret, men swap stiger — kan lønne seg å binde")
            return _make("BIND", "green")
        if swap_falling:
            reasons.append("Rateestimat omtrent uendret, men swap faller — kan lønne seg å vente")
            return _make("VENT", "red")
        return _make("USIKKER", "yellow")


def _recommend(
    lk: LanekassenRate | None,
    swap_history: dict[str, list[dict]],
    estimates: list[EstimatedRate],
) -> Signal:
    """Produce per-tenor signals and an overall recommendation.

    Picks best tenor by highest est_diff (= most savings from binding now).
    """
    est_by_label = {e.tenor: e for e in estimates}
    per_tenor = []

    for attr, tenor_key in TENOR_ATTRS:
        label = TENOR_LABELS[tenor_key]
        history = swap_history.get(tenor_key, [])
        estimated = est_by_label.get(label)
        ts = _tenor_signal(tenor_key, lk, attr, history, estimated)
        per_tenor.append(ts)

    bind_tenors = [t for t in per_tenor if t.recommendation == "BIND"]
    wait_tenors = [t for t in per_tenor if t.recommendation == "VENT"]
    unsure_tenors = [t for t in per_tenor if t.recommendation == "USIKKER"]

    reasons = []

    if bind_tenors:
        # Prefer tenor with highest est_diff (biggest savings from binding now)
        best = max(bind_tenors, key=lambda t: t.est_diff if t.est_diff is not None else -999)
        if best.est_diff is not None:
            reasons.append(f"Beste binding: {best.tenor} (neste rente est. {best.est_diff:+.3f}pp høyere)")
        else:
            reasons.append(f"Beste binding: {best.tenor}")
        for t in bind_tenors:
            reasons.extend(t.reasons[:1])
        return Signal(
            recommendation=f"BIND {best.tenor.upper()}",
            color="green",
            best_tenor=best.tenor,
            reasons=reasons,
            per_tenor=per_tenor,
        )

    if wait_tenors:
        reasons.append("Estimert neste fastrente er lavere — vent til neste vindu")
        for t in wait_tenors:
            reasons.extend(t.reasons[:1])
        return Signal(
            recommendation="VENT",
            color="red",
            best_tenor=None,
            reasons=reasons,
            per_tenor=per_tenor,
        )

    if unsure_tenors:
        reasons.append("Motstridende signaler eller utilstrekkelig data")
        for t in unsure_tenors:
            reasons.extend(t.reasons[:1])
        return Signal(
            recommendation="USIKKER",
            color="yellow",
            best_tenor=None,
            reasons=reasons,
            per_tenor=per_tenor,
        )

    # Fallback (shouldn't normally happen)
    reasons.append("Ingen sterke signaler i noen retning")
    return Signal(
        recommendation="USIKKER",
        color="yellow",
        best_tenor=None,
        reasons=reasons,
        per_tenor=per_tenor,
    )


async def _fetch_all_data(loan_amount: int) -> dict:
    lk_rates: list[LanekassenRate] = []
    swap_rates = []
    products_by_tenor: dict[int, list] = {}
    has_swap_history = False

    try:
        lk_rates = await lanekassen.fetch_rates()
    except Exception as e:
        logger.error(f"Lånekassen fetch failed: {e}")

    try:
        swap_rates = await seb.fetch_swap_rates()
        await db.insert_swap_rates(swap_rates)
    except Exception as e:
        logger.error(f"SEB fetch failed: {e}")

    try:
        products_by_tenor = await finansportalen.fetch_products_by_tenor(top_n=5)
    except Exception as e:
        logger.error(f"Finansportalen fetch failed: {e}")

    lk_current = lk_rates[0] if lk_rates else None

    # Swap history from DB
    swap_history = {}
    for tenor in ["3 Yr", "5 Yr", "10 Yr"]:
        swap_history[tenor] = await db.get_swap_history(tenor, days=90)
        if len(swap_history[tenor]) >= 2:
            has_swap_history = True

    # Estimated next Lånekassen rates
    estimates = finansportalen.estimate_next_lk_rates(products_by_tenor, lk_current)

    # Savings
    savings = _compute_savings(lk_current, loan_amount, estimates) if lk_current else []

    # Recommendation
    signal = _recommend(lk_current, swap_history, estimates)

    # Application window
    cw = current_window()
    nw = next_window()
    days_to_window = days_until_next_window()

    return {
        "lanekassen": lk_current,
        "lanekassen_all": lk_rates[:6],
        "swap_rates": swap_rates,
        "swap_history": swap_history,
        "has_swap_history": has_swap_history,
        "products_by_tenor": products_by_tenor,
        "estimates": estimates,
        "savings": savings,
        "signal": signal,
        "loan_amount": loan_amount,
        "current_window": cw,
        "next_window": nw,
        "days_to_window": days_to_window,
        "today": date.today(),
    }


# --- Routes ---

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, belop: int = Query(default=settings.default_loan_amount)):
    data = await _fetch_all_data(belop)
    return templates.TemplateResponse("dashboard.html", {"request": request, **data})


@app.get("/api/dashboard")
async def api_dashboard(belop: int = Query(default=settings.default_loan_amount)):
    data = await _fetch_all_data(belop)
    result = {
        "loan_amount": data["loan_amount"],
        "today": data["today"].isoformat(),
        "lanekassen": {
            "period": data["lanekassen"].period,
            "floating": data["lanekassen"].floating,
            "fixed_3y": data["lanekassen"].fixed_3y,
            "fixed_5y": data["lanekassen"].fixed_5y,
            "fixed_10y": data["lanekassen"].fixed_10y,
        } if data["lanekassen"] else None,
        "swap_rates": [
            {"tenor": r.tenor, "rate": r.rate, "change_today": r.change_today}
            for r in data["swap_rates"]
        ],
        "products_by_tenor": {
            str(years): [
                {"bank": p.bank, "nominal_rate": p.nominal_rate, "effective_rate": p.effective_rate, "period": p.period}
                for p in products
            ]
            for years, products in data["products_by_tenor"].items()
        },
        "estimates": [
            {"tenor": e.tenor, "avg_top5": e.avg_top5, "estimated_lk": e.estimated_lk,
             "current_lk": e.current_lk, "diff": e.diff, "std_dev": e.std_dev}
            for e in data["estimates"]
        ],
        "savings": [
            {"tenor": s.tenor, "fixed_rate": s.fixed_rate, "estimated_next_rate": s.estimated_next_rate,
             "annual_diff": s.annual_diff, "total_diff": s.total_diff, "bind_now": s.bind_now, "risk": s.risk}
            for s in data["savings"]
        ],
        "signal": {
            "recommendation": data["signal"].recommendation,
            "color": data["signal"].color,
            "best_tenor": data["signal"].best_tenor,
            "reasons": data["signal"].reasons,
            "per_tenor": [
                {"tenor": t.tenor, "recommendation": t.recommendation, "color": t.color,
                 "current_rate": t.current_rate, "estimated_next": t.estimated_next,
                 "est_diff": t.est_diff, "swap_trend": t.swap_trend}
                for t in data["signal"].per_tenor
            ],
        },
    }
    return JSONResponse(result)


@app.get("/api/swap-history")
async def api_swap_history(tenor: str = "3 Yr", days: int = 90):
    history = await db.get_swap_history(tenor, days)
    return JSONResponse(history)


# --- HTMX Partials ---

@app.get("/partials/lanekassen", response_class=HTMLResponse)
async def partial_lanekassen(request: Request):
    try:
        rates = await lanekassen.fetch_rates()
        lk = rates[0] if rates else None
    except Exception:
        lk = None
    return templates.TemplateResponse("partials/lanekassen.html", {"request": request, "lanekassen": lk})


@app.get("/partials/swap", response_class=HTMLResponse)
async def partial_swap(request: Request):
    try:
        rates = await seb.fetch_swap_rates()
        await db.insert_swap_rates(rates)
    except Exception:
        rates = []

    swap_history = {}
    for tenor in ["3 Yr", "5 Yr", "10 Yr"]:
        swap_history[tenor] = await db.get_swap_history(tenor, days=90)

    return templates.TemplateResponse("partials/swap_rates.html", {
        "request": request,
        "swap_rates": rates,
        "swap_history": swap_history,
    })


@app.get("/partials/banker", response_class=HTMLResponse)
async def partial_banker(request: Request):
    try:
        lk_rates = await lanekassen.fetch_rates()
        lk = lk_rates[0] if lk_rates else None
    except Exception:
        lk = None

    try:
        products_by_tenor = await finansportalen.fetch_products_by_tenor(top_n=5)
    except Exception:
        products_by_tenor = {}

    estimates = finansportalen.estimate_next_lk_rates(products_by_tenor, lk)

    return templates.TemplateResponse("partials/banker.html", {
        "request": request,
        "products_by_tenor": products_by_tenor,
        "estimates": estimates,
    })


@app.get("/partials/besparelse", response_class=HTMLResponse)
async def partial_besparelse(request: Request, belop: int = Query(default=settings.default_loan_amount)):
    try:
        rates = await lanekassen.fetch_rates()
        lk = rates[0] if rates else None
    except Exception:
        lk = None

    try:
        products_by_tenor = await finansportalen.fetch_products_by_tenor(top_n=5)
    except Exception:
        products_by_tenor = {}

    estimates = finansportalen.estimate_next_lk_rates(products_by_tenor, lk)
    savings = _compute_savings(lk, belop, estimates) if lk else []
    return templates.TemplateResponse("partials/besparelse.html", {
        "request": request,
        "savings": savings,
        "loan_amount": belop,
    })


@app.get("/partials/vurdering", response_class=HTMLResponse)
async def partial_vurdering(request: Request):
    swap_rates = []
    try:
        swap_rates = await seb.fetch_swap_rates()
    except Exception:
        pass

    swap_history = {}
    for tenor in ["3 Yr", "5 Yr", "10 Yr"]:
        swap_history[tenor] = await db.get_swap_history(tenor, days=90)

    try:
        lk_rates = await lanekassen.fetch_rates()
        lk = lk_rates[0] if lk_rates else None
    except Exception:
        lk = None

    try:
        products_by_tenor = await finansportalen.fetch_products_by_tenor(top_n=5)
    except Exception:
        products_by_tenor = {}

    estimates = finansportalen.estimate_next_lk_rates(products_by_tenor, lk)
    signal = _recommend(lk, swap_history, estimates)

    return templates.TemplateResponse("partials/vurdering.html", {
        "request": request,
        "signal": signal,
        "has_swap_history": any(len(h) >= 2 for h in swap_history.values()),
    })


# --- Admin endpoints ---

@app.post("/api/oppdater")
async def oppdater():
    try:
        rates = await seb.fetch_swap_rates()
        await db.insert_swap_rates(rates)
        return {"status": "ok", "rates_stored": len(rates)}
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)


@app.post("/api/bootstrap")
async def bootstrap():
    try:
        rates = await cbonds.fetch_history(days_back=365)
        await db.insert_swap_rates(rates)
        return {"status": "ok", "rates_stored": len(rates)}
    except Exception as e:
        logger.error(f"Bootstrap failed: {e}")
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=500)
