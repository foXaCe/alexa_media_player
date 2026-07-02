"""Tests for setup.coordinator_data.async_update_data guards + error handling."""

import asyncio
import contextlib
import time
from unittest.mock import AsyncMock, MagicMock, patch

from alexapy import AlexapyConnectionError, AlexapyLoginError
from alexapy.errors import AlexapyTooManyRequestsError
from homeassistant.const import CONF_EMAIL
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed
import pytest

from custom_components.alexa_media.const import (
    CONF_DEBUG,
    CONF_EXCLUDE_DEVICES,
    CONF_EXTENDED_ENTITY_DISCOVERY,
    CONF_INCLUDE_DEVICES,
    COORDINATOR_429_RETRY_AFTER_S,
    DATA_ALEXAMEDIA,
    HTTP2_ERROR_THRESHOLD,
    LOGIN_ERROR_RETRY_TOLERANCE,
)
from custom_components.alexa_media.setup.context import SetupContext
from custom_components.alexa_media.setup.coordinator_data import (
    _push_healthy,
    async_update_data,
)

_MOD = "custom_components.alexa_media.setup.coordinator_data"
EMAIL = "user@example.com"


def _ctx(accounts, *, include="", exclude="", metrics=None):
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": accounts}}
    hass.config_entries.async_forward_entry_setups = AsyncMock()

    def _eat_coro(coro, name=None):
        # async_create_background_task receives a real coroutine; close it so the
        # event loop does not warn about a coroutine that was never awaited.
        if asyncio.iscoroutine(coro):
            coro.close()
        return MagicMock()

    hass.async_create_background_task = MagicMock(side_effect=_eat_coro)
    entry = MagicMock()
    entry.data = {
        CONF_EMAIL: EMAIL,
        CONF_INCLUDE_DEVICES: include,
        CONF_EXCLUDE_DEVICES: exclude,
    }
    entry.entry_id = "entry1"
    return (
        SetupContext(hass=hass, config_entry=entry, email=EMAIL, metrics=metrics),
        hass,
    )


def _login(network_ok=True):
    login = MagicMock()
    login.email = EMAIL
    login.close_requested = not network_ok
    login.session.closed = False
    login.status = {"login_successful": True}
    return login


def _full_account(login):
    return {
        "login_obj": login,
        "entities": {
            "media_player": {},
            "sensor": {},
            "light": [],
            "binary_sensor": [],
            "alarm_control_panel": {},
            "smart_switch": [],
        },
        "auth_info": None,
        "new_devices": True,
        "should_get_network": False,
        "first_run": False,
        "options": {CONF_EXTENDED_ENTITY_DISCOVERY: False, CONF_DEBUG: False},
    }


def _patch_api(get_devices_mock):
    """Patch the serial helpers and AlexaAPI; get_devices uses the given mock."""
    api = MagicMock()
    api.get_devices = get_devices_mock
    api.get_bluetooth = AsyncMock(return_value={})
    api.get_device_preferences = AsyncMock(return_value={})
    api.get_dnd_state = AsyncMock(return_value={})
    api.get_authentication = AsyncMock(return_value={})
    return (
        patch(f"{_MOD}._existing_serials", return_value=set()),
        patch(f"{_MOD}._entity_backed_serials", return_value=set()),
        patch(f"{_MOD}.AlexaAPI", api),
    )


# --- guards -----------------------------------------------------------------


async def test_returns_none_when_account_missing():
    ctx, _ = _ctx({})
    assert await async_update_data(ctx) is None


async def test_returns_none_when_login_obj_missing():
    ctx, _ = _ctx({EMAIL: {}})
    assert await async_update_data(ctx) is None


async def test_returns_none_when_network_not_allowed():
    ctx, _ = _ctx({EMAIL: {"login_obj": _login(network_ok=False)}})
    assert await async_update_data(ctx) is None


# --- error handling ---------------------------------------------------------


async def test_raises_update_failed_on_connection_error():
    ctx, _ = _ctx({EMAIL: _full_account(_login())})
    p1, p2, p3 = _patch_api(AsyncMock(side_effect=AlexapyConnectionError("boom")))
    with p1, p2, p3, pytest.raises(UpdateFailed):
        await async_update_data(ctx)


