"""HTTP/2 push connection and message handlers for Alexa Media Player.

Extracted from ``setup_alexa``. ``http2_connect`` opens the push connection and
binds the message/open/close/error handlers to a :class:`SetupContext` via
``functools.partial``; the handlers translate Alexa push events into coordinator
refreshes, last_called updates, bluetooth/DND syncs and notification refreshes.

``http2_handler`` routes each push command through the ``_PUSH_HANDLERS``
dispatch table; per-command logic lives in small ``_handle_*`` coroutines that
all share the :class:`_PushEvent` view of one parsed push message.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import timedelta
import functools
from json import JSONDecodeError, loads
import logging
import time
from typing import TYPE_CHECKING, Any

from alexapy import AlexapyLoginError, HTTP2EchoClient, hide_serial
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ..const import (
    DATA_ALEXAMEDIA,
    DOMAIN,
    EVENT_RELOGIN_REQUIRED,
    ISSUE_URL,
    LAST_CALLED_COALESCE_WINDOW_MS,
)
from ..helpers import _entity_backed_serials, _existing_serials, hide_email
from . import (
    bluetooth as setup_bluetooth,
    dnd as setup_dnd,
    notifications as setup_notifications,
)
from .last_called import _queue_last_called_activity

if TYPE_CHECKING:
    from .context import SetupContext

_LOGGER = logging.getLogger(__name__)

# Commands acknowledged but deliberately dropped.
_IGNORED_COMMANDS = frozenset(
    {
        "PUSH_DELETE_DOPPLER_ACTIVITIES",  # Delete Alexa history
    }
)

# Known commands with no implementation yet.
_UNSUPPORTED_COMMANDS = frozenset(
    {
        "PUSH_TODO_CHANGE",  # Update To-Do List
        "PUSH_LIST_CHANGE",  # Clear a shopping list https://github.com/alandtse/alexa_media_player/issues/1190
        "PUSH_LIST_ITEM_CHANGE",  # Update shopping list
        "PUSH_CONTENT_FOCUS_CHANGE",  # Likely prime related refocus
        "PUSH_DEVICE_SETUP_STATE_CHANGE",  # Likely device changes mid setup
        "PUSH_MEDIA_PREFERENCE_CHANGE",  # Disliking or liking songs, https://github.com/alandtse/alexa_media_player/issues/1599
    }
)

# Commands first observed in the wild, kept separate for triage logging.
_NEW_UNSUPPORTED_COMMANDS = frozenset(
    {
        "MATTER_SETUP_NOTIFICATION",  # New command observed 2026-02-20
    }
)


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class _PushEvent:
    """One parsed push message plus the account context handlers need."""

    ctx: SetupContext
    account: dict[str, Any]
    email: str
    serial: str | None
    json_payload: Any
    resource: Any
    existing_serials: set[str]

    @property
    def hass(self):
        return self.ctx.hass

    @property
    def login_obj(self):
        return self.ctx.login_obj

    def dispatcher_send(self, payload: dict[str, Any]) -> None:
        """Send a payload to this account's entity dispatcher channel."""
        async_dispatcher_send(
            self.hass,
            f"{DOMAIN}_{hide_email(self.email)}"[0:32],
            payload,
        )

    def timestamp_ms(self) -> int | None:
        """Return the push message timestamp in ms, if present."""
        ts = self.resource.get("timeStamp")
        return int(ts) if ts else None


def _simulate_activity(
    account: dict[str, Any],
    device_serial: str,
    customer_id: str | None,
    trigger_command: str,
    trigger_ts_ms: int | None,
) -> None:
    """Queue a synthetic last_called activity and kick the probe worker."""
    _queue_last_called_activity(
        account,
        device_serial=device_serial,
        customer_id=customer_id,
        activity_ts=trigger_ts_ms,
        command=trigger_command,
    )
    account["last_called_probe_trigger_serial"] = device_serial
    trigger = account.get("last_called_probe_trigger")
    if callable(trigger):
        trigger(trigger_command, trigger_ts_ms)


