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


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


def _tenor_years_from_label(label: str) -> int:
    try:
        return int(label.split()[0])
    except (ValueError, IndexError):
        return 99


def _tenor_signal(
    tenor_key: str,
    lk: LanekassenRate | None,
    lk_attr: str,
    swap_history: list[dict],
    estimated: EstimatedRate | None,
    loan_amount: int,
) -> TenorSignal:
    """Analyze a single tenor with a score-based model."""
    label = TENOR_LABELS[tenor_key]
    bound_years = TENOR_MAP[tenor_key]
    reasons = []

    # Current fixed rate
    current_rate = None
    if lk:
        current_rate = getattr(lk, lk_attr)

    # Estimated next LK fastrente
    est_next = estimated.estimated_lk if estimated else None
    est_diff = estimated.diff if estimated else None
    bank_count = estimated.bank_count if estimated else 0
    std_dev = estimated.std_dev if estimated else 0.0
    total_diff_kr = None
    if est_diff is not None:
        total_diff_kr = round((est_diff / 100) * loan_amount * bound_years)

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

    # Observations: current vs estimated next
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

    # Score components
    rate_score = _clamp((est_diff or 0.0) / 0.05, -4.0, 4.0) if est_diff is not None else 0.0
    trend_score = _clamp((swap_trend or 0.0) / 0.10, -3.0, 3.0) if has_trend and swap_trend is not None else 0.0

    quality_penalty = 0.0
    if estimated is None:
        quality_penalty += 1.2
    else:
        if bank_count < 3:
            quality_penalty += 1.2
        elif bank_count < 5:
            quality_penalty += 0.5
        if std_dev > 0.10:
            quality_penalty += min(1.2, (std_dev - 0.10) / 0.10)

    tenor_penalty = {3: 0.00, 5: 0.25, 10: 0.60}.get(bound_years, 0.30)
    score = round(0.7 * rate_score + 0.3 * trend_score - quality_penalty - tenor_penalty, 3)

    if quality_penalty <= 0.4:
        data_quality = "høy"
    elif quality_penalty <= 1.2:
        data_quality = "middels"
    else:
        data_quality = "lav"

    evidence = min(1.0, abs(est_diff or 0.0) / 0.20) if est_diff is not None else 0.0
    trend_evidence = min(1.0, abs(trend_score) / 3.0) if has_trend else 0.3
    quality_factor = max(0.0, 1.0 - (quality_penalty / 2.4))
    confidence = 0.55 * evidence + 0.25 * trend_evidence + 0.20 * quality_factor
    if not has_trend:
        confidence -= 0.05
    confidence = round(_clamp(confidence, 0.10, 0.95), 2)

    reasons.append(f"Datakvalitet: {data_quality} (banker={bank_count}, std={std_dev:.3f})")
    reasons.append(
        "Score: "
        f"{score:+.2f} = 0.7×ratesignal ({rate_score:+.2f}) + "
        f"0.3×swapsignal ({trend_score:+.2f}) − "
        f"datapåslag ({quality_penalty:.2f}) − tenorpåslag ({tenor_penalty:.2f})"
    )

    decision_margin = 1.0
    if not has_trend:
        decision_margin += 0.2
    if bank_count and bank_count < 5:
        decision_margin += 0.2
    if std_dev > 0.15:
        decision_margin += 0.2

    def _make(rec, color):
        return TenorSignal(
            tenor=label, recommendation=rec, color=color,
            current_rate=current_rate, estimated_next=est_next,
            est_diff=est_diff, total_diff_kr=total_diff_kr,
            swap_trend=swap_trend,
            swap_trend_days=swap_days, score=score,
            confidence=confidence, data_quality=data_quality,
            reasons=reasons,
        )

    if est_diff is None:
        reasons.append("Mangler rateestimat, kan ikke gi robust anbefaling")
        return _make("USIKKER", "yellow")

    # Small moves in both primary and confirmation signals should be treated as noise.
    if abs(est_diff) < 0.03 and abs(trend_score) < 0.8:
        reasons.append("Små utslag i både estimat og swap-trend — signalet er svakt")
        return _make("USIKKER", "yellow")

    if score >= decision_margin:
        return _make("BIND", "green")

    if score <= -decision_margin:
        return _make("VENT", "red")

    reasons.append("Score er i gråsonen — hverken klart BIND eller klart VENT")
    return _make("USIKKER", "yellow")


