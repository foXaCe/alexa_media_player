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

from alexapy import AlexaAPI, AlexapyConnectionError, AlexapyLoginError, hide_serial
from alexapy.errors import AlexapyTooManyRequestsError
from homeassistant.core import HomeAssistant
from homeassistant.helpers.dispatcher import async_dispatcher_send

from ..const import DATA_ALEXAMEDIA, DOMAIN, LAST_CALLED_STALE_FUDGE_MS
from ..helpers import _network_allowed, hide_email, report_relogin_required
from ..metrics import get_metrics

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