def _handle_volume_change_activity(
    account: dict[str, Any],
    serial: str,
    json_payload: dict,
    trigger_ts_ms: int | None,
) -> None:
    """Mirror alexa-remote.ts PUSH_VOLUME_CHANGE -> simulateActivity() conditions."""
    last_volumes: dict = account["last_volumes"]
    last_equalizer: dict = account["last_equalizer"]

    vol = json_payload.get("volumeSetting")
    muted = json_payload.get("isMuted")

    eq_prev = last_equalizer.get(serial)

    should_simulate = (
        eq_prev is not None
        and abs(_now_ms() - int(eq_prev.get("updated", 0)))
        < LAST_CALLED_COALESCE_WINDOW_MS
    )

    if should_simulate:
        _LOGGER.debug(
            "[_handle_volume_change_activity] Simulating activity",
        )
        _simulate_activity(
            account,
            serial,
            json_payload.get("destinationUserId"),
            "PUSH_VOLUME_CHANGE",
            trigger_ts_ms,
        )
    else:
        _LOGGER.debug(
            "[_handle_volume_change_activity] Not simulating activity",
        )

    last_volumes[serial] = {
        "volumeSetting": vol,
        "isMuted": muted,
        "updated": _now_ms(),
    }


def _handle_equalizer_change_activity(
    account: dict[str, Any],
    serial: str,
    json_payload: dict,
    trigger_ts_ms: int | None,
) -> None:
    """Mirror alexa-remote.ts PUSH_EQUALIZER_STATE_CHANGE -> simulateActivity() conditions."""
    last_volumes: dict = account["last_volumes"]
    last_equalizer: dict = account["last_equalizer"]

    bass = json_payload.get("bass")
    treble = json_payload.get("treble")
    midrange = json_payload.get("midrange")

    prev = last_equalizer.get(serial)
    vol_prev = last_volumes.get(serial)

    should_simulate = (
        prev is not None
        and prev.get("bass") == bass
        and prev.get("treble") == treble
        and prev.get("midrange") == midrange
    ) or (
        vol_prev is not None
        and abs(_now_ms() - int(vol_prev.get("updated", 0)))
        < LAST_CALLED_COALESCE_WINDOW_MS
    )

    if should_simulate:
        _simulate_activity(
            account,
            serial,
            json_payload.get("destinationUserId"),
            "PUSH_EQUALIZER_STATE_CHANGE",
            trigger_ts_ms,
        )

    last_equalizer[serial] = {
        "bass": bass,
        "treble": treble,
        "midrange": midrange,
        "updated": _now_ms(),
    }


async def http2_connect(ctx: SetupContext) -> HTTP2EchoClient:
    """Open HTTP2 Push connection.

    This will only attempt one login before failing.
    """
    hass = ctx.hass
    login_obj = ctx.login_obj
    if login_obj is None:
        return None
    http2: HTTP2EchoClient | None = None
    email = login_obj.email
    try:
        if login_obj.session.closed:
            _LOGGER.debug(
                "%s: HTTP2 creation aborted. Session is closed.",
                hide_email(email),
            )
            return
        http2 = HTTP2EchoClient(
            login_obj,
            msg_callback=functools.partial(http2_handler, ctx),
            open_callback=functools.partial(http2_open_handler, ctx),
            close_callback=functools.partial(http2_close_handler, ctx),
            error_callback=functools.partial(http2_error_handler, ctx),
            loop=hass.loop,
        )
        _LOGGER.debug("%s: Starting HTTP2: %s", hide_email(email), http2)
        await http2.async_run()
    except AlexapyLoginError as exception_:
        _LOGGER.debug(
            "%s: Login Error detected from http2: %s",
            hide_email(email),
            exception_,
        )
        hass.bus.async_fire(
            EVENT_RELOGIN_REQUIRED,
            event_data={"email": hide_email(email), "url": login_obj.url},
        )
        return
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # Real cancellation of this task (unload/shutdown) must propagate.
            raise
        # alexapy leaks internal aiohttp CancelledErrors when the push channel
        # dies mid-handshake; treat that as a failed connection (fall back to
        # polling), matching the historical behavior.
        _LOGGER.debug(
            "%s: HTTP2 creation cancelled internally; falling back to polling",
            hide_email(email),
        )
        return None
    except Exception as exception_:  # pylint: disable=broad-except
        _LOGGER.debug("%s: HTTP2 creation failed: %s", hide_email(email), exception_)
        return
    _LOGGER.debug("%s: HTTP2 created: %s", hide_email(email), http2)
    return http2


async def _handle_player_state(event: _PushEvent, command: str) -> None:
    """Forward media/player state pushes to the media_player entities."""
    if event.serial and event.serial in event.existing_serials:
        _LOGGER.debug("Updating media_player: %s", hide_serial(event.json_payload))
        event.dispatcher_send({"player_state": event.json_payload})
    elif command == "NotifyNowPlayingUpdated":
        _LOGGER.debug("Send NowPlaying: %s", hide_serial(event.json_payload))
        event.dispatcher_send({"now_playing": event.json_payload})


