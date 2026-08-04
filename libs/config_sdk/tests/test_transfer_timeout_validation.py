"""Phase 5F: validate_transfer_timeout_ms bounds (10s min / 45s default /
120s max) — invalid or out-of-range values fall back to the default with a
warning, never raise."""

from libs.config_sdk import (
    TRANSFER_TIMEOUT_DEFAULT_MS,
    TRANSFER_TIMEOUT_MAX_MS,
    TRANSFER_TIMEOUT_MIN_MS,
    validate_transfer_timeout_ms,
)


def test_in_bounds_values_pass_through():
    assert validate_transfer_timeout_ms(10_000) == 10_000
    assert validate_transfer_timeout_ms(45_000) == 45_000
    assert validate_transfer_timeout_ms(120_000) == 120_000
    assert validate_transfer_timeout_ms("30000") == 30_000  # numeric string ok


def test_out_of_bounds_falls_back_to_default():
    assert validate_transfer_timeout_ms(0) == TRANSFER_TIMEOUT_DEFAULT_MS
    assert validate_transfer_timeout_ms(-1) == TRANSFER_TIMEOUT_DEFAULT_MS
    assert validate_transfer_timeout_ms(9_999) == TRANSFER_TIMEOUT_DEFAULT_MS
    assert validate_transfer_timeout_ms(120_001) == TRANSFER_TIMEOUT_DEFAULT_MS
    assert validate_transfer_timeout_ms(3_600_000) == TRANSFER_TIMEOUT_DEFAULT_MS


def test_non_numeric_falls_back_to_default():
    assert validate_transfer_timeout_ms(None) == TRANSFER_TIMEOUT_DEFAULT_MS
    assert validate_transfer_timeout_ms("forever") == TRANSFER_TIMEOUT_DEFAULT_MS


def test_warning_emitted_on_fallback(caplog):
    import logging
    with caplog.at_level(logging.WARNING, logger="libs.config_sdk.models"):
        validate_transfer_timeout_ms(0, context="agent=t/a")
    assert any("out of bounds" in r.message and "agent=t/a" in r.message
               for r in caplog.records)


def test_bounds_are_sane():
    assert TRANSFER_TIMEOUT_MIN_MS < TRANSFER_TIMEOUT_DEFAULT_MS < TRANSFER_TIMEOUT_MAX_MS
