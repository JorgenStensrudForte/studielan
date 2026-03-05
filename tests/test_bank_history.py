import pytest
from datetime import datetime

from app.models import BankProduct, EstimatedRate
from app import db


@pytest.fixture
async def test_db(tmp_path):
    """Initialize a temporary database for testing."""
    original = db.settings.db_path
    db.settings.db_path = str(tmp_path / "test.db")
    await db.init_db()
    yield
    db.settings.db_path = original


SAMPLE_PRODUCTS = {
    3: [
        BankProduct(bank="Sbanken", nominal_rate=4.89, effective_rate=5.00, period="3 år", bound_years=3, product_name="Boliglån fast 3 år"),
        BankProduct(bank="SpareBank 1 Østfold", nominal_rate=4.93, effective_rate=5.09, period="3 år", bound_years=3, product_name="Boliglån fast"),
        BankProduct(bank="Sparebanken Møre", nominal_rate=4.99, effective_rate=5.16, period="3 år", bound_years=3, product_name="Boliglån fast"),
    ],
    5: [
        BankProduct(bank="Sbanken", nominal_rate=4.84, effective_rate=4.95, period="5 år", bound_years=5, product_name="Boliglån fast 5 år"),
        BankProduct(bank="Bien Sparebank", nominal_rate=4.90, effective_rate=5.06, period="5 år", bound_years=5, product_name="Boliglån fast"),
    ],
}

SAMPLE_ESTIMATES = [
    EstimatedRate(tenor="3 år", avg_top5=4.96, estimated_lk=4.81, current_lk=4.736, diff=0.074, bank_count=3, std_dev=0.05),
    EstimatedRate(tenor="5 år", avg_top5=4.87, estimated_lk=4.72, current_lk=4.745, diff=-0.025, bank_count=2, std_dev=0.04),
]


@pytest.mark.asyncio
async def test_insert_and_query_bank_products(test_db):
    await db.insert_bank_products(SAMPLE_PRODUCTS, observed_date="2026-03-05")

    history = await db.get_bank_products_history(bound_years=3, days=365)
    assert len(history) == 3
    assert history[0]["bank"] == "Sbanken"
    assert history[0]["nominal_rate"] == 4.89
    assert history[0]["effective_rate"] == 5.00
    assert history[0]["rank"] == 1
    assert history[0]["observed_date"] == "2026-03-05"


@pytest.mark.asyncio
async def test_insert_and_query_bank_estimates(test_db):
    await db.insert_bank_rate_estimates(SAMPLE_ESTIMATES, SAMPLE_PRODUCTS, observed_date="2026-03-05")

    history = await db.get_bank_rate_history(tenor="3 år", days=365)
    assert len(history) == 1
    row = history[0]
    assert row["tenor"] == "3 år"
    assert row["bound_years"] == 3
    assert row["avg_top5_nominal"] == 4.96
    assert row["avg_top5_effective"] > 0
    assert row["estimated_lk_nominal"] == 4.81
    assert row["estimated_lk_effective"] > 0
    assert row["bank_count"] == 3
    assert row["observed_date"] == "2026-03-05"


@pytest.mark.asyncio
async def test_duplicate_insert_ignored(test_db):
    await db.insert_bank_products(SAMPLE_PRODUCTS, observed_date="2026-03-05")
    await db.insert_bank_products(SAMPLE_PRODUCTS, observed_date="2026-03-05")

    history = await db.get_bank_products_history(bound_years=3, days=365)
    assert len(history) == 3  # no duplicates


@pytest.mark.asyncio
async def test_bank_history_all_tenors(test_db):
    await db.insert_bank_rate_estimates(SAMPLE_ESTIMATES, SAMPLE_PRODUCTS, observed_date="2026-03-05")

    history = await db.get_bank_rate_history(days=365)
    assert len(history) == 2
    tenors = {r["tenor"] for r in history}
    assert tenors == {"3 år", "5 år"}


@pytest.mark.asyncio
async def test_bank_history_date_filtering(test_db):
    await db.insert_bank_rate_estimates(SAMPLE_ESTIMATES, SAMPLE_PRODUCTS, observed_date="2020-01-01")

    # Should not appear within last 365 days
    history = await db.get_bank_rate_history(tenor="3 år", days=365)
    assert len(history) == 0
