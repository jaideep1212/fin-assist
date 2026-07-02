"""Tests for fin_assist.processing."""

from datetime import datetime

from src.processing import build_output_filename


def test_build_output_filename_formats_timestamp():
    when = datetime(2026, 7, 2, 6, 0, 0)
    result = build_output_filename("fin-assist", when)
    assert result == "fin-assist_20260702_060000.csv"


def test_build_output_filename_uses_prefix():
    when = datetime(2026, 1, 15, 9, 30, 0)
    result = build_output_filename("holdings", when)
    assert result.startswith("holdings_")
    assert result.endswith(".csv")