def _recommend(
    lk: LanekassenRate | None,
    swap_history: dict[str, list[dict]],
    estimates: list[EstimatedRate],
    loan_amount: int = settings.default_loan_amount,
) -> Signal:
    """Produce per-tenor signals and an overall recommendation."""
    est_by_label = {e.tenor: e for e in estimates}
    per_tenor = []

    for attr, tenor_key in TENOR_ATTRS:
        label = TENOR_LABELS[tenor_key]
        history = swap_history.get(tenor_key, [])
        estimated = est_by_label.get(label)
        ts = _tenor_signal(tenor_key, lk, attr, history, estimated, loan_amount=loan_amount)
        per_tenor.append(ts)

    with_estimate = [t for t in per_tenor if t.est_diff is not None]
    max_gain_recommendation = None
    max_gain_detail = None
    if with_estimate:
        gain = max(with_estimate, key=lambda t: abs(t.est_diff or 0.0))
        gain_diff = gain.est_diff or 0.0
        if abs(gain_diff) >= 0.01:
            if gain_diff >= 0:
                max_gain_recommendation = f"BIND {gain.tenor.upper()}"
                max_gain_detail = f"størst estimert gevinst: {gain_diff:+.3f}pp vs dagens rente"
            else:
                max_gain_recommendation = f"VENT ({gain.tenor})"
                max_gain_detail = f"størst estimert gevinst ved venting: {abs(gain_diff):.3f}pp lavere neste rente"

    if not per_tenor:
        return Signal(
            recommendation="USIKKER",
            color="yellow",
            best_tenor=None,
            max_gain_recommendation=max_gain_recommendation,
            max_gain_detail=max_gain_detail,
            reasons=["Ingen tenor-data tilgjengelig for vurdering"],
            per_tenor=[],
        )

    bind_tenors = [t for t in per_tenor if t.recommendation == "BIND"]
    wait_tenors = [t for t in per_tenor if t.recommendation == "VENT"]
    unsure_tenors = [t for t in per_tenor if t.recommendation == "USIKKER"]

    by_score = sorted(per_tenor, key=lambda t: (t.score, t.confidence), reverse=True)
    reasons = []

    if bind_tenors:
        bind_sorted = sorted(bind_tenors, key=lambda t: (t.score, t.confidence), reverse=True)
        best = bind_sorted[0]
        if len(bind_sorted) > 1 and abs(bind_sorted[0].score - bind_sorted[1].score) < 0.25:
            # If two candidates are nearly tied, prefer shorter lock-in period.
            best = min(
                [bind_sorted[0], bind_sorted[1]],
                key=lambda t: _tenor_years_from_label(t.tenor),
            )
            reasons.append("To bindingstider scorer nesten likt; velger kortere binding for lavere låserisiko.")

        reasons.append(
            f"Beste binding etter score: {best.tenor} "
            f"(score {best.score:+.2f}, sikkerhet {best.confidence:.2f})"
        )

        strongest_wait = min((t.score for t in per_tenor), default=0.0)
        if strongest_wait <= -1.5 and best.score < 1.5:
            reasons.append("Ulik retning mellom tenorene gir svak total klarhet — setter USIKKER.")
            reasons.extend(best.reasons[:2])
            return Signal(
                recommendation="USIKKER",
                color="yellow",
                best_tenor=None,
                max_gain_recommendation=max_gain_recommendation,
                max_gain_detail=max_gain_detail,
                reasons=reasons,
                per_tenor=per_tenor,
            )

        if best.confidence < 0.45:
            reasons.append("Lav sikkerhet i beste signal — setter USIKKER fremfor hard BIND.")
            reasons.extend(best.reasons[:2])
            return Signal(
                recommendation="USIKKER",
                color="yellow",
                best_tenor=None,
                max_gain_recommendation=max_gain_recommendation,
                max_gain_detail=max_gain_detail,
                reasons=reasons,
                per_tenor=per_tenor,
            )

        reasons.extend(best.reasons[:2])
        return Signal(
            recommendation=f"BIND {best.tenor.upper()}",
            color="green",
            best_tenor=best.tenor,
            max_gain_recommendation=max_gain_recommendation,
            max_gain_detail=max_gain_detail,
            reasons=reasons,
            per_tenor=per_tenor,
        )

    if wait_tenors and not bind_tenors:
        best_wait = min(wait_tenors, key=lambda t: (t.score, -t.confidence))
        reasons.append(
            f"Sterkeste waits-signal: {best_wait.tenor} "
            f"(score {best_wait.score:+.2f}, sikkerhet {best_wait.confidence:.2f})"
        )
        reasons.append("Flere tenor-signaler peker mot lavere neste fastrente.")

        if best_wait.confidence < 0.45:
            reasons.append("Signalet er ikke robust nok — setter USIKKER.")
            reasons.extend(best_wait.reasons[:2])
            return Signal(
                recommendation="USIKKER",
                color="yellow",
                best_tenor=None,
                max_gain_recommendation=max_gain_recommendation,
                max_gain_detail=max_gain_detail,
                reasons=reasons,
                per_tenor=per_tenor,
            )

        reasons.extend(best_wait.reasons[:2])
        return Signal(
            recommendation="VENT",
            color="red",
            best_tenor=None,
            max_gain_recommendation=max_gain_recommendation,
            max_gain_detail=max_gain_detail,
            reasons=reasons,
            per_tenor=per_tenor,
        )

    if unsure_tenors:
        top = by_score[0]
        reasons.append("Ingen tenor har sterk nok score til tydelig BIND/VENT.")
        reasons.append(f"Høyeste score: {top.tenor} ({top.score:+.2f}, sikkerhet {top.confidence:.2f})")
        reasons.extend(top.reasons[:2])
        return Signal(
            recommendation="USIKKER",
            color="yellow",
            best_tenor=None,
            max_gain_recommendation=max_gain_recommendation,
            max_gain_detail=max_gain_detail,
            reasons=reasons,
            per_tenor=per_tenor,
        )

    reasons.append("Ingen sterke signaler i noen retning")
    return Signal(
        recommendation="USIKKER",
        color="yellow",
        best_tenor=None,
        max_gain_recommendation=max_gain_recommendation,
        max_gain_detail=max_gain_detail,
        reasons=reasons,
        per_tenor=per_tenor,
    )


