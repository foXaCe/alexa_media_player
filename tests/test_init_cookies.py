"""Tests for the cookie sanitization helper in __init__.py.

Regression coverage for the HA 2026.7 / Python 3.14 ``KeyError: 'partitioned'``
raised when stale ``http.cookies.Morsel`` objects from a restored aiohttp cookie
jar are re-processed. ``_sanitize_cookies`` flattens them to plain string values.
"""

from http.cookies import Morsel

from custom_components.alexa_media import _sanitize_cookies


def _morsel(key, value):
    morsel = Morsel()
    morsel.set(key, value, value)
    return morsel


def test_sanitize_none_returns_none():
    assert _sanitize_cookies(None) is None


def test_sanitize_empty_returns_empty():
    assert _sanitize_cookies({}) == {}


def test_sanitize_plain_dict_is_unchanged():
    cookies = {"session-id": "abc123", "ubid-main": "130-1"}
    assert _sanitize_cookies(cookies) == cookies


def test_sanitize_flattens_morsels_to_values():
    cookies = {
        "session-id": _morsel("session-id", "abc123"),
        "at-main": _morsel("at-main", "Atza|token"),
    }
    result = _sanitize_cookies(cookies)
    assert result == {"session-id": "abc123", "at-main": "Atza|token"}
    # No Morsel survives -> aiohttp will rebuild fresh ones (with 'partitioned')
    assert all(not isinstance(v, Morsel) for v in result.values())


def test_sanitize_mixed_morsel_and_string():
    cookies = {"a": _morsel("a", "1"), "b": "2"}
    assert _sanitize_cookies(cookies) == {"a": "1", "b": "2"}
