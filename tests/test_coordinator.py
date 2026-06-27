"""Tests for AlexaMediaCoordinator (DataUpdateCoordinator subclass)."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from custom_components.alexa_media.const import DOMAIN, SCAN_INTERVAL
from custom_components.alexa_media.coordinator import AlexaMediaCoordinator


def _runtime_data(http2=None, config_entry=None):
    """Lightweight stand-in exposing the fields the coordinator reads."""
    return SimpleNamespace(http2=http2, config_entry=config_entry)


def _coordinator(runtime_data=None, update_method=None, scan_interval=None):
    return AlexaMediaCoordinator(
        MagicMock(),
        runtime_data,
        update_method or AsyncMock(),
        scan_interval=scan_interval,
    )


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------


def test_init_defaults_to_scan_interval_constant_and_domain_name():
    update_method = AsyncMock()
    coordinator = _coordinator(update_method=update_method)

    assert coordinator.name == DOMAIN
    assert coordinator._scan_interval == SCAN_INTERVAL.total_seconds()
    assert coordinator.update_interval == SCAN_INTERVAL
    assert coordinator.runtime_data is None
    assert coordinator.config_entry is None
    # The supplied fetch callable is wired through to the base coordinator.
    assert coordinator.update_method is update_method


def test_init_honours_custom_scan_interval_without_http2():
    coordinator = _coordinator(runtime_data=_runtime_data(), scan_interval=30)

    assert coordinator._scan_interval == 30
    assert coordinator.update_interval == timedelta(seconds=30)


def test_init_multiplies_interval_by_ten_when_http2_active():
    coordinator = _coordinator(
        runtime_data=_runtime_data(http2=object()), scan_interval=30
    )

    assert coordinator.update_interval == timedelta(seconds=300)


def test_init_passes_runtime_data_config_entry_to_base():
    entry = MagicMock()
    coordinator = _coordinator(runtime_data=_runtime_data(config_entry=entry))

    assert coordinator.config_entry is entry


def test_init_config_entry_none_when_runtime_data_has_none():
    coordinator = _coordinator(runtime_data=_runtime_data(config_entry=None))

    assert coordinator.config_entry is None


# ---------------------------------------------------------------------------
# set_http2_status
# ---------------------------------------------------------------------------


def test_set_http2_status_enabled_lengthens_interval():
    coordinator = _coordinator(runtime_data=_runtime_data(), scan_interval=30)
    assert coordinator.update_interval == timedelta(seconds=30)

    coordinator.set_http2_status(True)

    assert coordinator.update_interval == timedelta(seconds=300)


def test_set_http2_status_disabled_restores_base_interval():
    coordinator = _coordinator(
        runtime_data=_runtime_data(http2=object()), scan_interval=30
    )
    assert coordinator.update_interval == timedelta(seconds=300)

    coordinator.set_http2_status(False)

    assert coordinator.update_interval == timedelta(seconds=30)


def test_set_http2_status_noop_when_interval_unchanged():
    coordinator = _coordinator(runtime_data=_runtime_data(), scan_interval=30)
    before = coordinator.update_interval

    coordinator.set_http2_status(False)

    # Interval already matches -> the object is left untouched.
    assert coordinator.update_interval is before


# ---------------------------------------------------------------------------
# _async_update_data (base delegation to the wired update_method)
# ---------------------------------------------------------------------------


async def test_async_update_data_delegates_to_update_method():
    update_method = AsyncMock(return_value={"devices": 2})
    coordinator = _coordinator(update_method=update_method)

    result = await coordinator._async_update_data()

    assert result == {"devices": 2}
    update_method.assert_awaited_once()
