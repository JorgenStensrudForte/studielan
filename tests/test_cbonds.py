import pytest
from app.services.cbonds import _deobfuscate


def test_deobfuscate_removes_offset():
    chart_data = [
        {"date": "2026-01-01", "value": 604.44},
        {"date": "2026-01-02", "value": 604.50},
        {"date": "2026-02-12", "value": 606.42},
    ]
    true_value = 4.42

    result = _deobfuscate(chart_data, true_value)

    assert len(result) == 3
    assert result[0]["value"] == pytest.approx(2.44, abs=0.01)
    assert result[1]["value"] == pytest.approx(2.50, abs=0.01)
    assert result[2]["value"] == pytest.approx(4.42, abs=0.01)


def test_deobfuscate_empty():
    assert _deobfuscate([], 4.42) == []
