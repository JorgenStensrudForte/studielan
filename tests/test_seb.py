import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from app.services.seb import fetch_swap_rates

MOCK_RESPONSE = {
    "rows": [
        {"data": [{"value": "1 Yr"}, {"value": 4.10}, {"value": -0.01}, {"value": "09:30"}, {"value": "2026-02-12"}]},
        {"data": [{"value": "3 Yr"}, {"value": 4.42}, {"value": -0.02}, {"value": "09:47"}, {"value": "2026-02-12"}]},
        {"data": [{"value": "5 Yr"}, {"value": 4.36}, {"value": -0.02}, {"value": "09:47"}, {"value": "2026-02-12"}]},
        {"data": [{"value": "10 Yr"}, {"value": 4.35}, {"value": -0.02}, {"value": "09:47"}, {"value": "2026-02-12"}]},
    ]
}


@pytest.mark.asyncio
async def test_fetch_swap_rates_filters_tenors():
    mock_resp = MagicMock()
    mock_resp.json.return_value = MOCK_RESPONSE
    mock_resp.raise_for_status = MagicMock()

    with patch("app.services.seb.httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client_cls.return_value = mock_client

        rates = await fetch_swap_rates()

    assert len(rates) == 3
    tenors = {r.tenor for r in rates}
    assert tenors == {"3 Yr", "5 Yr", "10 Yr"}
    assert rates[0].rate == 4.42
    assert rates[0].change_today == -0.02
    assert rates[0].source == "seb"
