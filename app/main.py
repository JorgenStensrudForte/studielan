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

    Key insight: When you bind in a søknadsvindu, you get the ALREADY SET
    fastrente for the current period. The question is whether that fixed rate
    will be better than staying on floating for the binding period.

    - Spread (fast - flytende) is what you pay TODAY for certainty.
    - Estimated next LK rate tells you if the NEXT period's fastrente will be
      higher or lower (useful for deciding: bind now or wait for next window).
    - Swap trend tells you where the market expects rates to go.
    """
    label = TENOR_LABELS[tenor_key]
    reasons = []

    # Spread: current fixed rate vs current floating rate
    spread = None
    if lk:
        fixed = getattr(lk, lk_attr)
        if fixed is not None:
            spread = round(fixed - lk.floating, 3)

    # Swap trend from historical data
    swap_trend = None
    swap_days = 0
    has_trend = False
    if len(swap_history) >= 2:
        current_rate = swap_history[-1]["rate"]
        oldest_rate = swap_history[0]["rate"]
        swap_trend = round(current_rate - oldest_rate, 3)
        try:
            newest_dt = datetime.fromisoformat(swap_history[-1]["observed_at"])
            oldest_dt = datetime.fromisoformat(swap_history[0]["observed_at"])
            swap_days = (newest_dt - oldest_dt).days
        except (ValueError, KeyError):
            swap_days = len(swap_history)
        has_trend = swap_days >= 14

    # Estimated next LK fastrente (affects NEXT window, not this one)
    est_next = estimated.estimated_lk if estimated else None
    est_diff = estimated.diff if estimated else None

    # --- Build reasons (observations) ---

    # Spread observation
    if spread is not None:
        if spread < 0:
            reasons.append(f"Fast {label} er {abs(spread):.3f}pp BILLIGERE enn flytende - uvanlig gunstig")
        elif spread < 0.15:
            reasons.append(f"Lav spread ({spread:.3f}pp) - liten merkostnad for forutsigbarhet")
        else:
            reasons.append(f"Fast {label} er {spread:.3f}pp dyrere enn flytende")

    # Swap trend observation
    if has_trend:
        period_label = f"siste {swap_days}d" if swap_days < 80 else "siste 90d"
        if swap_trend < -0.20:
            reasons.append(f"Swap {label} falt {abs(swap_trend):.2f}pp {period_label} - markedet forventer lavere renter")
        elif swap_trend > 0.20:
            reasons.append(f"Swap {label} steg {swap_trend:.2f}pp {period_label} - markedet forventer høyere renter")
        else:
            reasons.append(f"Swap {label} stabil ({swap_trend:+.2f}pp {period_label})")
    elif len(swap_history) < 2:
        reasons.append(f"Mangler swap-historikk for {label} - kan ikke vurdere trend")

    # Estimated next LK rate (informational: affects next window)
    if est_diff is not None:
        if est_diff > 0.05:
            reasons.append(f"Neste periodes fastrente anslått til {est_next:.3f}% (opp {est_diff:.3f}pp) - denne renta er trolig billigere enn neste")
        elif est_diff < -0.05:
            reasons.append(f"Neste periodes fastrente anslått til {est_next:.3f}% (ned {abs(est_diff):.3f}pp) - kan lønne seg å vente til neste vindu")

    # --- Decision logic ---
    falling_swap = has_trend and swap_trend < -0.20
    rising_swap = has_trend and swap_trend > 0.20
    est_next_cheaper = est_diff is not None and est_diff < -0.10
    est_next_dearer = est_diff is not None and est_diff > 0.05

    # 1. Negative spread: free lunch
    if spread is not None and spread < 0:
        return TenorSignal(tenor=label, recommendation="BIND", color="green",
                           spread=spread, swap_trend=swap_trend, swap_trend_days=swap_days,
                           estimated_next=est_next, reasons=reasons)

    # 2. Clear "wait" signals: swap falling OR next period significantly cheaper
    if falling_swap and not rising_swap:
        return TenorSignal(tenor=label, recommendation="VENT", color="yellow",
                           spread=spread, swap_trend=swap_trend, swap_trend_days=swap_days,
                           estimated_next=est_next, reasons=reasons)

    if est_next_cheaper and not rising_swap:
        return TenorSignal(tenor=label, recommendation="VENT", color="yellow",
                           spread=spread, swap_trend=swap_trend, swap_trend_days=swap_days,
                           estimated_next=est_next, reasons=reasons)

    # 3. "Bind" signals: need BOTH a directional signal AND low spread
    #    We require at least one confirmed trend signal to say BIND
    small_spread = spread is not None and spread < 0.25
    has_bind_signal = rising_swap or (est_next_dearer and has_trend)

    if has_bind_signal and small_spread:
        return TenorSignal(tenor=label, recommendation="BIND", color="green",
                           spread=spread, swap_trend=swap_trend, swap_trend_days=swap_days,
                           estimated_next=est_next, reasons=reasons)

    # 4. Not enough data to make a strong call
    if not has_trend and spread is not None and spread > 0:
        reasons.append("Utilstrekkelig historikk for sikker anbefaling")
        return TenorSignal(tenor=label, recommendation="USIKKER", color="yellow",
                           spread=spread, swap_trend=swap_trend, swap_trend_days=swap_days,
                           estimated_next=est_next, reasons=reasons)

    # 5. Default: hold floating
    return TenorSignal(tenor=label, recommendation="HOLD FLYTENDE", color="red",
                       spread=spread, swap_trend=swap_trend, swap_trend_days=swap_days,
                       estimated_next=est_next, reasons=reasons)


def _recommend(
    lk: LanekassenRate | None,
    swap_history: dict[str, list[dict]],
    estimates: list[EstimatedRate],
) -> Signal:
    """Produce per-tenor signals and an overall recommendation."""
    est_by_label = {e.tenor: e for e in estimates}
    per_tenor = []

    for attr, tenor_key in TENOR_ATTRS:
        label = TENOR_LABELS[tenor_key]
        history = swap_history.get(tenor_key, [])
        estimated = est_by_label.get(label)
        ts = _tenor_signal(tenor_key, lk, attr, history, estimated)
        per_tenor.append(ts)

    # Overall: pick the best tenor to bind (if any)
    bind_tenors = [t for t in per_tenor if t.recommendation == "BIND"]
    wait_tenors = [t for t in per_tenor if t.recommendation == "VENT"]
    unsure_tenors = [t for t in per_tenor if t.recommendation == "USIKKER"]

    reasons = []

    if bind_tenors:
        # Prefer the tenor with the lowest (most negative) spread
        best = min(bind_tenors, key=lambda t: t.spread if t.spread is not None else 999)
        reasons.append(f"Beste binding: {best.tenor} (spread {best.spread:+.3f}pp)" if best.spread is not None else f"Beste binding: {best.tenor}")
        for t in bind_tenors:
            reasons.extend(t.reasons[:1])  # top reason per tenor
        return Signal(
            recommendation=f"BIND {best.tenor.upper()}",
            color="green",
            best_tenor=best.tenor,
            reasons=reasons,
            per_tenor=per_tenor,
        )

    if wait_tenors:
        reasons.append("Rentene peker nedover - vent til neste vindu")
        for t in wait_tenors:
            reasons.extend(t.reasons[:1])
        return Signal(
            recommendation="VENT",
            color="yellow",
            best_tenor=None,
            reasons=reasons,
            per_tenor=per_tenor,
        )

    if unsure_tenors and not any(t.recommendation == "HOLD FLYTENDE" for t in per_tenor):
        # All tenors are USIKKER (no swap history) — be honest about it
        reasons.append("Mangler swap-historikk for trendanalyse")
        reasons.append("Kjør bootstrap (Cbonds) eller vent til nok daglige SEB-datapunkter samles")
        return Signal(
            recommendation="USIKKER",
            color="yellow",
            best_tenor=None,
            reasons=reasons,
            per_tenor=per_tenor,
        )

    # Default: hold flytende
    for t in per_tenor:
        if t.spread is not None and t.spread > 0:
            reasons.append(f"Fast {t.tenor}: +{t.spread:.3f}pp dyrere")
    if not reasons:
        reasons.append("Ingen sterke signaler i noen retning")

    return Signal(
        recommendation="HOLD FLYTENDE",
        color="red",
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
                 "spread": t.spread, "swap_trend": t.swap_trend}
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