async def test_transient_login_error_retries_via_update_failed():
    # The first login error is treated as a transient boot blip: surfaced as
    # UpdateFailed (-> ConfigEntryNotReady re-bootstrap), NOT escalated to reauth.
    account = _full_account(_login())
    ctx, hass = _ctx({EMAIL: account})
    p1, p2, p3 = _patch_api(AsyncMock(side_effect=AlexapyLoginError("blip")))
    with p1, p2, p3, pytest.raises(UpdateFailed):
        await async_update_data(ctx)
    assert account["setup_login_error_count"] == 1
    fired_events = [call.args[0] for call in hass.bus.async_fire.call_args_list]
    assert "alexa_media_relogin_required" not in fired_events


async def test_login_error_escalates_to_relogin_after_tolerance():
    # Once the transient budget is spent, a further login error escalates to a
    # manual reauth and resets the counter.
    account = _full_account(_login())
    account["setup_login_error_count"] = LOGIN_ERROR_RETRY_TOLERANCE
    ctx, hass = _ctx({EMAIL: account})
    p1, p2, p3 = _patch_api(AsyncMock(side_effect=AlexapyLoginError("bad")))
    with p1, p2, p3:
        result = await async_update_data(ctx)
    assert result is None
    fired_events = [call.args[0] for call in hass.bus.async_fire.call_args_list]
    assert "alexa_media_relogin_required" in fired_events
    assert account["setup_login_error_count"] == 0


async def test_too_many_requests_raises_update_failed_with_retry_after():
    ctx, _ = _ctx({EMAIL: _full_account(_login())})
    p1, p2, p3 = _patch_api(AsyncMock(side_effect=AlexapyTooManyRequestsError("rate")))
    with p1, p2, p3, pytest.raises(UpdateFailed) as excinfo:
        await async_update_data(ctx)
    assert excinfo.value.retry_after == COORDINATOR_429_RETRY_AFTER_S


async def test_cancelled_error_propagates():
    ctx, _ = _ctx({EMAIL: _full_account(_login())})
    p1, p2, p3 = _patch_api(AsyncMock(side_effect=asyncio.CancelledError))
    with p1, p2, p3, pytest.raises(asyncio.CancelledError):
        await async_update_data(ctx)


# --- richer fixtures for the full fetch/assemble path -----------------------


def _login_full(network_ok=True):
    """A login mock fleshed out enough to reach the end of async_update_data."""
    login = _login(network_ok)
    login.save_cookiefile = AsyncMock()
    login.access_token = "tok"
    login.refresh_token = "ref"
    login.expires_in = 3600
    login.mac_dms = "mac"
    login.code_verifier = "cv"
    login.authorization_code = "ac"
    login.url = "https://example.test"
    return login


def _rich_account(login, entities=None, **overrides):
    """Account dict with every key async_update_data reads on the happy path."""
    acct = {
        "login_obj": login,
        "entities": entities
        if entities is not None
        else {
            "media_player": {},
            "sensor": {},
            "light": [],
            "binary_sensor": [],
            "alarm_control_panel": {},
            "smart_switch": [],
        },
        "auth_info": None,
        "new_devices": True,
        "should_get_network": False,
        "first_run": False,
        "options": {CONF_EXTENDED_ENTITY_DISCOVERY: False, CONF_DEBUG: False},
        "devices": {"media_player": {}, "switch": {}},
        "excluded": {},
    }
    acct.update(overrides)
    return acct


def _api_full(
    devices=None,
    bluetooth=None,
    preferences=None,
    dnd=None,
    auth=None,
    network=None,
):
    """AlexaAPI stand-in whose classmethods are AsyncMocks with given returns."""
    api = MagicMock()
    api.get_devices = AsyncMock(return_value=[] if devices is None else devices)
    api.get_bluetooth = AsyncMock(return_value={} if bluetooth is None else bluetooth)
    api.get_device_preferences = AsyncMock(
        return_value={} if preferences is None else preferences
    )
    api.get_dnd_state = AsyncMock(return_value={} if dnd is None else dnd)
    api.get_authentication = AsyncMock(return_value={} if auth is None else auth)
    api.get_network_details = AsyncMock(return_value=network)
    return api


def _full_patches(api, *, existing_serials=(), entity_backed=(), dr_mock=None):
    """Patch every network/registry boundary touched after the guard checks."""
    if dr_mock is None:
        dr_mock = MagicMock()
        dr_mock.async_entries_for_config_entry.return_value = []
    return [
        patch(f"{_MOD}._existing_serials", return_value=set(existing_serials)),
        patch(f"{_MOD}._entity_backed_serials", return_value=set(entity_backed)),
        patch(f"{_MOD}._entity_backed_device_identifiers", return_value=set()),
        patch(f"{_MOD}._network_allowed", return_value=True),
        patch(f"{_MOD}.AlexaAPI", api),
        patch(f"{_MOD}.dr", dr_mock),
        patch(f"{_MOD}._async_update_last_called_global", MagicMock()),
    ]