async def _handle_volume_change(event: _PushEvent, command: str) -> None:
    """Track volume pushes for last_called correlation and forward state."""
    try:
        ts_ms = event.timestamp_ms()
        if event.serial and isinstance(event.json_payload, dict):
            _handle_volume_change_activity(
                event.account, event.serial, event.json_payload, ts_ms
            )
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception(
            "%s: http2_handler failed processing %s",
            hide_email(event.email),
            command,
        )

    if event.serial and event.serial in event.existing_serials:
        _LOGGER.debug(
            "Updating media_player volume: %s",
            hide_serial(event.json_payload),
        )
        event.dispatcher_send({"player_state": event.json_payload})


async def _handle_doppler_connection_change(event: _PushEvent, command: str) -> None:
    """Forward player availability pushes."""
    if event.serial and event.serial in event.existing_serials:
        _LOGGER.debug(
            "Updating media_player availability %s",
            hide_serial(event.json_payload),
        )
        event.dispatcher_send({"player_state": event.json_payload})


async def _handle_equalizer_change(event: _PushEvent, command: str) -> None:
    """Track equalizer pushes for last_called correlation and forward state."""
    try:
        ts_ms = event.timestamp_ms()
        if event.serial and isinstance(event.json_payload, dict):
            _handle_equalizer_change_activity(
                event.account, event.serial, event.json_payload, ts_ms
            )
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception(
            "%s: http2_handler failed processing %s",
            hide_email(event.email),
            command,
        )

    if event.serial and event.serial in event.existing_serials:
        _LOGGER.debug(
            "Updating media_player equalizer state %s",
            hide_serial(event.json_payload),
        )
        event.dispatcher_send({"player_state": event.json_payload})


async def _handle_bluetooth_state_change(event: _PushEvent, command: str) -> None:
    """Refresh and forward bluetooth state on relevant bluetooth events."""
    json_payload = event.json_payload
    bt_event = (
        json_payload.get("bluetoothEvent") if isinstance(json_payload, dict) else None
    )
    _LOGGER.debug("bt_event: %s", bt_event)
    bt_success = (
        json_payload.get("bluetoothEventSuccess")
        if isinstance(json_payload, dict)
        else None
    )
    _LOGGER.debug("bt_success: %s", bt_success)
    if (
        event.serial
        and event.serial in event.existing_serials
        and bt_success
        and bt_event
        and bt_event
        in {
            "DEVICE_CONNECTED",
            "DEVICE_DISCONNECTED",
            "STREAMING_STATE_CHANGED",
        }
    ):
        _LOGGER.debug(
            "Updating media_player bluetooth %s",
            hide_serial(json_payload),
        )
        bluetooth_state = await setup_bluetooth.update_bluetooth_state(
            event.login_obj, event.ctx, event.serial
        )
        _LOGGER.debug("bluetooth_state %s", hide_serial(bluetooth_state))
        if bluetooth_state:
            event.dispatcher_send({"bluetooth_change": bluetooth_state})


async def _handle_media_queue_change(event: _PushEvent, command: str) -> None:
    """Forward queue state pushes."""
    if event.serial and event.serial in event.existing_serials:
        _LOGGER.debug(
            "Updating media_player queue %s",
            hide_serial(event.json_payload),
        )
        event.dispatcher_send({"queue_state": event.json_payload})


async def _handle_notification_change(event: _PushEvent, command: str) -> None:
    """Schedule a notifications refresh and forward the push."""
    # Notification/alarm state changed on this device.
    # Queue a refresh with backoff to ride out alexa-side cooldowns.
    setup_notifications.schedule_notifications_refresh(
        event.ctx,
        device_serial=event.serial,
        reason="PUSH_NOTIFICATION_CHANGE",
    )

    if event.serial and event.serial in event.existing_serials:
        _LOGGER.debug(
            "Updating mediaplayer notifications: %s",
            hide_serial(event.json_payload),
        )
        event.dispatcher_send({"notification_update": event.json_payload})


