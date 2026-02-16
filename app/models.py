from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class LanekassenRate:
    period: str  # e.g. "mars 2026"
    floating: float  # flytende rente
    fixed_3y: float | None = None
    fixed_5y: float | None = None
    fixed_10y: float | None = None


@dataclass
class SwapRate:
    tenor: str  # '3 Yr', '5 Yr', '10 Yr'
    rate: float
    change_today: float = 0.0
    observed_at: datetime = field(default_factory=datetime.now)
    source: str = "seb"


@dataclass
class BankProduct:
    bank: str
    nominal_rate: float
    effective_rate: float
    period: str  # e.g. "10 år"
    bound_years: int = 0
    product_name: str = ""


@dataclass
class EstimatedRate:
    """Estimert neste Lånekassen-rente basert på topp-5 bankrenter."""
    tenor: str  # "3 år", "5 år", "10 år"
    avg_top5: float  # snitt topp-5 nominelle bankrenter
    estimated_lk: float  # avg_top5 - 0.15pp
    current_lk: float | None  # nåværende Lånekassen-rente for denne tenoren
    diff: float | None  # estimated_lk - current_lk (positiv = renta forventes opp)
    bank_count: int = 0  # antall banker i grunnlaget
    std_dev: float = 0.0  # standardavvik i topp-5 bankrenter (spredning)


@dataclass
class Savings:
    tenor: str
    fixed_rate: float  # nåværende LK-fastrente
    estimated_next_rate: float  # estimert neste LK-fastrente
    loan_amount: float
    annual_diff: float  # positiv = neste rate høyere → bind nå sparer
    total_diff: float  # annual_diff * years
    years: int
    bind_now: bool  # True = binding nå er billigere
    risk: str  # "lav", "middels", "høy"


@dataclass
class TenorSignal:
    """Anbefaling per bindingsperiode."""
    tenor: str
    recommendation: str  # "BIND", "VENT", "USIKKER"
    color: str
    current_rate: float | None  # nåværende LK-fastrente
    estimated_next: float | None  # estimert neste LK-fastrente
    est_diff: float | None  # estimated_next - current_rate (positiv = neste dyrere → bind nå)
    total_diff_kr: int | None  # total forskjell i kroner over bindingsperioden
    swap_trend: float | None  # endring siste 90d
    swap_trend_days: int  # faktisk antall dager med data
    score: float = 0.0  # samlescore (høyere = sterkere case for binding)
    confidence: float = 0.0  # 0..1 hvor robust signalet er
    data_quality: str = "ukjent"  # "høy", "middels", "lav", "ukjent"
    reasons: list[str] = field(default_factory=list)


@dataclass
class Signal:
    recommendation: str  # "BIND 3 ÅR", "VENT", "HOLD FLYTENDE"
    color: str  # "green", "yellow", "red"
    best_tenor: str | None  # hvilken tenor er best å binde
    max_gain_recommendation: str | None = None  # mest aggressive gevinstvalg
    max_gain_detail: str | None = None  # forklaring til maks gevinst
    reasons: list[str] = field(default_factory=list)
    per_tenor: list[TenorSignal] = field(default_factory=list)
