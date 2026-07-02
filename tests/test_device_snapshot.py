"""Tests for the optimistic-boot device snapshot (device_snapshot.py + glue).

Covers the DeviceSnapshotStore wrapper, the setup-time restore helper
(_restore_device_snapshot), the background boot finisher
(_async_finish_optimistic_boot) and the coordinator-side save step.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.const import CONF_EMAIL

import custom_components.alexa_media as amp
from custom_components.alexa_media.const import (
    CONF_EXCLUDE_DEVICES,
    CONF_INCLUDE_DEVICES,
    DATA_ALEXAMEDIA,
)
from custom_components.alexa_media.device_snapshot import DeviceSnapshotStore
from custom_components.alexa_media.setup.coordinator_data import (
    _save_device_snapshot,
)

EMAIL = "user@example.com"

# --------------------------------------------------------------------------- #
# DeviceSnapshotStore
# --------------------------------------------------------------------------- #


async def test_store_load_returns_data():
    store = DeviceSnapshotStore(MagicMock(), "entry1")
    store._store.async_load = AsyncMock(return_value={"media_player": {"S1": {}}})
    assert await store.async_load() == {"media_player": {"S1": {}}}


async def test_store_load_swallows_corruption():
    # A corrupt snapshot must never block the classic boot path.
    store = DeviceSnapshotStore(MagicMock(), "entry1")
    store._store.async_load = AsyncMock(side_effect=ValueError("corrupt"))
    assert await store.async_load() is None


async def test_store_delay_save_serializes_live_devices():
    store = DeviceSnapshotStore(MagicMock(), "entry1")
    store._store.async_delay_save = MagicMock()
    devices = {"media_player": {"S1": {"accountName": "Echo"}}}
    store.async_delay_save(devices)
    (data_func, _delay), _ = (
        store._store.async_delay_save.call_args.args,
        store._store.async_delay_save.call_args.kwargs,
    )
    # The provider returns the live mapping so the freshest state is written.
    assert data_func() is devices


async def test_store_remove_delegates():
    store = DeviceSnapshotStore(MagicMock(), "entry1")
    store._store.async_remove = AsyncMock()
    await store.async_remove()
    store._store.async_remove.assert_awaited_once()


# --------------------------------------------------------------------------- #
# _restore_device_snapshot
# --------------------------------------------------------------------------- #


def _snapshot_env(
    snapshot,
    *,
    include="",
    exclude="",
    devices_already=None,
    with_store=True,
):
    hass = MagicMock()
    hass.config_entries.async_forward_entry_setups = AsyncMock()
    store = MagicMock()
    store.async_load = AsyncMock(return_value=snapshot)
    account = {
        "devices": {"media_player": devices_already or {}},
    }
    if with_store:
        account["device_snapshot_store"] = store
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {EMAIL: account}}}
    entry = MagicMock()
    entry.data = {
        CONF_EMAIL: EMAIL,
        CONF_INCLUDE_DEVICES: include,
        CONF_EXCLUDE_DEVICES: exclude,
    }
    return hass, entry, account, store


async def test_restore_returns_false_without_store():
    hass, entry, _, _ = _snapshot_env(None, with_store=False)
    assert await amp._restore_device_snapshot(hass, entry, EMAIL) is False


async def test_restore_returns_false_without_snapshot():
    hass, entry, _, _ = _snapshot_env(None)
    assert await amp._restore_device_snapshot(hass, entry, EMAIL) is False
    hass.config_entries.async_forward_entry_setups.assert_not_awaited()


async def test_restore_returns_false_when_devices_already_live():
    # Relogin path: setup_alexa re-runs while entities already exist.
    snapshot = {"media_player": {"S1": {"accountName": "Echo"}}}
    hass, entry, _, store = _snapshot_env(
        snapshot, devices_already={"S9": {"accountName": "Live"}}
    )
    assert await amp._restore_device_snapshot(hass, entry, EMAIL) is False
    store.async_load.assert_not_awaited()


async def test_restore_populates_devices_and_forwards_platforms():
    snapshot = {
        "media_player": {"S1": {"accountName": "Echo Salon"}},
        "light": [{"id": "L1"}],
        "temperature": [{"id": "T1"}],
    }
    hass, entry, account, _ = _snapshot_env(snapshot)
    assert await amp._restore_device_snapshot(hass, entry, EMAIL) is True
    assert account["devices"]["media_player"] == {"S1": {"accountName": "Echo Salon"}}
    assert account["devices"]["light"] == [{"id": "L1"}]
    assert account["devices"]["temperature"] == [{"id": "T1"}]
    hass.config_entries.async_forward_entry_setups.assert_awaited_once_with(
        entry, amp.ALEXA_COMPONENTS
    )


async def test_restore_applies_current_exclude_filter():
    # A device excluded since the snapshot was written must not resurrect.
    snapshot = {
        "media_player": {
            "S1": {"accountName": "Echo Salon"},
            "S2": {"accountName": "Echo Banni"},
        }
    }
    hass, entry, account, _ = _snapshot_env(snapshot, exclude="Echo Banni")
    assert await amp._restore_device_snapshot(hass, entry, EMAIL) is True
    assert set(account["devices"]["media_player"]) == {"S1"}


async def test_restore_applies_current_include_filter():
    snapshot = {
        "media_player": {
            "S1": {"accountName": "Echo Salon"},
            "S2": {"accountName": "Echo Autre"},
        }
    }
    hass, entry, account, _ = _snapshot_env(snapshot, include="Echo Salon")
    assert await amp._restore_device_snapshot(hass, entry, EMAIL) is True
    assert set(account["devices"]["media_player"]) == {"S1"}


async def test_restore_returns_false_when_all_filtered_out():
    snapshot = {"media_player": {"S2": {"accountName": "Echo Banni"}}}
    hass, entry, _, _ = _snapshot_env(snapshot, exclude="Echo Banni")
    assert await amp._restore_device_snapshot(hass, entry, EMAIL) is False
    hass.config_entries.async_forward_entry_setups.assert_not_awaited()


# --------------------------------------------------------------------------- #
# _async_finish_optimistic_boot
# --------------------------------------------------------------------------- #


async def test_finish_optimistic_boot_refreshes_and_sets_http2():
    hass = MagicMock()
    account = {}
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {EMAIL: account}}}
    coordinator = MagicMock()
    coordinator.async_refresh = AsyncMock()
    http2_task = asyncio.get_event_loop().create_future()
    http2_task.set_result("push-client")
    await amp._async_finish_optimistic_boot(
        hass, EMAIL, coordinator, http2_task, True, 60
    )
    coordinator.async_refresh.assert_awaited_once()
    assert account["http2"] == "push-client"
    coordinator.set_http2_status.assert_called_once_with(True)


async def test_finish_optimistic_boot_generic_coordinator_interval():
    from datetime import timedelta

    hass = MagicMock()
    account = {}
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {EMAIL: account}}}
    coordinator = MagicMock()
    coordinator.async_refresh = AsyncMock()
    http2_task = asyncio.get_event_loop().create_future()
    http2_task.set_result(None)
    await amp._async_finish_optimistic_boot(
        hass, EMAIL, coordinator, http2_task, False, 60
    )
    assert account["http2"] is None
    assert coordinator.update_interval == timedelta(seconds=60)


# --------------------------------------------------------------------------- #
# coordinator-side save step
# --------------------------------------------------------------------------- #


def test_save_device_snapshot_delegates_to_store():
    store = MagicMock()
    devices = {"media_player": {"S1": {}}}
    _save_device_snapshot({"device_snapshot_store": store, "devices": devices})
    store.async_delay_save.assert_called_once_with(devices)


def test_save_device_snapshot_noop_without_store():
    _save_device_snapshot({"devices": {}})  # must not raise


# --------------------------------------------------------------------------- #
# async_remove_entry purges the snapshot
# --------------------------------------------------------------------------- #


async def test_remove_entry_purges_snapshot():
    hass = MagicMock()
    hass.config.path = MagicMock(side_effect=lambda *a: "/".join(str(x) for x in a))
    entry = MagicMock()
    entry.data = {"email": EMAIL}
    entry.entry_id = "entry1"
    with (
        patch.object(amp, "DeviceSnapshotStore") as store_cls,
        patch.object(amp, "AlexaLogin") as login_cls,
    ):
        store_cls.return_value.async_remove = AsyncMock()
        login_cls.return_value.delete_cookiefile = AsyncMock()
        await amp.async_remove_entry(hass, entry)
    store_cls.assert_called_once_with(hass, "entry1")
    store_cls.return_value.async_remove.assert_awaited_once()
