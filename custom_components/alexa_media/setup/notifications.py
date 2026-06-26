"""Notification snapshot processing and debounced refresh for Alexa Media Player.

Extracted from the monolithic ``setup_alexa`` function. ``process_notifications``
keeps ``login_obj`` as its first positional argument because
``@_catch_login_errors`` inspects ``args[0]``; the scheduler/worker pair takes
the shared :class:`SetupContext` first. Behaviour is unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING

from alexapy import AlexaAPI
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.util import dt

from ..const import (
    DATA_ALEXAMEDIA,
    DOMAIN,
    NOTIFICATION_COOLDOWN,
    NOTIFY_REFRESH_BACKOFF,
    NOTIFY_REFRESH_MAX_RETRIES,
)
from ..helpers import _catch_login_errors, alarm_just_dismissed, hide_email, safe_get

if TYPE_CHECKING:
    from alexapy import AlexaLogin

    from .context import SetupContext

_LOGGER = logging.getLogger(__name__)


@_catch_login_errors
async def process_notifications(
    login_obj: AlexaLogin, ctx: SetupContext, raw_notifications=None
) -> bool:
    """Process raw notifications json.

    Returns True if notifications were updated, False if we skipped
    (e.g. due to cooldown or alexapy returned None).
    """
    hass = ctx.hass
    email: str = login_obj.email
    account_dict = hass.data[DATA_ALEXAMEDIA]["accounts"][email]

    if raw_notifications is None:
        now = time.time()
        last = account_dict.get("last_notif_poll", 0.0)
        delta = now - last

        if delta < NOTIFICATION_COOLDOWN:
            _LOGGER.debug(
                "%s: Skipping get_notifications; last poll %.1fs ago (cooldown %ss).",
                hide_email(email),
                delta,
                NOTIFICATION_COOLDOWN,
            )
            return False

        account_dict["last_notif_poll"] = now

        # Small delay to let Alexa settle if we're polling explicitly
        await asyncio.sleep(4)
        raw_notifications = await AlexaAPI.get_notifications(login_obj)

    previous = account_dict.get("notifications", {})
    notifications = {"process_timestamp": dt.utcnow()}

    if raw_notifications is not None:
        for notification in raw_notifications:
            n_dev_id = notification.get("deviceSerialNumber")
            if n_dev_id is None:
                # skip notifications untied to a device for now
                # https://github.com/alandtse/alexa_media_player/issues/633#issuecomment-610705651
                continue
            n_type = notification.get("type")
            if n_type is None:
                continue
            if n_type == "MusicAlarm":
                n_type = "Alarm"
            n_id = notification["notificationIndex"]
            if n_type == "Alarm":
                n_date = notification.get("originalDate")
                n_time = notification.get("originalTime")
                notification["date_time"] = (
                    f"{n_date} {n_time}" if n_date and n_time else None
                )
                previous_alarm = safe_get(previous, [n_dev_id, "Alarm", n_id], {})
                if previous_alarm and alarm_just_dismissed(
                    notification,
                    previous_alarm.get("status"),
                    previous_alarm.get("version"),
                ):
                    hass.bus.async_fire(
                        "alexa_media_alarm_dismissal_event",
                        event_data={
                            "device": {"id": n_dev_id},
                            "event": notification,
                        },
                    )

            if n_dev_id not in notifications:
                notifications[n_dev_id] = {}
            if n_type not in notifications[n_dev_id]:
                notifications[n_dev_id][n_type] = {}
            notifications[n_dev_id][n_type][n_id] = notification

    account_dict["notifications"] = notifications
    _LOGGER.debug(
        "%s: Updated %s notifications for %s devices at %s",
        hide_email(email),
        len(raw_notifications) if raw_notifications is not None else 0,
        len(notifications),
        dt.as_local(account_dict["notifications"]["process_timestamp"]),
    )
    # Notify sensors that the notifications snapshot has been refreshed
    async_dispatcher_send(
        hass,
        f"{DOMAIN}_{hide_email(email)}"[0:32],
        {"notifications_refreshed": True},
    )
    return True


def schedule_notifications_refresh(
    ctx: SetupContext,
    device_serial: str | None = None,
    reason: str = "",
) -> None:
    """Mark notifications as needing refresh and ensure worker task is running.

    device_serial is just for debug; we track a set of pending devices but
    we always refresh the full notifications payload once.
    """
    hass = ctx.hass
    email = ctx.email

    account = hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {}).get(email)
    if not account:
        return

    if device_serial:
        account["notifications_pending"].add(device_serial)
    else:
        # Special marker for "global" changes if you want one
        account["notifications_pending"].add("*")

    if reason:
        _LOGGER.debug(
            "%s: Scheduling notifications refresh (reason=%s, pending=%s)",
            hide_email(email),
            reason,
            account["notifications_pending"],
        )

    task = account.get("notifications_refresh_task")
    if task is not None and not task.done():
        # Already have a running worker; it'll see the new pending set
        return

    # Start new worker
    account["notifications_refresh_task"] = hass.async_create_task(
        run_notifications_refresh(ctx)
    )


async def run_notifications_refresh(ctx: SetupContext) -> None:
    """Worker task: refresh notifications for an account if pending.

    - Uses alexapy.AlexaAPI.get_notifications(login)
    - Retries a few times if we only get None (cooldown/throttle)
    - Clears notifications_pending when successful or when we give up
    """
    hass = ctx.hass
    email = ctx.email
    account = hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {}).get(email)
    if not account:
        return
    login = account.get("login_obj")
    if not login:
        return

    try:
        retries = 0
        while (
            account.get("notifications_pending", set())
            and retries <= NOTIFY_REFRESH_MAX_RETRIES
        ):
            try:
                data = await AlexaAPI.get_notifications(login)
            except Exception as ex:
                _LOGGER.warning(
                    "%s: get_notifications raised %s; treating as None. This may indicate an unexpected error.",
                    hide_email(email),
                    ex,
                )
                data = None

            if data is not None:
                # Success: update through the normal processing path
                await process_notifications(login, ctx, raw_notifications=data)
                account["notifications_retry_count"] = 0
                account["notifications_pending"].clear()

                _LOGGER.debug(
                    "%s: Refreshed notifications snapshot (pending cleared)",
                    hide_email(email),
                )
                return

            # If we get here, alexapy side returned None (cooldown / throttle)
            retries += 1
            account["notifications_retry_count"] = retries

            if not account["notifications_pending"]:
                # Nothing to do anymore, bail early
                break

            _LOGGER.debug(
                "%s: Notifications refresh returned None (retry %s/%s); "
                "pending=%s; sleeping %.1fs",
                hide_email(email),
                retries,
                NOTIFY_REFRESH_MAX_RETRIES,
                account["notifications_pending"],
                NOTIFY_REFRESH_BACKOFF,
            )
            await asyncio.sleep(NOTIFY_REFRESH_BACKOFF)

        # If we fall through, give up for now but leave pending set alone
        if account["notifications_pending"]:
            _LOGGER.debug(
                "%s: Giving up notifications refresh after %s attempts; "
                "still pending=%s",
                hide_email(email),
                retries,
                account["notifications_pending"],
            )

    finally:
        # Always clear the task pointer so future pushes can schedule again
        account["notifications_refresh_task"] = None
