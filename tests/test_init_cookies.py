"""Tests for the Morsel ``partitioned`` compatibility patch in __init__.py.

Regression coverage for the HA 2026.7 / Python 3.14 ``KeyError: 'partitioned'``
raised when stale ``http.cookies.Morsel`` objects from a restored aiohttp cookie
jar are re-saved. ``_patch_morsel_partitioned`` returns the default for any
missing reserved key. (The boot login passes the raw cookies from
``AlexaLogin.load_cookie()`` straight through, matching upstream.)
"""

from http.cookies import Morsel

import pytest

from custom_components.alexa_media import _patch_morsel_partitioned


def _morsel(key, value):
    morsel = Morsel()
    morsel.set(key, value, value)
    return morsel


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
