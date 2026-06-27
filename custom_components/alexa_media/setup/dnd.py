"""Do-Not-Disturb (DND) state synchronisation for Alexa Media Player.

Extracted from the monolithic ``setup_alexa`` function. The throttling state
(lock, last-run timers, pending flags, scheduled tasks) lives on the shared
:class:`SetupContext`. Behaviour is unchanged; ``update_dnd_state`` keeps
``login_obj`` as its first positional argument because ``@_catch_login_errors``
inspects ``args[0]``.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import logging
from typing import TYPE_CHECKING

from alexapy import AlexaAPI
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ..const import DATA_ALEXAMEDIA, DOMAIN, MIN_TIME_BETWEEN_SCANS
from ..helpers import _catch_login_errors, hide_email

if TYPE_CHECKING:
    from alexapy import AlexaLogin

    from .context import SetupContext

_LOGGER = logging.getLogger(__name__)


async def schedule_update_dnd_state(ctx: SetupContext, email: str) -> None:
    """Run one deferred DND refresh after the cooldown expires."""
    hass = ctx.hass
    try:
        while True:
            async with ctx.dnd_update_lock:
                if not ctx.pending_dnd_updates.get(email, False):
                    ctx.scheduled_dnd_tasks.pop(email, None)
                    return

                last_run = ctx.last_dnd_update_times.get(email)
                now = datetime.now(UTC)

                remaining = 0.0
                if last_run is not None:
                    elapsed = now - last_run
                    if elapsed < MIN_TIME_BETWEEN_SCANS:
                        remaining = (MIN_TIME_BETWEEN_SCANS - elapsed).total_seconds()

            if remaining > 0:
                _LOGGER.debug(
                    "%s: Deferred DND update sleeping %.3fs until cooldown expires",
                    hide_email(email),
                    remaining,
                )
                await asyncio.sleep(remaining)

            async with ctx.dnd_update_lock:
                if not ctx.pending_dnd_updates.get(email, False):
                    ctx.scheduled_dnd_tasks.pop(email, None)
                    return

                last_run = ctx.last_dnd_update_times.get(email)
                now = datetime.now(UTC)
                if last_run is not None and (now - last_run) < MIN_TIME_BETWEEN_SCANS:
                    # Another update snuck in or timing was slightly early; loop and re-evaluate.
                    continue

                ctx.pending_dnd_updates[email] = False
                ctx.scheduled_dnd_tasks.pop(email, None)

            login_obj = (
                hass.data.get(DATA_ALEXAMEDIA, {})
                .get("accounts", {})
                .get(email, {})
                .get("login_obj")
            )
            if not login_obj:
                _LOGGER.debug(
                    "%s: Skipping scheduled forced DND update: login_obj missing",
                    hide_email(email),
                )
                return

            _LOGGER.debug(
                "%s: Executing scheduled forced DND update",
                hide_email(email),
            )
            await update_dnd_state(login_obj, ctx)
            return

    except asyncio.CancelledError:
        _LOGGER.debug("%s: Deferred DND update task cancelled", hide_email(email))
        raise
    finally:
        async with ctx.dnd_update_lock:
            task = ctx.scheduled_dnd_tasks.get(email)
            if task is asyncio.current_task():
                ctx.scheduled_dnd_tasks.pop(email, None)


@_catch_login_errors
async def update_dnd_state(login_obj: AlexaLogin, ctx: SetupContext) -> None:
    """Update the DND state on websocket DND combo event."""
    hass = ctx.hass
    email = login_obj.email
    now = datetime.now(UTC)

    async with ctx.dnd_update_lock:
        last_run = ctx.last_dnd_update_times.get(email)

        if last_run is not None and (now - last_run) < MIN_TIME_BETWEEN_SCANS:
            ctx.pending_dnd_updates[email] = True

            if (
                email not in ctx.scheduled_dnd_tasks
                or ctx.scheduled_dnd_tasks[email].done()
            ):
                _LOGGER.debug(
                    "%s: Throttling active; scheduling deferred DND update.",
                    hide_email(email),
                )
                ctx.scheduled_dnd_tasks[email] = asyncio.create_task(
                    schedule_update_dnd_state(ctx, email)
                )
            else:
                _LOGGER.debug(
                    "%s: Throttling active; deferred DND update already scheduled.",
                    hide_email(email),
                )
            return

        ctx.last_dnd_update_times[email] = now

    _LOGGER.debug("%s: Updating DND state", hide_email(email))
    try:
        dnd = await AlexaAPI.get_dnd_state(login_obj)
    except TimeoutError:
        _LOGGER.error(
            "%s: Timeout occurred while fetching DND state",
            hide_email(email),
        )
        return
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.error(
            "%s: Unexpected error while fetching DND state: %s",
            hide_email(email),
            err,
        )
        return

    if dnd is not None and "doNotDisturbDeviceStatusList" in dnd:
        async_dispatcher_send(
            hass,
            f"{DOMAIN}_{hide_email(email)}"[0:32],
            {"dnd_update": dnd["doNotDisturbDeviceStatusList"]},
        )
        return

    _LOGGER.debug("%s: get_dnd_state failed: dnd:%s", hide_email(email), dnd)
