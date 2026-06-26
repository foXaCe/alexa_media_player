"""HTTP/2 push connection and message handlers for Alexa Media Player.

Extracted from ``setup_alexa``. ``http2_connect`` opens the push connection and
binds the message/open/close/error handlers to a :class:`SetupContext` via
``functools.partial``; the handlers translate Alexa push events into coordinator
refreshes, last_called updates, bluetooth/DND syncs and notification refreshes.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
import functools
from json import JSONDecodeError, loads
import logging
import time
from typing import TYPE_CHECKING

from alexapy import AlexapyLoginError, HTTP2EchoClient, hide_serial
from homeassistant.core import callback
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ..const import (
    DATA_ALEXAMEDIA,
    DOMAIN,
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
            "alexa_media_relogin_required",
            event_data={"email": hide_email(email), "url": login_obj.url},
        )
        return
    except BaseException as exception_:  # pylint: disable=broad-except
        _LOGGER.debug("%s: HTTP2 creation failed: %s", hide_email(email), exception_)
        return
    _LOGGER.debug("%s: HTTP2 created: %s", hide_email(email), http2)
    return http2


@callback
async def http2_handler(ctx: SetupContext, message_obj):
    # pylint: disable=too-many-branches,too-many-statements
    """Handle http2 push messages.

    This allows push notifications from Alexa to update last_called and media state.
    """
    hass = ctx.hass
    email = ctx.email
    login_obj = ctx.login_obj

    coordinator = hass.data[DATA_ALEXAMEDIA]["accounts"][email].get("coordinator")
    account = hass.data[DATA_ALEXAMEDIA]["accounts"][email]

    def _now_ms() -> int:
        return int(time.time() * 1000)

    def simulate_activity(
        device_serial: str,
        customer_id: str | None,
        trigger_command: str,
        trigger_ts_ms: int | None,
    ) -> None:
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
            simulate_activity(
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
            simulate_activity(
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

    # ---------------------------------------------------------------------
    # Main http2push parsing / dispatch
    # ---------------------------------------------------------------------
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
        seen_commands = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2_commands"]

        if command and json_payload:
            _LOGGER.debug(
                "%s: Received http2push command: %s : %s",
                hide_email(email),
                command,
                hide_serial(json_payload),
            )

            account["last_push_activity"] = time.time()
            serial = None
            command_time = time.time()
            if command not in seen_commands:
                _LOGGER.debug("Adding %s to seen_commands: %s", command, seen_commands)
            seen_commands[command] = command_time

            if (
                "dopplerId" in json_payload
                and "deviceSerialNumber" in json_payload["dopplerId"]
            ):
                serial = json_payload["dopplerId"]["deviceSerialNumber"]
            elif (
                "key" in json_payload
                and "entryId" in json_payload["key"]
                and json_payload["key"]["entryId"].find("#") != -1
            ):
                serial = (json_payload["key"]["entryId"]).split("#")[2]
                json_payload["key"]["serialNumber"] = serial
            else:
                serial = None

            if command in (
                "PUSH_AUDIO_PLAYER_STATE",
                "PUSH_MEDIA_CHANGE",
                "PUSH_MEDIA_PROGRESS_CHANGE",
                "NotifyMediaSessionsUpdated",
                "NotifyNowPlayingUpdated",
            ):
                if serial and serial in existing_serials:
                    _LOGGER.debug(
                        "Updating media_player: %s", hide_serial(json_payload)
                    )
                    async_dispatcher_send(
                        hass,
                        f"{DOMAIN}_{hide_email(email)}"[0:32],
                        {"player_state": json_payload},
                    )
                elif command == "NotifyNowPlayingUpdated":
                    _LOGGER.debug("Send NowPlaying: %s", hide_serial(json_payload))
                    async_dispatcher_send(
                        hass,
                        f"{DOMAIN}_{hide_email(email)}"[0:32],
                        {"now_playing": json_payload},
                    )

            elif command == "PUSH_VOLUME_CHANGE":
                try:
                    ts = resource.get("timeStamp")
                    ts_ms = int(ts) if ts else None

                    if serial and isinstance(json_payload, dict):
                        _handle_volume_change_activity(serial, json_payload, ts_ms)

                except Exception:
                    _LOGGER.exception(
                        "%s: http2_handler failed processing %s",
                        hide_email(email),
                        command,
                    )

                if serial and serial in existing_serials:
                    _LOGGER.debug(
                        "Updating media_player volume: %s",
                        hide_serial(json_payload),
                    )
                    async_dispatcher_send(
                        hass,
                        f"{DOMAIN}_{hide_email(email)}"[0:32],
                        {"player_state": json_payload},
                    )

            elif command == "PUSH_DOPPLER_CONNECTION_CHANGE":
                # Player availability update
                if serial and serial in existing_serials:
                    _LOGGER.debug(
                        "Updating media_player availability %s",
                        hide_serial(json_payload),
                    )
                    async_dispatcher_send(
                        hass,
                        f"{DOMAIN}_{hide_email(email)}"[0:32],
                        {"player_state": json_payload},
                    )

            elif command == "PUSH_EQUALIZER_STATE_CHANGE":
                # Player equalizer update
                try:
                    ts = resource.get("timeStamp")
                    ts_ms = int(ts) if ts else None

                    if serial and isinstance(json_payload, dict):
                        _handle_equalizer_change_activity(serial, json_payload, ts_ms)

                except Exception:
                    _LOGGER.exception(
                        "%s: http2_handler failed processing %s",
                        hide_email(email),
                        command,
                    )

                if serial and serial in existing_serials:
                    _LOGGER.debug(
                        "Updating media_player equalizer state %s",
                        hide_serial(json_payload),
                    )
                    async_dispatcher_send(
                        hass,
                        f"{DOMAIN}_{hide_email(email)}"[0:32],
                        {"player_state": json_payload},
                    )

            elif command == "PUSH_BLUETOOTH_STATE_CHANGE":
                # Player bluetooth update
                bt_event = (
                    json_payload.get("bluetoothEvent")
                    if isinstance(json_payload, dict)
                    else None
                )
                _LOGGER.debug("bt_event: %s", bt_event)
                bt_success = (
                    json_payload.get("bluetoothEventSuccess")
                    if isinstance(json_payload, dict)
                    else None
                )
                _LOGGER.debug("bt_success: %s", bt_success)
                if (
                    serial
                    and serial in existing_serials
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
                        login_obj, ctx, serial
                    )
                    _LOGGER.debug("bluetooth_state %s", hide_serial(bluetooth_state))
                    if bluetooth_state:
                        async_dispatcher_send(
                            hass,
                            f"{DOMAIN}_{hide_email(email)}"[0:32],
                            {"bluetooth_change": bluetooth_state},
                        )

            elif command == "PUSH_MEDIA_QUEUE_CHANGE":
                # Player availability update
                if serial and serial in existing_serials:
                    _LOGGER.debug(
                        "Updating media_player queue %s",
                        hide_serial(json_payload),
                    )
                    async_dispatcher_send(
                        hass,
                        f"{DOMAIN}_{hide_email(email)}"[0:32],
                        {"queue_state": json_payload},
                    )

            elif command == "PUSH_NOTIFICATION_CHANGE":
                # Notification/alarm state changed on this device.
                # Queue a refresh with backoff to ride out alexa-side cooldowns.
                setup_notifications.schedule_notifications_refresh(
                    ctx,
                    device_serial=serial,
                    reason="PUSH_NOTIFICATION_CHANGE",
                )

                if serial and serial in existing_serials:
                    _LOGGER.debug(
                        "Updating mediaplayer notifications: %s",
                        hide_serial(json_payload),
                    )
                    async_dispatcher_send(
                        hass,
                        f"{DOMAIN}_{hide_email(email)}"[0:32],
                        {"notification_update": json_payload},
                    )

            elif command in [
                "PUSH_DELETE_DOPPLER_ACTIVITIES",  # Delete Alexa history,
            ]:
                pass

            elif (
                command
                in [
                    "PUSH_TODO_CHANGE",  # Update To-Do List
                    "PUSH_LIST_CHANGE",  # Clear a shopping list https://github.com/alandtse/alexa_media_player/issues/1190
                    "PUSH_LIST_ITEM_CHANGE",  # Update shopping list
                ]
            ):
                # To-do
                _LOGGER.debug("%s currently not supported", command)
                pass

            elif command in [
                "PUSH_CONTENT_FOCUS_CHANGE",  # Likely prime related refocus
                "PUSH_DEVICE_SETUP_STATE_CHANGE",  # Likely device changes mid setup
            ]:
                _LOGGER.debug("%s currently not supported", command)
                pass

            elif (
                command
                in [
                    "PUSH_MEDIA_PREFERENCE_CHANGE",  # Disliking or liking songs, https://github.com/alandtse/alexa_media_player/issues/1599
                ]
            ):
                _LOGGER.debug("%s currently not supported", command)
                pass

            elif command in [
                "MATTER_SETUP_NOTIFICATION",  # New command observed 2026-02-20
            ]:
                _LOGGER.debug("%s: New command; currently not supported", command)
                pass

            else:
                _LOGGER.debug(
                    "Unhandled command: %s with data %s. Please report at %s",
                    command,
                    hide_serial(json_payload),
                    ISSUE_URL,
                )

            # Preserve existing http2 activity tracking + new-device discovery
            if serial in existing_serials:
                history = hass.data[DATA_ALEXAMEDIA]["accounts"][email][
                    "http2_activity"
                ]["serials"].get(serial)
                if history is None or (
                    history and command_time - history[len(history) - 1][1] > 2
                ):
                    history = [(command, command_time)]
                else:
                    history.append([command, command_time])

                hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2_activity"][
                    "serials"
                ][serial] = history

                events = []
                for old_command, old_command_time in history:
                    if (
                        old_command
                        in {"PUSH_VOLUME_CHANGE", "PUSH_EQUALIZER_STATE_CHANGE"}
                        and command_time - old_command_time < 0.25
                    ):
                        events.append(
                            (old_command, round(command_time - old_command_time, 2))
                        )
                    elif old_command in {"PUSH_AUDIO_PLAYER_STATE"}:
                        events = []

                if len(events) >= 4:
                    _LOGGER.debug(
                        "%s: Detected potential DND http2push change with %s events %s",
                        hide_serial(serial),
                        len(events),
                        events,
                    )
                    await setup_dnd.update_dnd_state(login_obj, ctx)

            if (
                serial
                and serial not in existing_serials
                and serial
                not in hass.data[DATA_ALEXAMEDIA]["accounts"][email]["excluded"].keys()
            ):
                _LOGGER.debug("Discovered new media_player %s", hide_serial(serial))
                hass.data[DATA_ALEXAMEDIA]["accounts"][email]["new_devices"] = True
                if coordinator:
                    await coordinator.async_request_refresh()


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
            "alexa_media_relogin_required",
            event_data={"email": hide_email(email), "url": login_obj.url},
        )
        return
    hass.data[DATA_ALEXAMEDIA]["accounts"][email]["http2error"] = errors + 1
