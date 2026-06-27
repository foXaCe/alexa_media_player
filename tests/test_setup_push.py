"""Tests for the simpler HTTP/2 push handlers in setup.push."""

from contextlib import contextmanager
from datetime import timedelta
import json
import time
from unittest.mock import AsyncMock, MagicMock, patch

from alexapy import AlexapyLoginError

from custom_components.alexa_media.const import DATA_ALEXAMEDIA
from custom_components.alexa_media.setup.context import SetupContext
from custom_components.alexa_media.setup.push import (
    http2_close_handler,
    http2_connect,
    http2_error_handler,
    http2_handler,
    http2_open_handler,
)

EMAIL = "user@example.com"
_PUSH = "custom_components.alexa_media.setup.push"
_CONNECT = f"{_PUSH}.http2_connect"
_CLIENT = f"{_PUSH}.HTTP2EchoClient"
_DISPATCH = f"{_PUSH}.async_dispatcher_send"
_EXISTING = f"{_PUSH}._existing_serials"
_ENTITY = f"{_PUSH}._entity_backed_serials"
_QUEUE = f"{_PUSH}._queue_last_called_activity"
_BLUETOOTH = f"{_PUSH}.setup_bluetooth.update_bluetooth_state"
_SCHEDULE_NOTIF = f"{_PUSH}.setup_notifications.schedule_notifications_refresh"
_DND = f"{_PUSH}.setup_dnd.update_dnd_state"


def _push_ctx(account):
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {EMAIL: account}}}
    login = MagicMock()
    login.email = EMAIL
    login.url = "amazon.fr"
    login.close_requested = False
    login.status = {"login_successful": True}
    login.session.closed = False
    ctx = SetupContext(
        hass=hass, config_entry=MagicMock(), email=EMAIL, login_obj=login
    )
    return ctx, hass, login


# --- http2_open_handler -----------------------------------------------------


async def test_http2_open_handler_resets_error_state():
    account = {"http2error": 5, "http2_lastattempt": 0.0}
    ctx, _, _ = _push_ctx(account)
    await http2_open_handler(ctx)
    assert account["http2error"] == 0
    assert account["http2_lastattempt"] > 0


# --- http2_close_handler early returns --------------------------------------


async def test_http2_close_handler_no_reconnect_when_close_requested():
    account = {"http2": "client", "http2error": 0, "http2_lastattempt": 0.0}
    ctx, _, login = _push_ctx(account)
    login.close_requested = True
    with patch(_CONNECT) as connect:
        await http2_close_handler(ctx)
    assert account["http2"] is None
    connect.assert_not_called()


async def test_http2_close_handler_no_reconnect_when_login_failed():
    account = {"http2": "client", "http2error": 0, "http2_lastattempt": 0.0}
    ctx, _, login = _push_ctx(account)
    login.status = {"login_successful": False}
    with patch(_CONNECT) as connect:
        await http2_close_handler(ctx)
    assert account["http2"] is None
    connect.assert_not_called()


# --- http2_error_handler ----------------------------------------------------


async def test_http2_error_handler_fires_relogin_on_login_error():
    account = {"http2error": 0, "http2": "client"}
    ctx, hass, _ = _push_ctx(account)
    await http2_error_handler(ctx, AlexapyLoginError("bad"))
    assert account["http2error"] == 5
    assert account["http2"] is None
    fired = [call.args[0] for call in hass.bus.async_fire.call_args_list]
    assert "alexa_media_relogin_required" in fired


async def test_http2_error_handler_increments_on_benign_error():
    account = {"http2error": 1, "http2": "client"}
    ctx, hass, _ = _push_ctx(account)
    await http2_error_handler(ctx, "transient blip")
    assert account["http2error"] == 2
    fired = [call.args[0] for call in hass.bus.async_fire.call_args_list]
    assert "alexa_media_relogin_required" not in fired


# --- shared helpers for http2_connect / http2_handler -----------------------


def _handler_account(**over):
    """Build an account dict with every key the push handlers touch."""
    account = {
        "coordinator": None,
        "http2_commands": {},
        "http2_activity": {"serials": {}},
        "excluded": {},
        "last_volumes": {},
        "last_equalizer": {},
        "new_devices": False,
    }
    account.update(over)
    return account


