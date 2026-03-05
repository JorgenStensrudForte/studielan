import aiosqlite
from pathlib import Path
from datetime import datetime, timedelta

from app.config import settings
from app.models import SwapRate, BankProduct, EstimatedRate

SCHEMA = """
CREATE TABLE IF NOT EXISTS swap_rates (
    id INTEGER PRIMARY KEY,
    tenor TEXT NOT NULL,
    rate REAL NOT NULL,
    change_today REAL DEFAULT 0,
    observed_at TEXT NOT NULL,
    source TEXT NOT NULL,
    UNIQUE(tenor, observed_at, source)
);
CREATE INDEX IF NOT EXISTS idx_swap_tenor_date ON swap_rates(tenor, observed_at);

CREATE TABLE IF NOT EXISTS bank_products (
    id INTEGER PRIMARY KEY,
    bank TEXT NOT NULL,
    product_name TEXT NOT NULL DEFAULT '',
    nominal_rate REAL NOT NULL,
    effective_rate REAL NOT NULL,
    bound_years INTEGER NOT NULL,
    rank INTEGER NOT NULL,
    observed_date TEXT NOT NULL,
    UNIQUE(bank, bound_years, observed_date)
);
CREATE INDEX IF NOT EXISTS idx_bank_products_date ON bank_products(bound_years, observed_date);

CREATE TABLE IF NOT EXISTS bank_rate_estimates (
    id INTEGER PRIMARY KEY,
    tenor TEXT NOT NULL,
    bound_years INTEGER NOT NULL,
    avg_top5_nominal REAL NOT NULL,
    avg_top5_effective REAL NOT NULL,
    estimated_lk_nominal REAL NOT NULL,
    estimated_lk_effective REAL NOT NULL,
    bank_count INTEGER NOT NULL,
    std_dev_nominal REAL NOT NULL DEFAULT 0,
    std_dev_effective REAL NOT NULL DEFAULT 0,
    current_lk REAL,
    diff REAL,
    observed_date TEXT NOT NULL,
    UNIQUE(tenor, observed_date)
);
CREATE INDEX IF NOT EXISTS idx_bank_estimates_date ON bank_rate_estimates(tenor, observed_date);
"""


async def get_db() -> aiosqlite.Connection:
    Path(settings.db_path).parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(settings.db_path)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    db.row_factory = aiosqlite.Row
    return db


async def init_db():
    db = await get_db()
    try:
        await db.executescript(SCHEMA)
        await db.commit()
    finally:
        await db.close()


async def insert_swap_rate(rate: SwapRate):
    db = await get_db()
    try:
        await db.execute(
            """INSERT OR IGNORE INTO swap_rates (tenor, rate, change_today, observed_at, source)
               VALUES (?, ?, ?, ?, ?)""",
            (rate.tenor, rate.rate, rate.change_today, rate.observed_at.isoformat(), rate.source),
        )
        await db.commit()
    finally:
        await db.close()


async def insert_swap_rates(rates: list[SwapRate]):
    db = await get_db()
    try:
        await db.executemany(
            """INSERT OR IGNORE INTO swap_rates (tenor, rate, change_today, observed_at, source)
               VALUES (?, ?, ?, ?, ?)""",
            [(r.tenor, r.rate, r.change_today, r.observed_at.isoformat(), r.source) for r in rates],
        )
        await db.commit()
    finally:
        await db.close()