async def _fetch_all_data(
    loan_amount: int,
    remaining_years: int = settings.default_remaining_years,
) -> dict:
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
    signal = _recommend(lk_current, swap_history, estimates, loan_amount=loan_amount)

    # Application window
    cw = current_window()
    nw = next_window()
    days_to_window = days_until_next_window()
    now_dt = datetime.now()
    banker_updated_at = now_dt
    window_countdown_target = None
    window_countdown_seconds = None
    window_countdown_label = None

    if cw:
        countdown_day = cw[1] - timedelta(days=1)
        window_countdown_target = datetime(
            countdown_day.year,
            countdown_day.month,
            countdown_day.day,
            23,
            59,
            0,
        )
        window_countdown_seconds = max(
            0,
            int((window_countdown_target - now_dt).total_seconds()),
        )
        window_countdown_label = window_countdown_target.strftime("%d. %B %Y kl. %H:%M")

    return {
        "lanekassen": lk_current,
        "lanekassen_all": lk_rates[:6],
        "swap_rates": swap_rates,
        "swap_history": swap_history,
        "has_swap_history": has_swap_history,
        "products_by_tenor": products_by_tenor,
        "banker_updated_at": banker_updated_at,
        "estimates": estimates,
        "savings": savings,
        "signal": signal,
        "loan_amount": loan_amount,
        "remaining_years": remaining_years,
        "current_window": cw,
        "next_window": nw,
        "days_to_window": days_to_window,
        "window_countdown_target": window_countdown_target,
        "window_countdown_seconds": window_countdown_seconds,
        "window_countdown_label": window_countdown_label,
        "today": date.today(),
    }


# --- Routes ---

@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    belop: int = Query(default=settings.default_loan_amount),
    remaining_years: int = Query(default=settings.default_remaining_years, ge=1, le=40),
):
    data = await _fetch_all_data(belop, remaining_years=remaining_years)
    return templates.TemplateResponse("dashboard.html", {"request": request, **data})


@app.get("/api/dashboard")
async def api_dashboard(
    belop: int = Query(default=settings.default_loan_amount),
    remaining_years: int = Query(default=settings.default_remaining_years, ge=1, le=40),
):
    data = await _fetch_all_data(belop, remaining_years=remaining_years)
    result = {
        "loan_amount": data["loan_amount"],
        "remaining_years": data["remaining_years"],
        "today": data["today"].isoformat(),
        "window_countdown_target": data["window_countdown_target"].isoformat() if data["window_countdown_target"] else None,
        "window_countdown_seconds": data["window_countdown_seconds"],
        "window_countdown_label": data["window_countdown_label"],
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
        "banker_updated_at": data["banker_updated_at"].isoformat() if data.get("banker_updated_at") else None,
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
            "max_gain_recommendation": data["signal"].max_gain_recommendation,
            "max_gain_detail": data["signal"].max_gain_detail,
            "reasons": data["signal"].reasons,
            "per_tenor": [
                {"tenor": t.tenor, "recommendation": t.recommendation, "color": t.color,
                 "current_rate": t.current_rate, "estimated_next": t.estimated_next,
                 "est_diff": t.est_diff, "total_diff_kr": t.total_diff_kr, "swap_trend": t.swap_trend,
                 "score": t.score, "confidence": t.confidence, "data_quality": t.data_quality}
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
    updated_at = datetime.now()
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
        "banker_updated_at": updated_at,
        "estimates": estimates,
    })


@app.get("/partials/besparelse", response_class=HTMLResponse)
async def partial_besparelse(
    request: Request,
    belop: int = Query(default=settings.default_loan_amount),
    remaining_years: int = Query(default=settings.default_remaining_years, ge=1, le=40),
):
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
        "remaining_years": remaining_years,
        "lanekassen": lk,
        "estimates": estimates,
    })


@app.get("/partials/vurdering", response_class=HTMLResponse)
async def partial_vurdering(
    request: Request,
    belop: int = Query(default=settings.default_loan_amount),
):
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
    signal = _recommend(lk, swap_history, estimates, loan_amount=belop)

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