def _coordinator():
    coord = MagicMock()
    coord.async_request_refresh = AsyncMock()
    return coord


def _envelope(resource_metadata):
    """Wrap a (possibly malformed) resourceMetadata string in a push envelope."""
    return {
        "directive": {
            "payload": {"renderingUpdates": [{"resourceMetadata": resource_metadata}]}
        }
    }


def _msg(command, payload, *, timestamp=None):
    """Build a single-command http2push message envelope."""
    resource = {"command": command, "payload": json.dumps(payload)}
    if timestamp is not None:
        resource["timeStamp"] = timestamp
    return _envelope(json.dumps(resource))


@contextmanager
def _handler_patches(existing=()):
    """Patch the shared http2_handler boundaries; yield the dispatch mock."""
    with (
        patch(_EXISTING, return_value=list(existing)),
        patch(_ENTITY, return_value=set()),
        patch(_DISPATCH) as dispatch,
    ):
        yield dispatch


def _payloads(dispatch):
    """Return the payload dict from each async_dispatcher_send call."""
    return [call.args[2] for call in dispatch.call_args_list]


# --- http2_connect ----------------------------------------------------------


async def test_http2_connect_returns_none_without_login():
    ctx, _, _ = _push_ctx({})
    ctx.login_obj = None
    assert await http2_connect(ctx) is None


async def test_http2_connect_aborts_when_session_closed():
    ctx, _, login = _push_ctx({})
    login.session.closed = True
    with patch(_CLIENT) as client:
        result = await http2_connect(ctx)
    assert result is None
    client.assert_not_called()


async def test_http2_connect_starts_client_and_returns_it():
    ctx, _, login = _push_ctx({})
    login.session.closed = False
    with patch(_CLIENT) as client:
        client.return_value.async_run = AsyncMock()
        result = await http2_connect(ctx)
    assert result is client.return_value
    client.return_value.async_run.assert_awaited_once()


async def test_http2_connect_fires_relogin_on_login_error():
    ctx, hass, login = _push_ctx({})
    login.session.closed = False
    with patch(_CLIENT) as client:
        client.return_value.async_run = AsyncMock(side_effect=AlexapyLoginError("nope"))
        result = await http2_connect(ctx)
    assert result is None
    fired = [call.args[0] for call in hass.bus.async_fire.call_args_list]
    assert "alexa_media_relogin_required" in fired


async def test_http2_connect_swallows_generic_error():
    ctx, hass, login = _push_ctx({})
    login.session.closed = False
    with patch(_CLIENT) as client:
        client.return_value.async_run = AsyncMock(side_effect=RuntimeError("boom"))
        result = await http2_connect(ctx)
    assert result is None
    fired = [call.args[0] for call in hass.bus.async_fire.call_args_list]
    assert "alexa_media_relogin_required" not in fired


# --- http2_handler: media / now-playing dispatch ----------------------------


async def test_handler_media_change_dispatches_player_state():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    msg = _msg("PUSH_MEDIA_CHANGE", {"dopplerId": {"deviceSerialNumber": "SER1"}})
    with _handler_patches(existing=["SER1"]) as dispatch:
        await http2_handler(ctx, msg)
    assert any("player_state" in p for p in _payloads(dispatch))


async def test_handler_now_playing_dispatched_when_serial_unknown():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "NotifyNowPlayingUpdated", {"dopplerId": {"deviceSerialNumber": "GHOST"}}
    )
    with _handler_patches(existing=[]) as dispatch:
        await http2_handler(ctx, msg)
    # No matching media_player -> falls through to the NowPlaying signal.
    assert any("now_playing" in p for p in _payloads(dispatch))


# --- http2_handler: volume change -------------------------------------------


