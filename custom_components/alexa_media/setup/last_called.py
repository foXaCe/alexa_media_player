"""Near-real-time last_called tracking for Alexa Media Player.

Module-level helpers extracted verbatim from ``__init__``. They correlate Alexa
voice activity (customer history records) with HTTP/2 push events to determine
which device was "last called" without aggressive polling. These functions take
explicit parameters (no closures) and are re-exported from ``__init__`` so all
existing call sites keep working.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import TYPE_CHECKING

from alexapy import AlexaAPI, AlexapyConnectionError, AlexapyLoginError, hide_serial
from alexapy.errors import AlexapyTooManyRequestsError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ..const import (
    DATA_ALEXAMEDIA,
    DOMAIN,
    LAST_CALLED_429_BACKOFF_INITIAL_S,
    LAST_CALLED_429_BACKOFF_MAX_S,
    LAST_CALLED_CONN_BACKOFF_S,
    LAST_CALLED_DEBOUNCE_S,
    LAST_CALLED_ITEMS,
    LAST_CALLED_LOGIN_BACKOFF_S,
    LAST_CALLED_LOOKBACK_MS,
    LAST_CALLED_RETRY_DELAY_S,
    LAST_CALLED_RETRY_LIMIT,
    LAST_CALLED_STALE_FUDGE_MS,
    LAST_CALLED_SUCCESS_PACE_S,
)
from ..helpers import (
    _catch_login_errors,
    _entity_backed_serials,
    _existing_serials,
    _network_allowed,
    hide_email,
    report_relogin_required,
)
from ..metrics import get_metrics
from . import dnd as setup_dnd

if TYPE_CHECKING:
    from .context import SetupContext

_LOGGER = logging.getLogger(__name__)


def _valid_voice_summary(summary: object) -> bool:
    """Return True if summary looks like a real spoken utterance."""
    if not isinstance(summary, str):
        return False
    summary = summary.strip()
    return bool(summary) and any(ch.isalnum() for ch in summary)


def _queue_last_called_activity(
    account: dict,
    *,
    device_serial: str,
    customer_id: str | None,
    activity_ts: int | None,
    command: str,
) -> None:
    """Queue or refresh a last_called activity candidate keyed by device/customer."""
    if not device_serial:
        return

    try:
        ts = int(activity_ts) if activity_ts is not None else 0
    except (TypeError, ValueError):
        ts = 0

    queue: list[dict] = account.setdefault("last_called_activity_queue", [])

    for item in queue:
        if (
            item.get("serial") == device_serial
            and item.get("customer_id") == customer_id
        ):
            # Keep earliest timestamp like alexa-remote does for the queued burst.
            prev_ts = int(item.get("activity_ts") or 0)
            if ts and (prev_ts == 0 or ts < prev_ts):
                item["activity_ts"] = ts
            item["command"] = command
            return

    queue.append(
        {
            "serial": device_serial,
            "customer_id": customer_id,
            "activity_ts": ts,
            "command": command,
        }
    )


def _snapshot_last_called_activity_queue(account: dict) -> list[dict]:
    """Return a shallow snapshot of queued activity entries."""
    queue = account.get("last_called_activity_queue") or []
    return [dict(item) for item in queue if isinstance(item, dict)]


def _remove_last_called_activity_queue_entries(
    account: dict, resolved_keys: set[tuple[str, str | None]]
) -> None:
    """Remove resolved queue entries by (serial, customer_id)."""
    queue = account.get("last_called_activity_queue") or []
    account["last_called_activity_queue"] = [
        item
        for item in queue
        if (
            item.get("serial"),
            item.get("customer_id"),
        )
        not in resolved_keys
    ]


def _valid_utterance_type(record: dict) -> bool:
    """Filter utterance types similar to Node-RED/alexa-remote logic."""
    utterance_type = record.get("utteranceType")
    if utterance_type in {
        "DEVICE_ARBITRATION",
        "ASR_TIMEOUT",
        "WAKE_WORD_ONLY",
    }:
        return False
    return True


def _select_last_called_payload_from_records(
    records: list[dict],
    queue_snapshot: list[dict],
    account: dict,
    existing_serials_local: set[str],
) -> tuple[dict | None, set[tuple[str, str | None]]]:
    """Select the best last_called payload from raw customer history records."""
    if not records or not queue_snapshot:
        return None, set()

    watermark = int(account.get("last_called_customer_history_ts") or 0)
    last_pushed_activity = account.get("last_called_last_pushed_activity") or {}

    queue_by_key = {
        (item.get("serial"), item.get("customer_id")): item
        for item in queue_snapshot
        if item.get("serial")
    }

    def _record_ts(record: dict) -> int:
        try:
            return int(record.get("creationTimestamp") or 0)
        except (TypeError, ValueError):
            return 0

    sorted_records = sorted(
        (r for r in records if isinstance(r, dict)),
        key=_record_ts,
        reverse=True,
    )

    for record in sorted_records:
        serial = record.get("deviceSerialNumber")
        if not serial or serial not in existing_serials_local:
            continue

        if not _valid_utterance_type(record):
            continue

        summary = ((record.get("description") or {}).get("summary") or "").strip()
        if not _valid_voice_summary(summary):
            continue

        try:
            ts = int(record.get("creationTimestamp") or 0)
        except (TypeError, ValueError):
            continue

        if ts <= watermark:
            continue

        if ts <= int(last_pushed_activity.get(serial) or 0):
            continue

        for key, queued in queue_by_key.items():
            queued_serial, _queued_customer = key
            queued_ts = int(queued.get("activity_ts") or 0)

            if serial != queued_serial:
                continue

            # Future enhancement: customer/user matching.
            # The push payload includes destinationUserId, but the current
            # get_customer_history_records() helper does not expose a matching
            # user identifier from the raw RVH records, so this check is currently
            # ineffective. Leave in place in case the API helper is extended later.
            #
            # if queued_customer and record.get("customerId") not in (None, queued_customer):
            #    continue

            if queued_ts and ts < (queued_ts - LAST_CALLED_STALE_FUDGE_MS):
                continue

            payload = {
                "serialNumber": serial,
                "timestamp": ts,
                "summary": summary,
                "response": (record.get("alexaResponse") or "").strip() or None,
            }
            return payload, {key}

    return None, set()


def _store_and_dispatch_last_called(
    hass: HomeAssistant,
    email: str,
    last_called: dict,
    force: bool = False,
) -> None:
    """Store last_called data and dispatch change event if needed.

    Shared helper used by both the closure-based update_last_called
    and the module-level _async_update_last_called_global.
    """
    accounts = hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {})
    if email not in accounts:
        _LOGGER.debug("%s: Account removed during update, skipping", hide_email(email))
        return
    stored_data = accounts[email]
    payload = dict(last_called) if isinstance(last_called, dict) else {}

    ts_raw = payload.get("timestamp")
    try:
        ts = int(ts_raw or 0)
    except (TypeError, ValueError):
        ts = 0

    if 0 < ts < 10_000_000_000:
        ts *= 1000
    if ts > 0:
        payload["timestamp"] = ts

    prev = stored_data.get("last_called")
    changed = prev != payload

    if ts > 0:
        stored_data["last_called_customer_history_ts"] = max(
            int(stored_data.get("last_called_customer_history_ts") or 0),
            ts,
        )

    stored_data["last_called"] = payload

    if (
        force
        or (prev is None and last_called is not None)
        or (prev is not None and changed)
    ):
        _LOGGER.debug(
            "%s: last_called changed",
            hide_email(email),
        )
        async_dispatcher_send(
            hass,
            f"{DOMAIN}_{hide_email(email)}"[0:32],
            {"last_called_change": payload},
        )


async def _async_update_last_called_global(
    hass: HomeAssistant,
    login_obj,
    email: str,
    last_called: dict | None = None,
    force: bool = False,
) -> None:
    """Update the last called device globally (standalone function).

    This version is defined at module level for use in background tasks.
    It delegates storage/dispatch to _store_and_dispatch_last_called.
    """
    if not _network_allowed(login_obj):
        return

    accounts = hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {})
    account = accounts.get(email)

    # If we're doing a "refresh" (no payload) and not forcing, prefer the probe worker
    # whenever it's available, regardless of http2 connection.
    if (
        (not isinstance(last_called, dict) or not last_called.get("summary"))
        and not force
        and account
    ):
        trigger = account.get("last_called_probe_trigger")
        if callable(trigger):
            trigger("GLOBAL_REFRESH", None)
            return

    if not isinstance(last_called, dict) or not last_called.get("summary"):
        try:
            # Do not timebox this call; alexapy may back off/sleep on 429.
            # Serialize RVH calls per account to avoid parallel rate-limited requests.
            api_lock = None
            if account:
                api_lock = account.get("last_called_api_lock")
            if api_lock is None:
                last_called = await AlexaAPI.get_last_device_serial(login_obj)
            else:
                async with api_lock:
                    last_called = await AlexaAPI.get_last_device_serial(login_obj)
        except asyncio.CancelledError:
            # Task cancelled during unload/shutdown; propagate cancellation.
            raise
        except AlexapyTooManyRequestsError:
            _LOGGER.debug(
                "%s: Rate limited during last_called update; skipping",
                hide_email(email),
            )
            return
        except AlexapyConnectionError as exc:
            _LOGGER.debug(
                "%s: Connection error during last_called update: %s",
                hide_email(email),
                exc,
            )
            return
        except AlexapyLoginError:
            _LOGGER.debug(
                "%s: Login error during last_called update", hide_email(email)
            )
            report_relogin_required(hass, login_obj, email)
            return
        except TypeError:
            _LOGGER.debug(
                "%s: Error updating last_called: %s",
                hide_email(email),
                repr(last_called),
            )
            return
        if not isinstance(last_called, dict):
            _LOGGER.debug(
                "%s: Error updating last_called: unexpected response %s",
                hide_email(email),
                repr(last_called),
            )
            return

    if not _valid_voice_summary(last_called.get("summary")):
        _LOGGER.debug(
            "%s: Ignoring last_called with invalid summary",
            hide_email(email),
        )
        return

    _LOGGER.debug(
        "%s: Updated last_called: %s", hide_email(email), hide_serial(last_called)
    )
    _store_and_dispatch_last_called(hass, email, last_called, force)


async def _async_update_last_called_background(
    hass: HomeAssistant, login_obj, email: str
) -> None:
    """Update last_called in background to avoid blocking startup."""
    try:
        await _async_update_last_called_global(hass, login_obj, email)
        _LOGGER.debug("%s: Background last_called update completed", hide_email(email))

        # Record metrics
        metrics = get_metrics(hass)
        if metrics:
            metrics.record_boot_stage(f"last_called_{hide_email(email)}")
    except asyncio.CancelledError:
        # Task cancelled during unload/shutdown; propagate cancellation.
        raise
    except Exception as err:
        _LOGGER.debug(
            "%s: Background last_called update failed: %s", hide_email(email), err
        )


def _is_dnd_voice_toggle(last_called: dict) -> bool:
    summary = " ".join(((last_called.get("summary") or "").strip().lower()).split())
    response = " ".join(((last_called.get("response") or "").strip().lower()).split())

    # Normalize apostrophes (replace smart quotes with ASCII)
    summary = summary.replace("\u2019", "'").replace("\u2018", "'")
    response = response.replace("\u2019", "'").replace("\u2018", "'")

    return (
        "do not disturb" in summary
        or "won't disturb you" in response
        or "do not disturb is now off" in response
    )


@_catch_login_errors
async def update_last_called(login_obj, ctx, last_called=None, force=False):
    """Update the last called device for the login_obj.

    Stores the last_called in hass.data and fires an event to notify listeners.
    Delegates storage/dispatch to the module-level _store_and_dispatch_last_called helper.
    """
    hass = ctx.hass
    email = ctx.email
    if not isinstance(last_called, dict) or not last_called.get("summary"):
        try:
            # Serialize calls per account to avoid parallel rate-limited requests.
            account = (
                hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {}).get(email, {})
            )
            api_lock = account.get("last_called_api_lock") if account else None

            if api_lock is None:
                last_called = await AlexaAPI.get_last_device_serial(login_obj)
            else:
                async with api_lock:
                    last_called = await AlexaAPI.get_last_device_serial(login_obj)

        except asyncio.CancelledError:
            raise

        except AlexapyTooManyRequestsError:
            _LOGGER.debug(
                "%s: Rate limited during last_called update; skipping",
                hide_email(email),
            )
            return

        except AlexapyLoginError:
            _LOGGER.debug(
                "%s: Login error during last_called update",
                hide_email(email),
            )
            report_relogin_required(hass, login_obj, email)
            return

        except AlexapyConnectionError as exc:
            _LOGGER.debug(
                "%s: Connection error during last_called update: %s",
                hide_email(email),
                exc,
            )
            return

        except TypeError:
            _LOGGER.debug(
                "%s: Error updating last_called: %s",
                hide_email(email),
                repr(last_called),
            )
            return

        if not isinstance(last_called, dict):
            _LOGGER.debug(
                "%s: Error updating last_called: unexpected response %s",
                hide_email(email),
                repr(last_called),
            )
            return

    # Central voice-only gate
    if not _valid_voice_summary(last_called.get("summary")):
        _LOGGER.debug(
            "%s: Ignoring last_called with invalid/non-voice summary: %s",
            hide_email(email),
            repr(last_called.get("summary")),
        )
        return

    _LOGGER.debug(
        "%s: Updated last_called: %s", hide_email(email), hide_serial(last_called)
    )
    _store_and_dispatch_last_called(hass, email, last_called, force)

    if _is_dnd_voice_toggle(last_called):
        _LOGGER.debug("%s: last_called indicates DND voice toggle", hide_email(email))
        await setup_dnd.update_dnd_state(login_obj, ctx)


def _init_last_called_probe_worker(ctx: SetupContext, account: dict) -> None:
    """Initialize per-account last_called probe worker trigger function."""
    hass = ctx.hass
    email = ctx.email
    debug = ctx.debug
    account.setdefault("last_called_api_lock", asyncio.Lock())
    account.setdefault("last_called_customer_history_ts", 0)  # ms epoch last applied
    account.setdefault("last_called_probe_backoff_s", 0.0)
    account.setdefault("last_called_probe_event", asyncio.Event())
    account.setdefault("last_called_probe_next_allowed", 0.0)  # monotonic seconds
    account.setdefault("last_called_probe_task", None)
    account.setdefault("last_called_probe_trigger_cmd", "")
    account.setdefault("last_called_probe_trigger_serial", None)
    account.setdefault("last_called_probe_trigger_ts", 0)  # newest push ts (ms)
    account.setdefault("last_called_probe_trigger", None)
    account.setdefault("last_called_activity_queue", [])
    account.setdefault("last_called_last_pushed_activity", {})
    account.setdefault("last_volumes", {})
    account.setdefault("last_equalizer", {})

    if callable(account.get("last_called_probe_trigger")):
        return

    async def _last_called_probe_worker() -> None:
        """Single worker per account: debounce bursts, then quick-retry until history >= trigger_ts."""
        skip_debounce = False
        try:
            while True:
                accounts = hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {})
                account_live = accounts.get(email)
                if not account_live:
                    return

                def _debug(msg, *args):
                    if debug:
                        _LOGGER.debug("%s: " + msg, hide_email(email), *args)

                login_live = account_live.get("login_obj")
                if not login_live or not _network_allowed(login_live):
                    await asyncio.sleep(5)
                    continue
                await account_live["last_called_probe_event"].wait()
                account_live["last_called_probe_event"].clear()

                if not skip_debounce:
                    await asyncio.sleep(
                        LAST_CALLED_DEBOUNCE_S + random.uniform(0.0, 0.05)  # nosec B311  # noqa: S311
                    )
                    if account_live["last_called_probe_event"].is_set():
                        continue
                skip_debounce = False

                preempted = False
                while True:
                    now = time.monotonic()
                    next_allowed = float(
                        account_live.get("last_called_probe_next_allowed", 0.0) or 0.0
                    )
                    delay = max(0.0, next_allowed - now)
                    if delay <= 0:
                        break
                    try:
                        await asyncio.wait_for(
                            account_live["last_called_probe_event"].wait(),
                            timeout=delay,
                        )
                        account_live["last_called_probe_event"].clear()
                        preempted = True
                        break
                    except TimeoutError:
                        break
                if preempted:
                    continue

                trigger_cmd = str(
                    account_live.get("last_called_probe_trigger_cmd") or "push"
                )

                for attempt in range(LAST_CALLED_RETRY_LIMIT + 1):
                    if account_live["last_called_probe_event"].is_set():
                        break

                    try:
                        async with account_live["last_called_api_lock"]:
                            queue_snapshot = _snapshot_last_called_activity_queue(
                                account_live
                            )
                            if not queue_snapshot:
                                if trigger_cmd in (
                                    "GLOBAL_REFRESH",
                                    "SERVICE_REFRESH",
                                    "POLL_REFRESH",
                                ):
                                    last_called = await AlexaAPI.get_last_device_serial(
                                        login_live, items=LAST_CALLED_ITEMS
                                    )
                                    if isinstance(
                                        last_called, dict
                                    ) and _valid_voice_summary(
                                        last_called.get("summary")
                                    ):
                                        await update_last_called(
                                            login_live, ctx, last_called
                                        )
                                    account_live["last_called_probe_trigger_ts"] = 0
                                break

                            earliest_ts = min(
                                (
                                    int(item.get("activity_ts") or 0)
                                    for item in queue_snapshot
                                    if item.get("activity_ts")
                                ),
                                default=0,
                            )

                            rvh_window_ms = max(LAST_CALLED_LOOKBACK_MS, 15 * 60 * 1000)
                            start_time = (
                                max(0, earliest_ts - rvh_window_ms)
                                if earliest_ts
                                else int((time.time() * 1000) - rvh_window_ms)
                            )
                            end_time = int(time.time() * 1000) + rvh_window_ms
                            max_record_size = max(
                                LAST_CALLED_ITEMS, len(queue_snapshot) + 2
                            )

                        try:
                            records = await AlexaAPI.get_customer_history_records(
                                login_live,
                                start_time=start_time,
                                end_time=end_time,
                                max_record_size=max_record_size,
                            )
                        except TypeError as exc:
                            # Known alexapy/aiohttp edge case: None header key, etc.
                            account_live["last_called_probe_next_allowed"] = (
                                time.monotonic() + LAST_CALLED_CONN_BACKOFF_S
                            )
                            _LOGGER.warning(
                                "%s: last_called probe API TypeError (%s): %s",
                                hide_email(email),
                                trigger_cmd,
                                exc,
                                exc_info=True,
                            )
                            # NOTE:
                            # We intentionally re-arm the probe (set event + backoff) on this TypeError
                            # because this path is typically caused by transient alexapy/aiohttp issues
                            # (e.g., None header key). Unlike Login/Connection errors, we retry quickly
                            # to avoid losing last_called updates triggered by push events.
                            skip_debounce = True
                            account_live["last_called_probe_event"].set()
                            break

                        if records is None:
                            _LOGGER.warning(
                                "%s: last_called probe API returned None (%s)",
                                hide_email(email),
                                trigger_cmd,
                            )
                            records = []

                        # 🔎 DEBUG: inspect raw history result
                        _LOGGER.debug(
                            "%s: last_called probe retrieved %s history records",
                            hide_email(email),
                            len(records or []),
                        )
                        if isinstance(records, list):
                            _debug(
                                "last_called probe raw records (%s): %s",
                                trigger_cmd,
                                [
                                    {
                                        "summary": (item.get("description") or {}).get(
                                            "summary"
                                        ),
                                        "response": item.get("alexaResponse"),
                                        "serial": item.get("deviceSerialNumber"),
                                        "ts": item.get("creationTimestamp"),
                                        "utteranceType": item.get("utteranceType"),
                                    }
                                    for item in records[:10]
                                    if isinstance(item, dict)
                                ],
                            )
                        else:
                            _debug(
                                "last_called probe returned unexpected type (%s): %r",
                                trigger_cmd,
                                records,
                            )

                    except asyncio.CancelledError:
                        raise
                    except AlexapyTooManyRequestsError:
                        uk_floor = random.uniform(  # noqa: S311
                            30.0, 63.0
                        )  # nosec B311
                        prev = float(
                            account_live.get("last_called_probe_backoff_s", 0.0) or 0.0
                        )
                        backoff = (
                            LAST_CALLED_429_BACKOFF_INITIAL_S
                            if prev <= 0.0
                            else min(prev * 2.0, LAST_CALLED_429_BACKOFF_MAX_S)
                        )
                        backoff = max(backoff, uk_floor)
                        jitter = random.uniform(  # noqa: S311
                            0.0, min(5.0, backoff * 0.1)
                        )  # nosec B311

                        account_live["last_called_probe_backoff_s"] = backoff
                        account_live["last_called_probe_next_allowed"] = (
                            time.monotonic() + backoff + jitter
                        )

                        _LOGGER.debug(
                            "%s: last_called probe rate-limited (%s); backing off %.1fs (%.1fs jitter) then self-retry",
                            hide_email(email),
                            trigger_cmd,
                            backoff,
                            jitter,
                        )
                        skip_debounce = True
                        account_live["last_called_probe_event"].set()
                        break
                    except AlexapyLoginError:
                        account_live["last_called_probe_next_allowed"] = (
                            time.monotonic() + LAST_CALLED_LOGIN_BACKOFF_S
                        )
                        _LOGGER.debug(
                            "%s: last_called probe login error (%s); skipping",
                            hide_email(email),
                            trigger_cmd,
                        )
                        report_relogin_required(hass, login_live, email)
                        break
                    except AlexapyConnectionError as exc:
                        account_live["last_called_probe_next_allowed"] = (
                            time.monotonic() + LAST_CALLED_CONN_BACKOFF_S
                        )
                        _LOGGER.debug(
                            "%s: last_called probe connection error (%s): %s",
                            hide_email(email),
                            trigger_cmd,
                            exc,
                        )
                        break

                    existing_serials_local = set(_existing_serials(hass, login_live))
                    existing_serials_local |= _entity_backed_serials(account_live)

                    payload, resolved_keys = _select_last_called_payload_from_records(
                        records,
                        queue_snapshot,
                        account_live,
                        existing_serials_local,
                    )

                    _debug(
                        "last_called queue match (%s): queued=%s matched=%s resolved=%s",
                        trigger_cmd,
                        [
                            {
                                "serial": item.get("serial"),
                                "customer_id": item.get("customer_id"),
                                "activity_ts": item.get("activity_ts"),
                                "command": item.get("command"),
                            }
                            for item in queue_snapshot
                        ],
                        (
                            {
                                "serialNumber": payload.get("serialNumber"),
                                "timestamp": payload.get("timestamp"),
                                "summary": payload.get("summary"),
                                "response": payload.get("response"),
                            }
                            if payload
                            else None
                        ),
                        sorted(resolved_keys),
                    )

                    if not payload:
                        if attempt < LAST_CALLED_RETRY_LIMIT:
                            await asyncio.sleep(LAST_CALLED_RETRY_DELAY_S)
                            continue

                        if trigger_cmd in (
                            "GLOBAL_REFRESH",
                            "SERVICE_REFRESH",
                            "POLL_REFRESH",
                        ):
                            _LOGGER.debug(
                                "%s: queued activity unresolved after retries; falling back to direct refresh",
                                hide_email(email),
                            )

                            unresolved_keys = {
                                (item.get("serial"), item.get("customer_id"))
                                for item in queue_snapshot
                                if item.get("serial")
                            }
                            _remove_last_called_activity_queue_entries(
                                account_live, unresolved_keys
                            )

                            try:
                                async with account_live["last_called_api_lock"]:
                                    last_called = await AlexaAPI.get_last_device_serial(
                                        login_live,
                                        items=LAST_CALLED_ITEMS,
                                    )
                                if isinstance(
                                    last_called, dict
                                ) and _valid_voice_summary(last_called.get("summary")):
                                    await update_last_called(
                                        login_live, ctx, last_called
                                    )
                            except asyncio.CancelledError:
                                raise
                            except AlexapyLoginError as exc:
                                _LOGGER.debug(
                                    "%s: fallback last_called refresh failed (%s): %s",
                                    hide_email(email),
                                    trigger_cmd,
                                    exc,
                                )
                                report_relogin_required(hass, login_live, email)
                            except (
                                AlexapyTooManyRequestsError,
                                AlexapyConnectionError,
                            ) as exc:
                                _LOGGER.debug(
                                    "%s: fallback last_called refresh failed (%s): %s",
                                    hide_email(email),
                                    trigger_cmd,
                                    exc,
                                )
                            account_live["last_called_probe_trigger_ts"] = 0
                            account_live["last_called_probe_event"].clear()

                        break

                    account_live["last_called_probe_backoff_s"] = 0.0
                    account_live["last_called_probe_next_allowed"] = (
                        time.monotonic()
                        + LAST_CALLED_SUCCESS_PACE_S
                        + random.uniform(0.0, 0.25)  # nosec B311  # noqa: S311
                    )

                    trigger_serial = account_live.get(
                        "last_called_probe_trigger_serial"
                    )
                    _LOGGER.debug(
                        "%s: Updating last_called via %s (triggered by %s): %s",
                        hide_email(email),
                        trigger_cmd,
                        (hide_serial(trigger_serial) if trigger_serial else "unknown"),
                        hide_serial(payload["serialNumber"]),
                    )

                    await update_last_called(login_live, ctx, payload)
                    account_live["last_called_last_pushed_activity"][
                        payload["serialNumber"]
                    ] = payload["timestamp"]
                    _remove_last_called_activity_queue_entries(
                        account_live, resolved_keys
                    )
                    account_live["last_called_probe_trigger_ts"] = 0
                    account_live["last_called_probe_event"].clear()
                    break
        except asyncio.CancelledError:
            raise

        except Exception:  # pylint: disable=broad-except
            _LOGGER.exception(
                "%s: last_called probe worker crashed",
                hide_email(email),
            )

    def _trigger_last_called_probe(
        trigger_command: str, trigger_ts_ms: int | None
    ) -> None:
        """Record newest trigger + wake worker. Does NOT cancel running worker."""
        accounts = hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {})
        account_live = accounts.get(email)
        if not account_live:
            return

        if trigger_ts_ms is not None:
            try:
                ts = int(trigger_ts_ms)
            except (TypeError, ValueError):
                ts = 0

            prev = int(account_live.get("last_called_probe_trigger_ts") or 0)
            if ts > prev:
                account_live["last_called_probe_trigger_ts"] = ts
            account_live["last_called_probe_trigger_cmd"] = trigger_command
        else:
            # Manual refresh triggers clear any push watermark
            if trigger_command in (
                "GLOBAL_REFRESH",
                "SERVICE_REFRESH",
                "POLL_REFRESH",
            ):
                account_live["last_called_probe_trigger_ts"] = 0
                account_live["last_called_probe_trigger_serial"] = None
            account_live["last_called_probe_trigger_cmd"] = trigger_command

        task = account_live.get("last_called_probe_task")
        if task is None or task.done():
            account_live["last_called_probe_task"] = hass.async_create_background_task(
                _last_called_probe_worker(),
                name=f"{DOMAIN}_last_called_probe_{hide_email(email)}",
            )

        account_live["last_called_probe_event"].set()

    # Store the trigger on the live account as well (so reload swaps don't strand it)
    account["last_called_probe_trigger"] = _trigger_last_called_probe