async def get_swap_history(tenor: str, days: int = 90) -> list[dict]:
    db = await get_db()
    try:
        since = (datetime.now() - timedelta(days=days)).isoformat()
        cursor = await db.execute(
            """SELECT tenor, rate, change_today, observed_at, source
               FROM swap_rates
               WHERE tenor = ? AND observed_at >= ?
               ORDER BY observed_at ASC""",
            (tenor, since),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_latest_swap_rates() -> list[dict]:
    db = await get_db()
    try:
        cursor = await db.execute(
            """SELECT tenor, rate, change_today, observed_at, source
               FROM swap_rates
               WHERE observed_at = (SELECT MAX(observed_at) FROM swap_rates)
               ORDER BY tenor"""
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_swap_rate_90d_ago(tenor: str) -> float | None:
    db = await get_db()
    try:
        target = (datetime.now() - timedelta(days=90)).isoformat()
        cursor = await db.execute(
            """SELECT rate FROM swap_rates
               WHERE tenor = ? AND observed_at <= ?
               ORDER BY observed_at DESC LIMIT 1""",
            (tenor, target),
        )
        row = await cursor.fetchone()
        return row["rate"] if row else None
    finally:
        await db.close()


# --- Bank product history ---

async def insert_bank_products(
    products_by_tenor: dict[int, list[BankProduct]],
    observed_date: str | None = None,
):
    """Store individual bank products snapshot for a given date."""
    if observed_date is None:
        observed_date = datetime.now().strftime("%Y-%m-%d")

    rows = []
    for years, products in products_by_tenor.items():
        for rank, p in enumerate(products, 1):
            rows.append((
                p.bank, p.product_name, p.nominal_rate, p.effective_rate,
                years, rank, observed_date,
            ))

    if not rows:
        return

    db = await get_db()
    try:
        await db.executemany(
            """INSERT OR REPLACE INTO bank_products
               (bank, product_name, nominal_rate, effective_rate, bound_years, rank, observed_date)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await db.commit()
    finally:
        await db.close()


async def insert_bank_rate_estimates(
    estimates: list[EstimatedRate],
    products_by_tenor: dict[int, list[BankProduct]],
    observed_date: str | None = None,
):
    """Store aggregate estimates (avg top 5 nominal/effective, estimated LK rates)."""
    import statistics as stat

    if observed_date is None:
        observed_date = datetime.now().strftime("%Y-%m-%d")

    tenor_years_map = {"3 år": 3, "5 år": 5, "10 år": 10}
    rows = []
    for est in estimates:
        years = tenor_years_map.get(est.tenor)
        if years is None:
            continue

        products = products_by_tenor.get(years, [])[:5]
        nominal_rates = [p.nominal_rate for p in products]
        effective_rates = [p.effective_rate for p in products]

        avg_eff = sum(effective_rates) / len(effective_rates) if effective_rates else 0
        est_lk_eff = round(avg_eff - 0.15, 3) if effective_rates else 0
        std_dev_eff = round(stat.stdev(effective_rates), 3) if len(effective_rates) >= 2 else 0.0

        rows.append((
            est.tenor, years,
            est.avg_top5, avg_eff,
            est.estimated_lk, est_lk_eff,
            est.bank_count,
            est.std_dev, std_dev_eff,
            est.current_lk, est.diff,
            observed_date,
        ))

    if not rows:
        return

    db = await get_db()
    try:
        await db.executemany(
            """INSERT OR REPLACE INTO bank_rate_estimates
               (tenor, bound_years, avg_top5_nominal, avg_top5_effective,
                estimated_lk_nominal, estimated_lk_effective,
                bank_count, std_dev_nominal, std_dev_effective,
                current_lk, diff, observed_date)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            rows,
        )
        await db.commit()
    finally:
        await db.close()


async def get_bank_rate_history(tenor: str | None = None, days: int = 365) -> list[dict]:
    """Fetch historical bank rate estimates for charting."""
    db = await get_db()
    try:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        if tenor:
            cursor = await db.execute(
                """SELECT tenor, bound_years, avg_top5_nominal, avg_top5_effective,
                          estimated_lk_nominal, estimated_lk_effective,
                          bank_count, std_dev_nominal, std_dev_effective,
                          current_lk, diff, observed_date
                   FROM bank_rate_estimates
                   WHERE tenor = ? AND observed_date >= ?
                   ORDER BY observed_date ASC""",
                (tenor, since),
            )
        else:
            cursor = await db.execute(
                """SELECT tenor, bound_years, avg_top5_nominal, avg_top5_effective,
                          estimated_lk_nominal, estimated_lk_effective,
                          bank_count, std_dev_nominal, std_dev_effective,
                          current_lk, diff, observed_date
                   FROM bank_rate_estimates
                   WHERE observed_date >= ?
                   ORDER BY observed_date ASC""",
                (since,),
            )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()


async def get_bank_products_history(bound_years: int, days: int = 365) -> list[dict]:
    """Fetch historical individual bank products for a given tenor."""
    db = await get_db()
    try:
        since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
        cursor = await db.execute(
            """SELECT bank, product_name, nominal_rate, effective_rate,
                      bound_years, rank, observed_date
               FROM bank_products
               WHERE bound_years = ? AND observed_date >= ?
               ORDER BY observed_date ASC, rank ASC""",
            (bound_years, since),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
    finally:
        await db.close()