async def test_handler_volume_change_simulates_and_dispatches():
    now_ms = int(time.time() * 1000)
    account = _handler_account(
        last_equalizer={"SER1": {"updated": now_ms}},
        last_called_probe_trigger=MagicMock(),
    )
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "PUSH_VOLUME_CHANGE",
        {
            "dopplerId": {"deviceSerialNumber": "SER1"},
            "volumeSetting": 30,
            "isMuted": False,
        },
    )
    with _handler_patches(existing=["SER1"]) as dispatch, patch(_QUEUE) as queue:
        await http2_handler(ctx, msg)
    queue.assert_called_once()
    assert account["last_called_probe_trigger_serial"] == "SER1"
    account["last_called_probe_trigger"].assert_called_once()
    assert "SER1" in account["last_volumes"]
    assert any("player_state" in p for p in _payloads(dispatch))


async def test_handler_volume_change_no_simulate_without_recent_eq():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "PUSH_VOLUME_CHANGE",
        {
            "dopplerId": {"deviceSerialNumber": "SER1"},
            "volumeSetting": 10,
            "isMuted": True,
        },
    )
    with _handler_patches(existing=["SER1"]), patch(_QUEUE) as queue:
        await http2_handler(ctx, msg)
    queue.assert_not_called()
    assert account["last_volumes"]["SER1"]["isMuted"] is True


async def test_handler_volume_change_handles_bad_timestamp():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "PUSH_VOLUME_CHANGE",
        {"dopplerId": {"deviceSerialNumber": "SER1"}, "volumeSetting": 5},
        timestamp="not-a-number",
    )
    with _handler_patches(existing=["SER1"]) as dispatch, patch(_QUEUE) as queue:
        await http2_handler(ctx, msg)
    # int("not-a-number") raises -> activity skipped, dispatch still happens.
    queue.assert_not_called()
    assert any("player_state" in p for p in _payloads(dispatch))


# --- http2_handler: equalizer change ----------------------------------------


async def test_handler_equalizer_change_simulates_on_match():
    account = _handler_account(
        last_equalizer={"SER1": {"bass": 1, "treble": 2, "midrange": 3, "updated": 0}}
    )
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "PUSH_EQUALIZER_STATE_CHANGE",
        {
            "dopplerId": {"deviceSerialNumber": "SER1"},
            "bass": 1,
            "treble": 2,
            "midrange": 3,
        },
    )
    with _handler_patches(existing=["SER1"]) as dispatch, patch(_QUEUE) as queue:
        await http2_handler(ctx, msg)
    queue.assert_called_once()
    assert any("player_state" in p for p in _payloads(dispatch))


async def test_handler_equalizer_change_no_simulate():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "PUSH_EQUALIZER_STATE_CHANGE",
        {
            "dopplerId": {"deviceSerialNumber": "SER1"},
            "bass": 9,
            "treble": 8,
            "midrange": 7,
        },
    )
    with _handler_patches(existing=["SER1"]), patch(_QUEUE) as queue:
        await http2_handler(ctx, msg)
    queue.assert_not_called()
    assert account["last_equalizer"]["SER1"]["bass"] == 9


async def test_handler_equalizer_change_handles_bad_timestamp():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "PUSH_EQUALIZER_STATE_CHANGE",
        {"dopplerId": {"deviceSerialNumber": "SER1"}, "bass": 1},
        timestamp="bad",
    )
    with _handler_patches(existing=["SER1"]) as dispatch, patch(_QUEUE) as queue:
        await http2_handler(ctx, msg)
    # int("bad") raises -> activity skipped, dispatch still happens.
    queue.assert_not_called()
    assert any("player_state" in p for p in _payloads(dispatch))


# --- http2_handler: doppler / queue -----------------------------------------


async def test_handler_doppler_connection_change_dispatches():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "PUSH_DOPPLER_CONNECTION_CHANGE",
        {"dopplerId": {"deviceSerialNumber": "SER1"}},
    )
    with _handler_patches(existing=["SER1"]) as dispatch:
        await http2_handler(ctx, msg)
    assert any("player_state" in p for p in _payloads(dispatch))


async def test_handler_media_queue_change_dispatches():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    msg = _msg("PUSH_MEDIA_QUEUE_CHANGE", {"dopplerId": {"deviceSerialNumber": "SER1"}})
    with _handler_patches(existing=["SER1"]) as dispatch:
        await http2_handler(ctx, msg)
    assert any("queue_state" in p for p in _payloads(dispatch))


