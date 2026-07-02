"""Core data-processing helpers for the fin-assist pipeline."""

from datetime import datetime


def build_output_filename(prefix: str, when: datetime) -> str:
    """Build a timestamped CSV filename.

    Example:
        build_output_filename("fin-assist", datetime(2026, 7, 2, 6, 0, 0))
        -> "fin-assist_20260702_060000.csv"
    """
    timestamp = when.strftime("%Y%m%d_%H%M%S")
    return f"{prefix}_{timestamp}.csv"