@contextlib.contextmanager
def _applied(patches):
    with contextlib.ExitStack() as stack:
        for p in patches:
            stack.enter_context(p)
        yield


# --- happy path -------------------------------------------------------------


async def test_happy_path_assembles_account_and_loads_platforms():
    login = _login_full()
    account = _rich_account(login)
    metrics = MagicMock()
    metrics.api_cache.get.return_value = None
    ctx, hass = _ctx({EMAIL: account}, metrics=metrics)
    s1 = {"serialNumber": "S1", "accountName": "Echo", "capabilities": ["MUSIC_SKILL"]}
    # Lacks music/alarm/reminder capability -> filtered out at the skill check.
    s2 = {"serialNumber": "S2", "accountName": "NoCap", "capabilities": ["OTHER"]}
    api = _api_full(
        devices=[s1, s2],
        bluetooth={"bluetoothStates": [{"deviceSerialNumber": "S1"}]},
        preferences={
            "devicePreferences": [
                {"deviceSerialNumber": "S1", "locale": "en-US", "timeZoneId": "TZ"}
            ]
        },
        dnd={
            "doNotDisturbDeviceStatusList": [
                {"deviceSerialNumber": "S1", "enabled": True}
            ]
        },
        auth={"a": 1},
    )
    # A leftover transient-error count must be cleared by a successful fetch.
    account["setup_login_error_count"] = 3
    with _applied(_full_patches(api)):
        result = await async_update_data(ctx)

    assert result == {}
    assert account["setup_login_error_count"] == 0
    media_player = account["devices"]["media_player"]
    assert media_player["S1"] is s1
    assert "S2" not in media_player
    assert s1["dnd"] is True
    assert s1["locale"] == "en-US"
    assert "bluetooth_state" in s1
    assert s1["auth_info"] == {"a": 1}
    assert account["new_devices"] is False
    assert account["first_run"] is False
    hass.config_entries.async_forward_entry_setups.assert_awaited_once()
    login.save_cookiefile.assert_awaited_once()
    metrics.record_api_call.assert_called_once()
    metrics.api_cache.cache_set.assert_called_once_with(
        f"{EMAIL}_entry1_devices", [s1, s2]
    )
    metrics.record_boot_stage.assert_called_once()


async def test_uses_cached_devices_skips_get_devices():
    login = _login_full()
    account = _rich_account(login, new_devices=False)
    cached = [
        {"serialNumber": "C1", "accountName": "Cached", "capabilities": ["MUSIC_SKILL"]}
    ]
    metrics = MagicMock()
    metrics.api_cache.get.return_value = cached
    ctx, _ = _ctx({EMAIL: account}, metrics=metrics)
    api = _api_full()
    with _applied(_full_patches(api)):
        result = await async_update_data(ctx)

    assert result == {}
    api.get_devices.assert_not_called()
    assert account["devices"]["media_player"]["C1"] is cached[0]
    metrics.record_api_call.assert_called_once()
    metrics.api_cache.cache_set.assert_not_called()