_PUSH_HANDLERS: dict[str, Callable[[_PushEvent, str], Awaitable[None]]] = {
    "PUSH_AUDIO_PLAYER_STATE": _handle_player_state,
    "PUSH_MEDIA_CHANGE": _handle_player_state,
    "PUSH_MEDIA_PROGRESS_CHANGE": _handle_player_state,
    "NotifyMediaSessionsUpdated": _handle_player_state,
    "NotifyNowPlayingUpdated": _handle_player_state,
    "PUSH_VOLUME_CHANGE": _handle_volume_change,
    "PUSH_DOPPLER_CONNECTION_CHANGE": _handle_doppler_connection_change,
    "PUSH_EQUALIZER_STATE_CHANGE": _handle_equalizer_change,
    "PUSH_BLUETOOTH_STATE_CHANGE": _handle_bluetooth_state_change,
    "PUSH_MEDIA_QUEUE_CHANGE": _handle_media_queue_change,
    "PUSH_NOTIFICATION_CHANGE": _handle_notification_change,
}


def _extract_serial(json_payload: dict) -> str | None:
    """Pull the device serial out of a push payload, if present."""
    if (
        "dopplerId" in json_payload
        and "deviceSerialNumber" in json_payload["dopplerId"]
    ):
        return json_payload["dopplerId"]["deviceSerialNumber"]
    if (
        "key" in json_payload
        and "entryId" in json_payload["key"]
        and json_payload["key"]["entryId"].find("#") != -1
    ):
        serial = (json_payload["key"]["entryId"]).split("#")[2]
        json_payload["key"]["serialNumber"] = serial
        return serial
    return None


async def _track_http2_activity(
    event: _PushEvent, command: str, command_time: float
) -> None:
    """Record per-serial push history and detect DND change bursts."""
    account = event.account
    history = account["http2_activity"]["serials"].get(event.serial)
    if history is None or (history and command_time - history[len(history) - 1][1] > 2):
        history = [(command, command_time)]
    else:
        history.append([command, command_time])

    account["http2_activity"]["serials"][event.serial] = history

    events = []
    for old_command, old_command_time in history:
        if (
            old_command in {"PUSH_VOLUME_CHANGE", "PUSH_EQUALIZER_STATE_CHANGE"}
            and command_time - old_command_time < 0.25
        ):
            events.append((old_command, round(command_time - old_command_time, 2)))
        elif old_command in {"PUSH_AUDIO_PLAYER_STATE"}:
            events = []

    if len(events) >= 4:
        _LOGGER.debug(
            "%s: Detected potential DND http2push change with %s events %s",
            hide_serial(event.serial),
            len(events),
            events,
        )
        await setup_dnd.update_dnd_state(event.login_obj, event.ctx)


async def _maybe_discover_new_device(event: _PushEvent) -> None:
    """Trigger a coordinator refresh when an unknown serial pushes."""
    account = event.account
    if (
        event.serial
        and event.serial not in event.existing_serials
        and event.serial not in account["excluded"].keys()
    ):
        _LOGGER.debug("Discovered new media_player %s", hide_serial(event.serial))
        account["new_devices"] = True
        coordinator = account.get("coordinator")
        if coordinator:
            await coordinator.async_request_refresh()


@callback
async def http2_handler(ctx: SetupContext, message_obj):
    """Handle http2 push messages.

    This allows push notifications from Alexa to update last_called and media state.
    """
    hass = ctx.hass
    email = ctx.email
    login_obj = ctx.login_obj

    account = hass.data[DATA_ALEXAMEDIA]["accounts"][email]

    updates = (
        message_obj.get("directive", {}).get("payload", {}).get("renderingUpdates", [])
    )
    existing_serials = set(_existing_serials(hass, login_obj))
    existing_serials |= _entity_backed_serials(account)
    for item in updates:
        try:
            resource = loads(item.get("resourceMetadata", ""))
        except JSONDecodeError:
            continue

        command = (
            resource["command"]
            if isinstance(resource, dict) and "command" in resource
            else None
        )
        try:
            json_payload = (
                loads(resource["payload"])
                if isinstance(resource, dict) and "payload" in resource
                else None
            )
        except (JSONDecodeError, TypeError):
            _LOGGER.debug(
                "%s: Skipping malformed push payload for command %s",
                hide_email(email),
                command,
            )
            continue
        seen_commands = account["http2_commands"]

        if not (command and json_payload):
            continue

        _LOGGER.debug(
            "%s: Received http2push command: %s : %s",
            hide_email(email),
            command,
            hide_serial(json_payload),
        )

        account["last_push_activity"] = time.time()
        command_time = time.time()
        if command not in seen_commands:
            _LOGGER.debug("Adding %s to seen_commands: %s", command, seen_commands)
        seen_commands[command] = command_time

        serial = _extract_serial(json_payload)
        event = _PushEvent(
            ctx=ctx,
            account=account,
            email=email,
            serial=serial,
            json_payload=json_payload,
            resource=resource,
            existing_serials=existing_serials,
        )

        handler = _PUSH_HANDLERS.get(command)
        if handler is not None:
            await handler(event, command)
        elif command in _IGNORED_COMMANDS:
            pass
        elif command in _UNSUPPORTED_COMMANDS:
            _LOGGER.debug("%s currently not supported", command)
        elif command in _NEW_UNSUPPORTED_COMMANDS:
            _LOGGER.debug("%s: New command; currently not supported", command)
        else:
            _LOGGER.debug(
                "Unhandled command: %s with data %s. Please report at %s",
                command,
                hide_serial(json_payload),
                ISSUE_URL,
            )

        # Preserve existing http2 activity tracking + new-device discovery
        if serial in existing_serials:
            await _track_http2_activity(event, command, command_time)
        await _maybe_discover_new_device(event)