# --- http2_handler: bluetooth -----------------------------------------------


async def test_handler_bluetooth_state_change_dispatches():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "PUSH_BLUETOOTH_STATE_CHANGE",
        {
            "dopplerId": {"deviceSerialNumber": "SER1"},
            "bluetoothEvent": "DEVICE_CONNECTED",
            "bluetoothEventSuccess": True,
        },
    )
    with (
        _handler_patches(existing=["SER1"]) as dispatch,
        patch(_BLUETOOTH, new=AsyncMock(return_value={"on": True})) as bt,
    ):
        await http2_handler(ctx, msg)
    bt.assert_awaited_once()
    assert any("bluetooth_change" in p for p in _payloads(dispatch))


async def test_handler_bluetooth_state_change_ignores_unknown_event():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "PUSH_BLUETOOTH_STATE_CHANGE",
        {
            "dopplerId": {"deviceSerialNumber": "SER1"},
            "bluetoothEvent": "SOMETHING_ELSE",
            "bluetoothEventSuccess": True,
        },
    )
    with (
        _handler_patches(existing=["SER1"]) as dispatch,
        patch(_BLUETOOTH, new=AsyncMock()) as bt,
    ):
        await http2_handler(ctx, msg)
    bt.assert_not_awaited()
    assert dispatch.call_count == 0


# --- http2_handler: notification change (key/entryId serial) ----------------


async def test_handler_notification_change_uses_key_serial_and_schedules():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    msg = _msg("PUSH_NOTIFICATION_CHANGE", {"key": {"entryId": "a#b#SERIALN"}})
    with (
        _handler_patches(existing=["SERIALN"]) as dispatch,
        patch(_SCHEDULE_NOTIF) as schedule,
    ):
        await http2_handler(ctx, msg)
    schedule.assert_called_once()
    assert schedule.call_args.kwargs["device_serial"] == "SERIALN"
    assert any("notification_update" in p for p in _payloads(dispatch))


# --- http2_handler: ignored / unknown commands ------------------------------


async def test_handler_ignores_known_unsupported_commands():
    commands = [
        "PUSH_DELETE_DOPPLER_ACTIVITIES",
        "PUSH_TODO_CHANGE",
        "PUSH_LIST_CHANGE",
        "PUSH_LIST_ITEM_CHANGE",
        "PUSH_CONTENT_FOCUS_CHANGE",
        "PUSH_DEVICE_SETUP_STATE_CHANGE",
        "PUSH_MEDIA_PREFERENCE_CHANGE",
        "MATTER_SETUP_NOTIFICATION",
    ]
    for command in commands:
        account = _handler_account()
        ctx, _, _ = _push_ctx(account)
        msg = _msg(command, {"foo": "bar"})  # no serial
        with _handler_patches(existing=[]) as dispatch:
            await http2_handler(ctx, msg)
        assert dispatch.call_count == 0, command


async def test_handler_unknown_command_is_ignored():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    msg = _msg("SOME_BRAND_NEW_COMMAND", {"foo": "bar"})
    with _handler_patches(existing=[]) as dispatch:
        await http2_handler(ctx, msg)
    assert dispatch.call_count == 0


# --- http2_handler: malformed input -----------------------------------------


async def test_handler_skips_malformed_payload():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    resource = json.dumps({"command": "PUSH_MEDIA_CHANGE", "payload": "{not-json"})
    with _handler_patches(existing=[]) as dispatch:
        await http2_handler(ctx, _envelope(resource))
    assert dispatch.call_count == 0


async def test_handler_skips_invalid_resource_metadata():
    account = _handler_account()
    ctx, _, _ = _push_ctx(account)
    with _handler_patches(existing=[]) as dispatch:
        await http2_handler(ctx, _envelope("this is not json"))
    assert dispatch.call_count == 0


# --- http2_handler: activity tracking / discovery ---------------------------