async def test_entities_to_monitor_collected_and_returned():
    login = _login_full()
    temp_on = MagicMock(enabled=True)
    temp_on.alexa_entity_id = "temp1"
    temp_off = MagicMock(enabled=False)
    aq_on = MagicMock(enabled=True)
    aq_on.alexa_entity_id = "aq1"
    airq_single = MagicMock(enabled=True)  # legacy single (non-dict) Air_Quality
    airq_single.alexa_entity_id = "aqs"
    light_on = MagicMock(enabled=True)
    light_on.alexa_entity_id = "light1"
    light_off = MagicMock(enabled=False)
    bs_on = MagicMock(enabled=True)
    bs_on.alexa_entity_id = "bs1"
    bs_off = MagicMock(enabled=False)
    guard_on = MagicMock(enabled=True)
    guard_on.unique_id = "guard1"
    guard_off = MagicMock(enabled=False)
    ss_on = MagicMock(enabled=True)
    ss_on.alexa_entity_id = "ss1"
    ss_off = MagicMock(enabled=False)
    entities = {
        "media_player": {},
        "sensor": {
            "S1": {"Temperature": temp_on, "Air_Quality": {"u1": aq_on}},
            "S2": "not-a-dict",  # skipped (not a dict)
            "S3": {"Temperature": temp_off},  # disabled temp -> ignored
            "S4": {"Air_Quality": airq_single},  # legacy backwards-compat branch
        },
        "light": [light_on, light_off],
        "binary_sensor": [bs_on, bs_off],
        "alarm_control_panel": {"g1": guard_on, "g2": guard_off},
        "smart_switch": [ss_on, ss_off],
    }
    notif_task = MagicMock()
    notif_task.done.return_value = False  # already-running notifications branch
    trigger = MagicMock()  # last_called probe trigger (callable branch)
    account = _rich_account(
        login,
        entities=entities,
        notifications_init_task=notif_task,
        last_called_probe_trigger=trigger,
    )
    ctx, hass = _ctx({EMAIL: account})
    api = _api_full(devices=[])
    get_ed = AsyncMock(return_value={"temp1": {"state": 1}})
    with _applied([*_full_patches(api), patch(f"{_MOD}.get_entity_data", get_ed)]):
        result = await async_update_data(ctx)

    assert result == {"temp1": {"state": 1}}
    get_ed.assert_awaited_once()
    monitored = set(get_ed.await_args.args[1])
    assert monitored == {"temp1", "aq1", "aqs", "light1", "bs1", "guard1", "ss1"}
    trigger.assert_called_once_with("POLL_REFRESH", None)
    # notifications task already running + probe trigger callable -> no bg tasks
    hass.async_create_background_task.assert_not_called()


async def test_network_discovery_success():
    login = _login_full()
    light_on = MagicMock(enabled=True)
    light_on.alexa_entity_id = "light1"
    entities = {
        "media_player": {},
        "sensor": {},
        "light": [light_on],
        "binary_sensor": [],
        "alarm_control_panel": {},
        "smart_switch": [],
    }
    account = _rich_account(
        login, entities=entities, should_get_network=True, first_run=True
    )
    ctx, _ = _ctx({EMAIL: account})
    api = _api_full(devices=[], network=[{"id": "n1"}])
    # guard type is always monitored; light type is skipped (extended discovery off)
    parsed = {
        "guard": [{"id": "g1", "name": "G"}],
        "light": [{"id": "l1", "name": "L"}],
    }
    parse = MagicMock(return_value=parsed)
    get_ed = AsyncMock(return_value={"g1": {"x": 1}})
    extra = [
        patch(f"{_MOD}.parse_alexa_entities", parse),
        patch(f"{_MOD}.get_entity_data", get_ed),
    ]
    with _applied(_full_patches(api) + extra):
        result = await async_update_data(ctx)

    assert result == {"g1": {"x": 1}}
    assert account["should_get_network"] is False
    parse.assert_called_once_with([{"id": "n1"}], debug=False)
    monitored_calls = [list(c.args[1]) for c in get_ed.await_args_list]
    assert ["g1"] in monitored_calls


async def test_network_discovery_empty_response_keeps_flag():
    login = _login_full()
    account = _rich_account(login, should_get_network=True, new_devices=False)
    ctx, _ = _ctx({EMAIL: account})
    api = _api_full(devices=[], network=None)
    parse = MagicMock(return_value={})
    get_ed = AsyncMock(return_value={})
    extra = [
        patch(f"{_MOD}.parse_alexa_entities", parse),
        patch(f"{_MOD}.get_entity_data", get_ed),
    ]
    with _applied(_full_patches(api) + extra):
        result = await async_update_data(ctx)

    assert result == {}
    # No usable network response -> flag stays set so the next cycle retries.
    assert account["should_get_network"] is True
    parse.assert_called_once_with(None, debug=False)


# --- include / exclude filtering --------------------------------------------


async def test_include_filter_excludes_non_included_device():
    login = _login_full()
    account = _rich_account(login)
    ctx, _ = _ctx({EMAIL: account}, include="Keep")
    keep = {
        "serialNumber": "S_keep",
        "accountName": "Keep",
        "capabilities": ["MUSIC_SKILL"],
    }
    drop = {
        "serialNumber": "S_drop",
        "accountName": "Drop",
        "capabilities": ["MUSIC_SKILL"],
        "appDeviceList": [{"serialNumber": "APP_drop"}],
    }
    api = _api_full(devices=[keep, drop], auth={"a": 1})
    with _applied(_full_patches(api)):
        result = await async_update_data(ctx)

    assert result == {}
    assert "S_keep" in account["devices"]["media_player"]
    assert "S_drop" not in account["devices"]["media_player"]
    assert "S_drop" in account["excluded"]
    assert "APP_drop" in account["excluded"]