@callback
async def http2_open_handler(ctx: SetupContext):
    """Handle http2 open."""
    hass = ctx.hass
    login_obj = ctx.login_obj

    email: str = login_obj.email
    _LOGGER.debug("%s: HTTP2push successfully connected", hide_email(email))
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"] = 0  # set errors to 0
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2_lastattempt"] = time.time()


@callback
async def http2_close_handler(ctx: SetupContext):
    """Handle http2 close.

    This should attempt to reconnect up to 5 times
    """
    hass = ctx.hass
    login_obj = ctx.login_obj
    scan_interval = ctx.scan_interval
    email: str = login_obj.email
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2"] = None
    if login_obj.close_requested:
        _LOGGER.debug(
            "%s: Close requested; will not reconnect http2", hide_email(email)
        )
        return
    if not login_obj.status.get("login_successful"):
        _LOGGER.debug("%s: Login error; will not reconnect http2", hide_email(email))
        return
    errors: int = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"]
    delay: int = 5 * 2**errors
    last_attempt = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2_lastattempt"]
    now = time.time()
    if (now - last_attempt) < delay:
        return
    http2_client = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2"]
    http2_enabled = bool(http2_client)
    while errors < 5 and not (http2_enabled):
        _LOGGER.debug(
            "%s: HTTP2 push closed; reconnect #%i in %is",
            hide_email(email),
            errors,
            delay,
        )
        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2_lastattempt"] = time.time()
        http2_client = await http2_connect(ctx)
        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2"] = http2_client
        http2_enabled = bool(http2_client)
        if http2_enabled:
            break
        errors = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"] = (
            hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"] + 1
        )
        delay = 5 * 2**errors
        errors = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"]
        await asyncio.sleep(delay)
    if not http2_enabled:
        _LOGGER.debug(
            "%s: HTTP2Push connection closed; retries exceeded; polling",
            hide_email(email),
        )
    coordinator = hass.data[DATA_ALEXAMEDIA]["accounts"][email].get("coordinator")
    if coordinator:
        coordinator.update_interval = timedelta(
            seconds=scan_interval * 10 if http2_enabled else scan_interval
        )
        _LOGGER.debug(
            "HTTP2push: %s, Polling interval: %s",
            http2_enabled,
            coordinator.update_interval,
        )
        await coordinator.async_request_refresh()


@callback
async def http2_error_handler(ctx: SetupContext, message):
    """Handle http2push error.

    This currently logs the error.  In the future, this should invalidate
    the http2push and determine if a reconnect should be done.
    """
    hass = ctx.hass
    login_obj = ctx.login_obj
    email: str = login_obj.email
    errors = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"]
    _LOGGER.debug(
        "%s: Received http2push error #%i %s: type %s",
        hide_email(email),
        errors,
        message,
        type(message),
    )
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2"] = None
    if not login_obj.close_requested and (
        login_obj.session.closed or isinstance(message, AlexapyLoginError)
    ):
        hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"] = 5
        _LOGGER.debug("%s: Login error detected.", hide_email(email))
        hass.bus.async_fire(
            EVENT_RELOGIN_REQUIRED,
            event_data={"email": hide_email(email), "url": login_obj.url},
        )
        return
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"] = errors + 1