async def test_handler_triggers_dnd_update_on_burst():
    now = time.time()
    history = [["PUSH_VOLUME_CHANGE", now] for _ in range(4)]
    account = _handler_account(http2_activity={"serials": {"SER1": history}})
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "PUSH_VOLUME_CHANGE",
        {"dopplerId": {"deviceSerialNumber": "SER1"}, "volumeSetting": 1},
    )
    with (
        _handler_patches(existing=["SER1"]),
        patch(_QUEUE),
        patch(_DND, new=AsyncMock()) as dnd,
    ):
        await http2_handler(ctx, msg)
    dnd.assert_awaited_once()


async def test_handler_audio_player_state_resets_activity_events():
    now = time.time()
    history = [["PUSH_AUDIO_PLAYER_STATE", now]]
    account = _handler_account(http2_activity={"serials": {"SER1": history}})
    ctx, _, _ = _push_ctx(account)
    msg = _msg("PUSH_AUDIO_PLAYER_STATE", {"dopplerId": {"deviceSerialNumber": "SER1"}})
    with (
        _handler_patches(existing=["SER1"]) as dispatch,
        patch(_DND, new=AsyncMock()) as dnd,
    ):
        await http2_handler(ctx, msg)
    # PUSH_AUDIO_PLAYER_STATE in the window clears the burst -> no DND probe.
    dnd.assert_not_awaited()
    assert any("player_state" in p for p in _payloads(dispatch))


async def test_handler_discovers_new_device_and_refreshes():
    coord = _coordinator()
    account = _handler_account(coordinator=coord)
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "PUSH_AUDIO_PLAYER_STATE", {"dopplerId": {"deviceSerialNumber": "NEWSER"}}
    )
    with _handler_patches(existing=[]) as dispatch:
        await http2_handler(ctx, msg)
    assert account["new_devices"] is True
    coord.async_request_refresh.assert_awaited_once()
    assert dispatch.call_count == 0


async def test_handler_excluded_device_not_rediscovered():
    coord = _coordinator()
    account = _handler_account(coordinator=coord, excluded={"EXSER": {}})
    ctx, _, _ = _push_ctx(account)
    msg = _msg(
        "PUSH_AUDIO_PLAYER_STATE", {"dopplerId": {"deviceSerialNumber": "EXSER"}}
    )
    with _handler_patches(existing=[]):
        await http2_handler(ctx, msg)
    assert account["new_devices"] is False
    coord.async_request_refresh.assert_not_awaited()


# --- http2_close_handler: reconnect path ------------------------------------


async def test_http2_close_handler_skips_reconnect_within_backoff():
    account = {"http2": "client", "http2error": 0, "http2_lastattempt": time.time()}
    ctx, _, _ = _push_ctx(account)
    with patch(_CONNECT) as connect:
        await http2_close_handler(ctx)
    assert account["http2"] is None
    connect.assert_not_called()


async def test_http2_close_handler_reconnects_successfully():
    coord = _coordinator()
    account = {
        "http2": "old",
        "http2error": 0,
        "http2_lastattempt": 0.0,
        "coordinator": coord,
    }
    ctx, _, _ = _push_ctx(account)
    with patch(_CONNECT, new=AsyncMock(return_value="newclient")) as connect:
        await http2_close_handler(ctx)
    connect.assert_awaited_once()
    assert account["http2"] == "newclient"
    # http2 alive -> slow polling (scan_interval * 10) and a refresh.
    assert coord.update_interval == timedelta(seconds=60.0 * 10)
    coord.async_request_refresh.assert_awaited_once()


async def test_http2_close_handler_gives_up_after_retries():
    coord = _coordinator()
    account = {
        "http2": "old",
        "http2error": 0,
        "http2_lastattempt": 0.0,
        "coordinator": coord,
    }
    ctx, _, _ = _push_ctx(account)
    with (
        patch(_CONNECT, new=AsyncMock(return_value=None)) as connect,
        patch(f"{_PUSH}.asyncio.sleep", new=AsyncMock()) as sleep,
    ):
        await http2_close_handler(ctx)
    assert connect.await_count == 5
    assert account["http2error"] == 5
    assert account["http2"] is None
    # reconnect failed -> normal polling cadence and a refresh.
    assert coord.update_interval == timedelta(seconds=60.0)
    coord.async_request_refresh.assert_awaited_once()
    assert sleep.await_count == 5