async def test_exclude_filter_drops_excluded_device():
    login = _login_full()
    account = _rich_account(login)
    ctx, _ = _ctx({EMAIL: account}, exclude="Bad")
    good = {
        "serialNumber": "S_good",
        "accountName": "Good",
        "capabilities": ["MUSIC_SKILL"],
    }
    bad = {
        "serialNumber": "S_bad",
        "accountName": "Bad",
        "capabilities": ["MUSIC_SKILL"],
        "appDeviceList": [{"serialNumber": "APP_bad"}],
    }
    api = _api_full(devices=[good, bad], auth={"a": 1})
    with _applied(_full_patches(api)):
        result = await async_update_data(ctx)

    assert result == {}
    assert "S_good" in account["devices"]["media_player"]
    assert "S_bad" in account["excluded"]
    assert "APP_bad" in account["excluded"]


async def test_existing_enabled_serial_triggers_refresh():
    login = _login_full()
    mp = MagicMock(enabled=True)
    mp.refresh = AsyncMock()
    entities = {
        "media_player": {"S1": mp},
        "sensor": {},
        "light": [],
        "binary_sensor": [],
        "alarm_control_panel": {},
        "smart_switch": [],
    }
    account = _rich_account(login, entities=entities)
    ctx, hass = _ctx({EMAIL: account})
    s1 = {"serialNumber": "S1", "accountName": "Echo", "capabilities": ["MUSIC_SKILL"]}
    api = _api_full(devices=[s1], auth={"a": 1})
    with _applied(_full_patches(api, existing_serials={"S1"})):
        result = await async_update_data(ctx)

    assert result == {}
    mp.refresh.assert_awaited_once_with(s1, skip_api=True)
    # Known serial -> not a new client -> platforms are not re-forwarded.
    hass.config_entries.async_forward_entry_setups.assert_not_called()


async def test_platform_load_timeout_raises_config_entry_not_ready():
    login = _login_full()
    account = _rich_account(login)
    ctx, hass = _ctx({EMAIL: account})
    hass.config_entries.async_forward_entry_setups = AsyncMock(side_effect=TimeoutError)
    s1 = {"serialNumber": "S1", "accountName": "Echo", "capabilities": ["MUSIC_SKILL"]}
    api = _api_full(devices=[s1], auth={"a": 1})
    with _applied(_full_patches(api)), pytest.raises(ConfigEntryNotReady):
        await async_update_data(ctx)


# --- _push_healthy ----------------------------------------------------------


def _http2(is_closed=False, last_ping=None):
    h = MagicMock()
    h.client.is_closed = is_closed
    h._last_ping = last_ping
    return h


def test_push_healthy_false_without_http2():
    assert _push_healthy({}) is False
    assert _push_healthy({"http2": None}) is False


def test_push_healthy_false_when_client_closed():
    assert _push_healthy({"http2": _http2(is_closed=True)}) is False


def test_push_healthy_false_at_error_threshold():
    acct = {"http2": _http2(), "http2error": HTTP2_ERROR_THRESHOLD}
    assert _push_healthy(acct) is False


def test_push_healthy_false_when_push_inactive():
    acct = {"http2": _http2(), "last_push_activity": time.time() - 100000}
    assert _push_healthy(acct) is False


def test_push_healthy_true_with_recent_ping():
    ping = MagicMock()
    ping.timestamp.return_value = time.time()
    assert _push_healthy({"http2": _http2(last_ping=ping)}) is True


def test_push_healthy_true_when_ping_stale():
    ping = MagicMock()
    ping.timestamp.return_value = time.time() - 100000
    assert _push_healthy({"http2": _http2(last_ping=ping)}) is True


def test_push_healthy_true_without_ping():
    assert _push_healthy({"http2": _http2(last_ping=None)}) is True


def test_push_healthy_true_when_ping_eval_raises():
    ping = MagicMock()
    ping.timestamp.side_effect = ValueError("bad")
    assert _push_healthy({"http2": _http2(last_ping=ping)}) is True


# --- network entity-data timeout --------------------------------------------


