"""Tests for diagnostics.py (collection, redaction, obfuscation, and coordinator discovery)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
import logging
import sys
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
import pytest

from custom_components.alexa_media.const import DOMAIN, TO_REDACT
from custom_components.alexa_media.diagnostics import (
    _find_coordinators,
    _maybe_keys,
    _maybe_len,
    _obfuscate_identifier,
    _obfuscate_title_with_email,
    _safe_dt,
    _sample_names,
    _summarize_amp_domain,
    _summarize_amp_entry_runtime,
    _summarize_coordinator,
    _summarize_coordinator_data,
    async_get_config_entry_diagnostics,
    async_get_device_diagnostics,
)


@pytest.fixture
def mock_hass():
    """Create a minimal hass-like object for unit tests."""
    hass = MagicMock()
    hass.data = {}
    return hass


@pytest.mark.parametrize(
    ("val", "expected"),
    [
        (None, "****"),
        ("", "****"),
        ("abc", "****"),
        ("abcd", "****"),
        ("abcde", "ab...de"),
        (12345, "****"),
    ],
)
def test_obfuscate_identifier(val, expected):
    assert _obfuscate_identifier(val) == expected


def test_obfuscate_title_with_email_uses_hide_email_when_available(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "alexapy",
        SimpleNamespace(hide_email=lambda _s: "h***@example.com"),
    )

    title = "Alexa Media Player (daniel@example.com)"
    assert _obfuscate_title_with_email(title, "daniel@example.com") == (
        "Alexa Media Player (h***@example.com)"
    )


def test_obfuscate_title_with_email_falls_back_when_import_fails(monkeypatch):
    monkeypatch.delitem(sys.modules, "alexapy", raising=False)

    title = "Alexa Media Player (daniel@example.com)"
    out = _obfuscate_title_with_email(title, "daniel@example.com")

    assert out is not None
    assert "daniel@example.com" not in out
    assert "****" in out or "da...om" in out


def test_maybe_keys_non_mapping_returns_none():
    assert _maybe_keys(["not", "a", "mapping"]) is None
    assert _maybe_keys("nope") is None


def test_maybe_keys_sanitizes_email_keys_and_limits(monkeypatch):
    monkeypatch.setitem(
        sys.modules,
        "alexapy",
        SimpleNamespace(hide_email=lambda _s: "h***@example.com"),
    )

    val = {
        "daniel@example.com": 1,
        "some_token_value_abcdef": 2,  # nosec B105: Bandit hardcoded_password_string; dummy token string for unit test only
        "ok": 3,
    }

    keys = _maybe_keys(val, limit=2)
    assert keys is not None
    assert len(keys) == 2
    assert all("daniel@example.com" not in k for k in keys)


def test_find_coordinators_finds_nested_coordinator(mock_hass):
    coordinator = DataUpdateCoordinator(
        mock_hass,
        logger=logging.getLogger(__name__),
        config_entry=None,
        name="test",
        update_interval=timedelta(seconds=30),
    )

    tree = {"a": [{"b": coordinator}], "c": {"d": "nope"}}
    found = _find_coordinators(tree)

    assert coordinator in found
    assert len(found) == 1


def test_summarize_coordinator_error_handling(mock_hass):
    coordinator = DataUpdateCoordinator(
        mock_hass,
        logger=logging.getLogger(__name__),
        config_entry=None,
        name="test",
        update_interval=timedelta(seconds=30),
    )

    with patch(
        "custom_components.alexa_media.diagnostics._summarize_coordinator_data",
        side_effect=RuntimeError("boom"),
    ):
        summary = _summarize_coordinator(coordinator)

    assert summary["data_summary_error"] == "RuntimeError"
    assert summary["data_summary_error_present"] is True


def test_summarize_amp_entry_runtime_mapping(monkeypatch):
    monkeypatch.setattr(
        "custom_components.alexa_media.diagnostics._maybe_keys",
        lambda _v, _limit=50: ["aa...zz"],
    )

    entry_runtime = {"devices": [1, 2, 3]}
    out = _summarize_amp_entry_runtime(entry_runtime)

    assert out["present"] is True
    assert out["runtime_type"] == "mapping"
    assert out["runtime_keys"] == ["aa...zz"]


@pytest.mark.asyncio
async def test_async_get_config_entry_diagnostics_redacts_sensitive_fields(
    mock_hass, monkeypatch
):
    redact_key = next(iter(TO_REDACT))
    secret_value = "supersecret123"  # nosec B105

    entry = SimpleNamespace(
        entry_id="entry123",
        title="Alexa Media Player (daniel@example.com)",
        domain=DOMAIN,
        version=1,
        minor_version=0,
        data={"email": "daniel@example.com", redact_key: secret_value},
        options={redact_key: secret_value},
    )

    mock_hass.data.setdefault(DOMAIN, {})
    monkeypatch.setattr(
        "custom_components.alexa_media.diagnostics._get_safe_config_entry_title",
        lambda _entry: "Alexa Media Player (h***@example.com)",
    )

    out = await async_get_config_entry_diagnostics(mock_hass, entry)

    assert out["data"].get(redact_key) != secret_value
    assert out["options"].get(redact_key) != secret_value
    title = out["entry"]["title"]
    assert isinstance(title, str) or title is None
    if title:
        assert "daniel@example.com" not in title


@pytest.mark.asyncio
async def test_async_get_device_diagnostics_obfuscates_ids_and_serial(
    monkeypatch, mock_hass
):
    monkeypatch.setitem(
        sys.modules, "alexapy", SimpleNamespace(hide_serial=lambda _s: "12...90")
    )

    entry = SimpleNamespace(
        entry_id="entry123",
        title="Alexa Media Player (daniel@example.com)",
        domain=DOMAIN,
        version=1,
        minor_version=0,
        data={"email": "daniel@example.com"},
        options={},
    )

    device = SimpleNamespace(
        id="device_id_ABCDEFGH",
        name="Kitchen Echo",
        name_by_user="Kitchen",
        manufacturer="Amazon",
        model="Echo",
        sw_version="1.0",
        serial_number="G090X01234567890",
        identifiers={("alexa_media", "identifier_ABCDEFGH")},
        via_device_id="via_id_ABCDEFGH",
    )

    out = await async_get_device_diagnostics(mock_hass, entry, device)

    assert out["device"]["id"] != "device_id_ABCDEFGH"
    assert out["device"]["via_device_id"] != "via_id_ABCDEFGH"
    assert out["device"]["serial_number"] != "G090X01234567890"


@pytest.mark.asyncio
async def test_async_get_config_entry_diagnostics_domain_data_not_mapping_is_robust(
    mock_hass, monkeypatch
):
    monkeypatch.setattr(
        "custom_components.alexa_media.diagnostics._summarize_amp_entry_runtime",
        lambda v: {"present": v is not None},
    )
    monkeypatch.setattr(
        "custom_components.alexa_media.diagnostics._summarize_amp_domain",
        lambda domain_data, entry: {"present": domain_data is not None},
    )

    entry = SimpleNamespace(
        entry_id="entry123",
        title="Alexa Media Player (daniel@example.com)",
        domain=DOMAIN,
        version=1,
        minor_version=0,
        data={},
        options={},
    )

    mock_hass.data[DOMAIN] = "not-a-mapping"

    out = await async_get_config_entry_diagnostics(mock_hass, entry)

    assert out["account"]["searched_for_coordinators_in"] == []
    assert out["account"]["coordinator_count"] == 0
    assert out["account"]["coordinators"] == []


# --------------------------------------------------------------------------- #
# Small serialization / sampling helpers
# --------------------------------------------------------------------------- #


def test_safe_dt_serializes_datetime_and_rejects_other():
    assert _safe_dt(datetime(2020, 1, 2, 3, 4, 5)).startswith("2020-01-02T03:04:05")
    assert _safe_dt("not-a-datetime") is None


def test_maybe_len_counts_containers_only():
    assert _maybe_len([1, 2, 3]) == 3
    assert _maybe_len({"a": 1}) == 1
    assert _maybe_len((1,)) == 1
    assert _maybe_len({1, 2}) == 2
    # Strings and scalars are intentionally not treated as containers here.
    assert _maybe_len("string") is None
    assert _maybe_len(42) is None


def test_maybe_keys_falls_back_when_hide_email_raises(monkeypatch):
    def _raise(_s):
        raise ValueError("boom")

    monkeypatch.setitem(sys.modules, "alexapy", SimpleNamespace(hide_email=_raise))

    keys = _maybe_keys({"daniel@example.com": 1})
    assert keys is not None
    assert all("daniel@example.com" not in k for k in keys)


def test_maybe_keys_returns_none_when_keys_raises():
    class RaisingKeys(Mapping):
        def __getitem__(self, key):
            raise KeyError(key)

        def __iter__(self):
            return iter(())

        def __len__(self):
            return 0

        def keys(self):
            raise TypeError("boom")

    assert _maybe_keys(RaisingKeys()) is None


def test_sample_names_from_mappings_and_objects():
    # A mapping with more matching names than `limit` exercises the break path.
    big_map = {str(i): {"name": f"Echo {i}"} for i in range(20)}
    out = _sample_names(big_map, limit=5)
    assert out is not None
    assert len(out) == 5

    # A list mixing plain objects (``.name``) and mappings (``deviceName``).
    mixed = [SimpleNamespace(name="ObjName"), {"deviceName": "Echo Bed"}]
    names = _sample_names(mixed)
    assert "ObjName" in names
    assert "Echo Bed" in names


def test_sample_names_list_break_and_scalar():
    many = [{"name": f"N{i}"} for i in range(20)]
    assert len(_sample_names(many, limit=5)) == 5
    # Scalars and name-less entries yield None.
    assert _sample_names(42) is None
    assert _sample_names([{"no_name_here": 1}]) is None


def _make_coordinator(hass: MagicMock) -> DataUpdateCoordinator:
    return DataUpdateCoordinator(
        hass,
        logger=logging.getLogger(__name__),
        config_entry=None,
        name="amp",
        update_interval=timedelta(seconds=30),
    )


def test_find_coordinators_handles_visited_and_dataclass(mock_hass):
    coordinator = _make_coordinator(mock_hass)

    # A shared reference must only be walked once (visited guard).
    shared = {"c": coordinator}
    found = _find_coordinators({"a": shared, "b": shared})
    assert found == [coordinator]

    # Dataclass attributes are walked; unreadable fields are skipped silently.
    @dataclass
    class Holder:
        coord: object
        missing: object

    holder = Holder(coordinator, "x")
    del holder.missing  # getattr now raises -> swallowed by inner try/except
    assert coordinator in _find_coordinators(holder)


def test_find_coordinators_dataclass_vars_fallback(mock_hass, monkeypatch):
    coordinator = _make_coordinator(mock_hass)

    @dataclass
    class Holder:
        coord: object

    def _raise(_x):
        raise TypeError("no fields")

    # Forcing fields() to raise exercises the vars() fallback branch.
    monkeypatch.setattr("custom_components.alexa_media.diagnostics.fields", _raise)
    assert coordinator in _find_coordinators(Holder(coordinator))


def test_find_coordinators_dataclass_vars_fallback_also_fails(monkeypatch):
    # A slots dataclass has no __dict__, so vars() raises inside the fallback.
    @dataclass(slots=True)
    class SlotHolder:
        coord: object

    def _raise(_x):
        raise TypeError("no fields")

    monkeypatch.setattr("custom_components.alexa_media.diagnostics.fields", _raise)
    # Both the fields() walk and the vars() fallback fail -> swallowed, no crash.
    assert _find_coordinators(SlotHolder(MagicMock())) == []


# --------------------------------------------------------------------------- #
# Coordinator data + AMP runtime/domain summaries
# --------------------------------------------------------------------------- #


def test_summarize_coordinator_data_mapping():
    cdata = {
        "uuid-1": [1, 2],
        "uuid-2": {"x": 1},
        "account": {"a": 1},
        "last_called": {"timestamp": datetime(2021, 5, 6), "summary": "did-thing"},
        "devices": {"d1": {"name": "Kitchen Echo"}},
    }
    out = _summarize_coordinator_data(cdata)

    assert out["data_key_count"] == len(cdata)
    assert out["data_key_types_sample"]
    assert out["data_value_types_sample"]
    assert out["account_count"] == 1
    assert out["last_called"]["summary"] == "did-thing"
    assert out["last_called"]["timestamp"].startswith("2021-05-06")
    assert out["devices_sample_names"] == ["Kitchen Echo"]


def test_summarize_coordinator_data_list_and_scalar():
    list_out = _summarize_coordinator_data([{"name": "Echo One"}, {"name": "Echo Two"}])
    assert list_out["data_len"] == 2
    assert list_out["sample_names"]

    assert _summarize_coordinator_data("scalar")["data_type"] == "str"
    assert _summarize_coordinator_data(None) == {}


def test_summarize_amp_entry_runtime_names_and_non_mapping():
    out = _summarize_amp_entry_runtime({"devices": [{"name": "Kitchen Echo"}]})
    assert out["devices_count"] == 1
    assert out["devices_sample_names"] == ["Kitchen Echo"]

    scalar = _summarize_amp_entry_runtime("scalar")
    assert scalar["present"] is True
    assert scalar["runtime_type"] == "str"


def test_summarize_amp_domain_non_mapping_returns_early():
    entry = SimpleNamespace(entry_id="e1", title="T", data={}, options={})
    out = _summarize_amp_domain("scalar", entry)

    assert out["domain_data_present"] is True
    assert out["domain_data_type"] == "str"
    # Early-return before the key-sampling section.
    assert "domain_keys" not in out


def test_summarize_amp_domain_with_buckets():
    entry = SimpleNamespace(
        entry_id="e1",
        title="Alexa Media Player (daniel@example.com)",
        data={"email": "daniel@example.com"},
        options={},
    )
    domain = {"accounts": {"acc1": {"name": "Account One"}}}
    out = _summarize_amp_domain(domain, entry)

    assert out["accounts_type"] == "dict"
    assert out["accounts_len"] == 1
    assert out["accounts_sample_names"] == ["Account One"]
    assert out["has_entry_id_key"] is False
    assert out["has_title_key"] is False


@pytest.mark.asyncio
async def test_config_entry_diagnostics_finds_coordinator_under_entry_runtime(
    mock_hass,
):
    coordinator = _make_coordinator(mock_hass)
    entry = SimpleNamespace(
        entry_id="e1",
        title="Alexa Media Player (daniel@example.com)",
        domain=DOMAIN,
        version=1,
        minor_version=0,
        data={"email": "daniel@example.com"},
        options={},
    )
    mock_hass.data[DOMAIN] = {"e1": {"coordinator": coordinator}}

    out = await async_get_config_entry_diagnostics(mock_hass, entry)

    account = out["account"]
    assert "hass.data[DOMAIN][entry_id]" in account["searched_for_coordinators_in"]
    assert account["coordinator_count"] == 1
    assert len(account["coordinators"]) == 1


@pytest.mark.asyncio
async def test_device_diagnostics_serial_fallback_when_hide_serial_missing(
    monkeypatch, mock_hass
):
    # alexapy present but without hide_serial -> `from alexapy import ...` ImportError.
    monkeypatch.setitem(sys.modules, "alexapy", SimpleNamespace())

    entry = SimpleNamespace(
        entry_id="e1",
        title="Alexa Media Player (daniel@example.com)",
        domain=DOMAIN,
        version=1,
        minor_version=0,
        data={"email": "daniel@example.com"},
        options={},
    )
    device = SimpleNamespace(
        id="device_id_ABCDEFGH",
        name="Kitchen Echo",
        name_by_user="Kitchen",
        manufacturer="Amazon",
        model="Echo",
        sw_version="1.0",
        serial_number="G090X01234567890",
        identifiers={("alexa_media", "identifier_ABCDEFGH")},
        via_device_id="via_id_ABCDEFGH",
    )

    out = await async_get_device_diagnostics(mock_hass, entry, device)

    assert out["device"]["serial_number"] == _obfuscate_identifier("G090X01234567890")
