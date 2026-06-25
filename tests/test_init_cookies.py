"""Tests for the cookie sanitization helper in __init__.py.

Regression coverage for the HA 2026.7 / Python 3.14 ``KeyError: 'partitioned'``
raised when stale ``http.cookies.Morsel`` objects from a restored aiohttp cookie
jar are re-processed. ``_sanitize_cookies`` flattens them to plain string values.
"""

from http.cookies import Morsel
import pickle
from unittest.mock import MagicMock

import pytest

from custom_components.alexa_media import (
    _cookie_pickle_paths,
    _patch_morsel_partitioned,
    _purge_corrupt_cookie_files,
    _sanitize_cookies,
)


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


# --------------------------------------------------------------------------- #
# _patch_morsel_partitioned - the actual KeyError: 'partitioned' fix
# --------------------------------------------------------------------------- #


def test_patch_applied_on_import():
    # importing the package runs _patch_morsel_partitioned() at module load
    assert getattr(Morsel, "_alexa_media_partitioned_patch", False) is True


def test_missing_reserved_key_returns_default_instead_of_keyerror():
    # Simulate a Morsel restored from an old pickle that lacks 'partitioned'
    # (aiohttp >= 3.14 CookieJar.save reads morsel['partitioned'] directly).
    morsel = _morsel("session-id", "abc")
    if "partitioned" in morsel:
        dict.__delitem__(morsel, "partitioned")
    # patched __getitem__ returns "" for a missing reserved key
    assert morsel["partitioned"] == ""


def test_unknown_non_reserved_key_still_raises():
    morsel = _morsel("session-id", "abc")
    with pytest.raises(KeyError):
        _ = morsel["definitely_not_a_reserved_attr"]


def test_patch_is_idempotent():
    # calling again is a no-op (guarded) and must not double-wrap
    _patch_morsel_partitioned()
    morsel = _morsel("session-id", "abc")
    if "partitioned" in morsel:
        dict.__delitem__(morsel, "partitioned")
    assert morsel["partitioned"] == ""


# --------------------------------------------------------------------------- #
# blocking-call fix: purge corrupt cookie pickles off the event loop
# --------------------------------------------------------------------------- #


def _hass_for(tmp_path):
    hass = MagicMock()
    hass.config.path = lambda *parts: str(tmp_path.joinpath(*parts))

    async def _run(func, *args):
        return func(*args)

    hass.async_add_executor_job = _run
    return hass


def test_cookie_pickle_paths():
    hass = MagicMock()
    hass.config.path = lambda *parts: "/config/" + "/".join(parts)
    paths = _cookie_pickle_paths(hass, "a@example.com")
    assert paths == [
        "/config/.storage/alexa_media.a@example.com.pickle",
        "/config/alexa_media.a@example.com.pickle",
    ]


async def test_purge_removes_corrupt_keeps_valid(tmp_path):
    (tmp_path / ".storage").mkdir()
    valid = tmp_path / ".storage" / "alexa_media.a@example.com.pickle"
    valid.write_bytes(pickle.dumps({"cookie": "ok"}))
    corrupt = tmp_path / "alexa_media.a@example.com.pickle"
    corrupt.write_bytes(b"this is not a pickle \x00\x01\x02")

    await _purge_corrupt_cookie_files(_hass_for(tmp_path), "a@example.com")

    assert valid.exists()  # readable pickle is kept
    assert not corrupt.exists()  # unreadable pickle is removed


async def test_purge_no_files_is_noop(tmp_path):
    # nothing to purge, must not raise
    await _purge_corrupt_cookie_files(_hass_for(tmp_path), "a@example.com")