async def test_network_discovery_entity_data_timeout():
    login = _login_full()
    light_on = MagicMock(enabled=True)
    light_on.alexa_entity_id = "light1"
    entities = {
        "media_player": {},
        "sensor": {},
        "light": [light_on],
        "binary_sensor": [],
        "alarm_control_panel": {},
        "smart_switch": [],
    }
    account = _rich_account(login, entities=entities, should_get_network=True)
    ctx, _ = _ctx({EMAIL: account})
    api = _api_full(devices=[], network=[{"id": "n1"}])
    parse = MagicMock(return_value={"guard": [{"id": "g1", "name": "G"}]})
    # First call (gather, monitored entities) succeeds; the network entity-state
    # fetch (wrapped in wait_for) times out.
    get_ed = AsyncMock(side_effect=[{"discard": 1}, TimeoutError()])
    extra = [
        patch(f"{_MOD}.parse_alexa_entities", parse),
        patch(f"{_MOD}.get_entity_data", get_ed),
    ]
    with _applied(_full_patches(api) + extra):
        result = await async_update_data(ctx)

    # Timed-out entity fetch -> entity_state falls back to the empty default.
    assert result == {}
    assert account["should_get_network"] is False


# --- device-registry pruning ------------------------------------------------


async def test_prunes_stale_device_registry_entries():
    login = _login_full()
    account = _rich_account(login)
    ctx, _ = _ctx({EMAIL: account})
    s1 = {"serialNumber": "S1", "accountName": "Echo", "capabilities": ["MUSIC_SKILL"]}
    api = _api_full(devices=[s1], auth={"a": 1})

    kept = MagicMock()
    kept.identifiers = {("alexa_media", "S1")}  # matches a live media_player serial
    kept.id = "dev_keep"
    stale = MagicMock()
    stale.identifiers = {("alexa_media", "STALE")}  # no live device -> pruned
    stale.id = "dev_stale"

    dr_mock = MagicMock()
    dr_mock.async_entries_for_config_entry.return_value = [kept, stale]
    device_registry = dr_mock.async_get.return_value

    with _applied(_full_patches(api, dr_mock=dr_mock)):
        result = await async_update_data(ctx)

    assert result == {}
    device_registry.async_remove_device.assert_called_once_with("dev_stale")


# --- background notifications task body --------------------------------------


def _capture_bg(hass):
    """Make async_create_background_task stash coroutines instead of closing them."""
    captured = []

    def _grab(coro, name=None):
        captured.append(coro)
        return MagicMock()

    hass.async_create_background_task = MagicMock(side_effect=_grab)
    return captured


_PROC = "custom_components.alexa_media.setup.notifications.process_notifications"


async def test_background_notifications_success():
    login = _login_full()
    # callable probe trigger -> only the notifications coroutine is scheduled
    account = _rich_account(login, last_called_probe_trigger=MagicMock())
    ctx, hass = _ctx({EMAIL: account})
    captured = _capture_bg(hass)
    api = _api_full(devices=[])
    proc = AsyncMock()
    with _applied(_full_patches(api)), patch(_PROC, proc):
        await async_update_data(ctx)
        coros = [c for c in captured if asyncio.iscoroutine(c)]
        assert len(coros) == 1
        await coros[0]
    proc.assert_awaited_once()


async def test_background_notifications_retries_then_succeeds():
    login = _login_full()
    account = _rich_account(login, last_called_probe_trigger=MagicMock())
    ctx, hass = _ctx({EMAIL: account})
    captured = _capture_bg(hass)
    api = _api_full(devices=[])
    proc = AsyncMock(side_effect=[AlexapyConnectionError("x"), None])
    with (
        _applied(_full_patches(api)),
        patch(_PROC, proc),
        patch("asyncio.sleep", AsyncMock()),
    ):
        await async_update_data(ctx)
        coros = [c for c in captured if asyncio.iscoroutine(c)]
        await coros[0]
    assert proc.await_count == 2


async def test_background_notifications_retry_also_fails():
    login = _login_full()
    account = _rich_account(login, last_called_probe_trigger=MagicMock())
    ctx, hass = _ctx({EMAIL: account})
    captured = _capture_bg(hass)
    api = _api_full(devices=[])
    proc = AsyncMock(side_effect=AlexapyLoginError("down"))
    with (
        _applied(_full_patches(api)),
        patch(_PROC, proc),
        patch("asyncio.sleep", AsyncMock()),
    ):
        await async_update_data(ctx)
        coros = [c for c in captured if asyncio.iscoroutine(c)]
        # The retry failure is swallowed inside the background task.
        await coros[0]
    assert proc.await_count == 2
