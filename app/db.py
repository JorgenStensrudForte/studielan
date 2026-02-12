import aiosqlite
from pathlib import Path
from datetime import datetime, timedelta

from app.config import settings
from app.models import SwapRate

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
