import pytest
from app.services.lanekassen import _parse_rate


def test_parse_rate_comma():
    assert _parse_rate("4,621%") == 4.621


def test_parse_rate_dot():
    assert _parse_rate("4.621%") == 4.621


def test_parse_rate_dash():
    assert _parse_rate("-") is None


def test_parse_rate_empty():
    assert _parse_rate("") is None


def test_parse_rate_whitespace():
    assert _parse_rate("  4,736 % ") == 4.736
