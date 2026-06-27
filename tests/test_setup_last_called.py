"""Tests for the pure helpers in custom_components.alexa_media.setup.last_called."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from alexapy import AlexapyConnectionError, AlexapyLoginError
from alexapy.errors import AlexapyTooManyRequestsError
import pytest

from custom_components.alexa_media.const import DATA_ALEXAMEDIA
from custom_components.alexa_media.setup.context import SetupContext
from custom_components.alexa_media.setup.last_called import (
    _async_update_last_called_background,
    _async_update_last_called_global,
    _init_last_called_probe_worker,
    _is_dnd_voice_toggle,
    _queue_last_called_activity,
    _remove_last_called_activity_queue_entries,
    _select_last_called_payload_from_records,
    _snapshot_last_called_activity_queue,
    _store_and_dispatch_last_called,
    _valid_utterance_type,
    _valid_voice_summary,
    update_last_called,
)

# --- patch path constants ---------------------------------------------------
_MODULE = "custom_components.alexa_media.setup.last_called"
GET_LAST_SERIAL = f"{_MODULE}.AlexaAPI.get_last_device_serial"
NETWORK_ALLOWED = f"{_MODULE}._network_allowed"
REPORT_RELOGIN = f"{_MODULE}.report_relogin_required"
STORE_DISPATCH = f"{_MODULE}._store_and_dispatch_last_called"
DISPATCH = f"{_MODULE}.async_dispatcher_send"
GET_METRICS = f"{_MODULE}.get_metrics"
UPDATE_DND = f"{_MODULE}.setup_dnd.update_dnd_state"
GLOBAL_UPDATE = f"{_MODULE}._async_update_last_called_global"

EMAIL = "user@example.com"


def _hass(accounts=None):
    """Return a MagicMock hass whose .data carries a real accounts mapping."""
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {} if accounts is None else accounts}}
    return hass


def _ctx(account=None, *, email=EMAIL, debug=False):
    """Build a SetupContext-shaped mock backed by a fake hass."""
    accounts = {} if account is None else {email: account}
    ctx = MagicMock(spec=SetupContext)
    ctx.hass = _hass(accounts)
    ctx.email = email
    ctx.debug = debug
    return ctx


def _fake_create_task(store):
    """Return a fake hass.async_create_background_task that never runs the coro."""

    def _create(coro, name=None):
        store.append(name)
        coro.close()  # prevent "coroutine was never awaited" warnings
        return MagicMock()

    return _create


# ---------------------------------------------------------------------------
# _valid_voice_summary
# ---------------------------------------------------------------------------


def test_valid_voice_summary_accepts_real_utterance():
    assert _valid_voice_summary("turn off the lights") is True
    assert _valid_voice_summary("  what time is it  ") is True


def test_valid_voice_summary_rejects_non_strings_and_empty():
    assert _valid_voice_summary(None) is False
    assert _valid_voice_summary(123) is False
    assert _valid_voice_summary("") is False
    assert _valid_voice_summary("   ") is False
    # punctuation only -> no alphanumeric -> rejected
    assert _valid_voice_summary("...!!") is False


# ---------------------------------------------------------------------------
# _valid_utterance_type
# ---------------------------------------------------------------------------


def test_valid_utterance_type_filters_known_noise():
    for noise in ("DEVICE_ARBITRATION", "ASR_TIMEOUT", "WAKE_WORD_ONLY"):
        assert _valid_utterance_type({"utteranceType": noise}) is False


def test_valid_utterance_type_accepts_others_and_missing():
    assert _valid_utterance_type({"utteranceType": "GENERAL"}) is True
    assert _valid_utterance_type({}) is True


# ---------------------------------------------------------------------------
# _queue_last_called_activity / _snapshot / _remove
# ---------------------------------------------------------------------------


def test_queue_last_called_activity_appends_new_entry():
    account: dict = {}
    _queue_last_called_activity(
        account,
        device_serial="SERIAL1",
        customer_id="CUST1",
        activity_ts=1000,
        command="PUSH_VOLUME_CHANGE",
    )
    queue = account["last_called_activity_queue"]
    assert queue == [
        {
            "serial": "SERIAL1",
            "customer_id": "CUST1",
            "activity_ts": 1000,
            "command": "PUSH_VOLUME_CHANGE",
        }
    ]


def test_queue_last_called_activity_ignores_empty_serial():
    account: dict = {}
    _queue_last_called_activity(
        account,
        device_serial="",
        customer_id="C",
        activity_ts=1,
        command="X",
    )
    assert account.get("last_called_activity_queue", []) == []


def test_queue_last_called_activity_keeps_earliest_ts_on_refresh():
    account: dict = {}
    kwargs = {"device_serial": "S", "customer_id": "C", "command": "A"}
    _queue_last_called_activity(account, activity_ts=2000, **kwargs)
    # Same (serial, customer) with an EARLIER ts and a new command -> updated in place.
    _queue_last_called_activity(
        account, activity_ts=1000, command="B", device_serial="S", customer_id="C"
    )
    queue = account["last_called_activity_queue"]
    assert len(queue) == 1
    assert queue[0]["activity_ts"] == 1000  # earliest kept
    assert queue[0]["command"] == "B"


def test_queue_last_called_activity_coerces_bad_ts_to_zero():
    account: dict = {}
    _queue_last_called_activity(
        account,
        device_serial="S",
        customer_id="C",
        activity_ts="not-an-int",
        command="A",
    )
    assert account["last_called_activity_queue"][0]["activity_ts"] == 0


def test_snapshot_returns_independent_copies():
    account = {
        "last_called_activity_queue": [
            {"serial": "S", "customer_id": "C", "activity_ts": 1, "command": "A"},
            "not-a-dict",  # filtered out
        ]
    }
    snap = _snapshot_last_called_activity_queue(account)
    assert snap == [
        {"serial": "S", "customer_id": "C", "activity_ts": 1, "command": "A"}
    ]
    # Mutating the snapshot must not affect the account queue.
    snap[0]["command"] = "MUTATED"
    assert account["last_called_activity_queue"][0]["command"] == "A"


def test_snapshot_empty_when_missing():
    assert _snapshot_last_called_activity_queue({}) == []


def test_remove_entries_by_resolved_keys():
    account = {
        "last_called_activity_queue": [
            {"serial": "S1", "customer_id": "C1"},
            {"serial": "S2", "customer_id": "C2"},
            {"serial": "S3", "customer_id": None},
        ]
    }
    _remove_last_called_activity_queue_entries(account, {("S2", "C2"), ("S3", None)})
    assert account["last_called_activity_queue"] == [
        {"serial": "S1", "customer_id": "C1"}
    ]


# ---------------------------------------------------------------------------
# _select_last_called_payload_from_records
# ---------------------------------------------------------------------------


def _record(serial="S", ts=2000, summary="play music", utt="GENERAL", response="ok"):
    return {
        "deviceSerialNumber": serial,
        "creationTimestamp": ts,
        "utteranceType": utt,
        "description": {"summary": summary},
        "alexaResponse": response,
    }


def test_select_payload_none_without_records_or_queue():
    assert _select_last_called_payload_from_records(
        [], [{"serial": "S"}], {}, {"S"}
    ) == (
        None,
        set(),
    )
    assert _select_last_called_payload_from_records([_record()], [], {}, {"S"}) == (
        None,
        set(),
    )


def test_select_payload_happy_path():
    records = [_record(serial="S", ts=2000, summary="play jazz", response="Playing")]
    queue = [{"serial": "S", "customer_id": "C", "activity_ts": 1990}]
    payload, keys = _select_last_called_payload_from_records(records, queue, {}, {"S"})
    assert payload == {
        "serialNumber": "S",
        "timestamp": 2000,
        "summary": "play jazz",
        "response": "Playing",
    }
    assert keys == {("S", "C")}


def test_select_payload_filters_unknown_serial_and_noise_and_watermark():
    queue = [{"serial": "S", "customer_id": "C", "activity_ts": 0}]
    # serial not in existing_serials
    assert _select_last_called_payload_from_records(
        [_record(serial="S")], queue, {}, {"OTHER"}
    ) == (
        None,
        set(),
    )
    # noise utterance type
    assert _select_last_called_payload_from_records(
        [_record(serial="S", utt="WAKE_WORD_ONLY")], queue, {}, {"S"}
    ) == (None, set())
    # invalid (empty) summary
    assert _select_last_called_payload_from_records(
        [_record(serial="S", summary="")], queue, {}, {"S"}
    ) == (None, set())
    # ts at/under the stored watermark
    account = {"last_called_customer_history_ts": 5000}
    assert _select_last_called_payload_from_records(
        [_record(serial="S", ts=5000)], queue, account, {"S"}
    ) == (None, set())


# ---------------------------------------------------------------------------
# _queue_last_called_activity — extra refresh branches
# ---------------------------------------------------------------------------


def test_queue_keeps_earliest_when_refresh_has_later_ts():
    account: dict = {}
    kw = {"device_serial": "S", "customer_id": "C"}
    _queue_last_called_activity(account, activity_ts=1000, command="A", **kw)
    # Later ts must NOT overwrite the earliest, but command is still refreshed.
    _queue_last_called_activity(account, activity_ts=5000, command="B", **kw)
    item = account["last_called_activity_queue"][0]
    assert item["activity_ts"] == 1000
    assert item["command"] == "B"


def test_queue_upgrades_zero_ts_on_refresh():
    account: dict = {}
    kw = {"device_serial": "S", "customer_id": "C"}
    _queue_last_called_activity(account, activity_ts=None, command="A", **kw)  # -> 0
    _queue_last_called_activity(account, activity_ts=2000, command="B", **kw)
    item = account["last_called_activity_queue"][0]
    assert item["activity_ts"] == 2000  # upgraded from 0


def test_remove_entries_handles_missing_queue():
    account: dict = {}
    _remove_last_called_activity_queue_entries(account, {("S", "C")})
    assert account["last_called_activity_queue"] == []


# ---------------------------------------------------------------------------
# _select_last_called_payload_from_records — remaining branches
# ---------------------------------------------------------------------------


def test_select_payload_skips_last_pushed_activity():
    records = [_record(serial="S", ts=2000)]
    queue = [{"serial": "S", "customer_id": "C", "activity_ts": 1990}]
    account = {"last_called_last_pushed_activity": {"S": 2000}}
    # ts (2000) <= last_pushed (2000) -> skipped.
    assert _select_last_called_payload_from_records(records, queue, account, {"S"}) == (
        None,
        set(),
    )


def test_select_payload_skips_when_serial_not_in_queue():
    records = [_record(serial="S", ts=2000)]
    queue = [{"serial": "OTHER", "customer_id": "C", "activity_ts": 1990}]
    assert _select_last_called_payload_from_records(records, queue, {}, {"S"}) == (
        None,
        set(),
    )


def test_select_payload_skips_stale_relative_to_queue():
    # queued_ts=20000, fudge=5000 -> records older than 15000 are stale.
    records = [_record(serial="S", ts=10000)]
    queue = [{"serial": "S", "customer_id": "C", "activity_ts": 20000}]
    assert _select_last_called_payload_from_records(records, queue, {}, {"S"}) == (
        None,
        set(),
    )


def test_select_payload_skips_unparseable_creation_ts():
    records = [_record(serial="S", ts="not-int", summary="play")]
    queue = [{"serial": "S", "customer_id": "C", "activity_ts": 1990}]
    assert _select_last_called_payload_from_records(records, queue, {}, {"S"}) == (
        None,
        set(),
    )


# ---------------------------------------------------------------------------
# _store_and_dispatch_last_called
# ---------------------------------------------------------------------------


def test_store_dispatch_account_removed_early_return():
    hass = _hass()  # no accounts
    with patch(DISPATCH) as disp:
        _store_and_dispatch_last_called(
            hass, EMAIL, {"summary": "x", "timestamp": 1000}
        )
    disp.assert_not_called()


def test_store_dispatch_seconds_to_ms_and_dispatch_on_prev_none():
    account: dict = {}
    hass = _hass({EMAIL: account})
    with patch(DISPATCH) as disp:
        _store_and_dispatch_last_called(
            hass, EMAIL, {"summary": "x", "timestamp": 1000}
        )
    assert account["last_called"]["timestamp"] == 1_000_000  # seconds -> ms
    assert account["last_called_customer_history_ts"] == 1_000_000
    disp.assert_called_once()
    dispatched = disp.call_args.args[2]["last_called_change"]
    assert dispatched["timestamp"] == 1_000_000


def test_store_dispatch_ms_timestamp_not_multiplied():
    account: dict = {}
    hass = _hass({EMAIL: account})
    ms = 1_700_000_000_000  # already an epoch-ms value (>= 10_000_000_000)
    with patch(DISPATCH):
        _store_and_dispatch_last_called(hass, EMAIL, {"summary": "x", "timestamp": ms})
    assert account["last_called"]["timestamp"] == ms


def test_store_dispatch_no_dispatch_when_unchanged_and_not_forced():
    account = {"last_called": {"summary": "x", "timestamp": 1_000_000}}
    hass = _hass({EMAIL: account})
    with patch(DISPATCH) as disp:
        # 1000s normalizes to the already-stored 1_000_000 ms -> unchanged.
        _store_and_dispatch_last_called(
            hass, EMAIL, {"summary": "x", "timestamp": 1000}
        )
    disp.assert_not_called()


def test_store_dispatch_force_dispatches_even_when_unchanged():
    account = {"last_called": {"summary": "x", "timestamp": 1_000_000}}
    hass = _hass({EMAIL: account})
    with patch(DISPATCH) as disp:
        _store_and_dispatch_last_called(
            hass, EMAIL, {"summary": "x", "timestamp": 1000}, force=True
        )
    disp.assert_called_once()


def test_store_dispatch_history_ts_uses_max():
    account = {"last_called_customer_history_ts": 5_000_000}
    hass = _hass({EMAIL: account})
    with patch(DISPATCH):
        _store_and_dispatch_last_called(
            hass, EMAIL, {"summary": "x", "timestamp": 1000}
        )
    # incoming 1_000_000 < existing 5_000_000 -> max() keeps the larger value.
    assert account["last_called_customer_history_ts"] == 5_000_000


def test_store_dispatch_bad_timestamp_coerced_to_zero():
    account: dict = {}
    hass = _hass({EMAIL: account})
    with patch(DISPATCH) as disp:
        _store_and_dispatch_last_called(
            hass, EMAIL, {"summary": "x", "timestamp": "bad"}
        )
    # ts coerces to 0 -> not normalized, history untouched, but prev is None -> dispatch.
    assert account["last_called"]["timestamp"] == "bad"
    assert "last_called_customer_history_ts" not in account
    disp.assert_called_once()


# ---------------------------------------------------------------------------
# _is_dnd_voice_toggle
# ---------------------------------------------------------------------------


def test_is_dnd_voice_toggle_summary_contains_phrase():
    assert _is_dnd_voice_toggle({"summary": "Do Not Disturb is on"}) is True


def test_is_dnd_voice_toggle_response_wont_disturb_smartquote():
    # The smart apostrophe (U+2019) must be normalized to ASCII before matching.
    assert (
        _is_dnd_voice_toggle({"summary": "", "response": "I won’t disturb you"}) is True
    )


def test_is_dnd_voice_toggle_response_now_off():
    assert (
        _is_dnd_voice_toggle({"summary": "", "response": "Do not disturb is now off"})
        is True
    )


def test_is_dnd_voice_toggle_negative_and_missing_keys():
    assert (
        _is_dnd_voice_toggle({"summary": "play music", "response": "playing"}) is False
    )
    assert _is_dnd_voice_toggle({}) is False


# ---------------------------------------------------------------------------
# _async_update_last_called_global
# ---------------------------------------------------------------------------


async def test_global_returns_when_network_not_allowed():
    hass = _hass()
    with (
        patch(NETWORK_ALLOWED, return_value=False),
        patch(GET_LAST_SERIAL, new=AsyncMock()) as api,
        patch(STORE_DISPATCH) as store,
    ):
        await _async_update_last_called_global(hass, MagicMock(), EMAIL)
    api.assert_not_called()
    store.assert_not_called()


async def test_global_uses_probe_trigger_when_available():
    trigger = MagicMock()
    account = {"last_called_probe_trigger": trigger}
    hass = _hass({EMAIL: account})
    with (
        patch(NETWORK_ALLOWED, return_value=True),
        patch(GET_LAST_SERIAL, new=AsyncMock()) as api,
    ):
        await _async_update_last_called_global(hass, MagicMock(), EMAIL)
    trigger.assert_called_once_with("GLOBAL_REFRESH", None)
    api.assert_not_called()


async def test_global_swallows_rate_limit():
    hass = _hass()
    with (
        patch(NETWORK_ALLOWED, return_value=True),
        patch(
            GET_LAST_SERIAL, new=AsyncMock(side_effect=AlexapyTooManyRequestsError())
        ),
        patch(STORE_DISPATCH) as store,
    ):
        await _async_update_last_called_global(hass, MagicMock(), EMAIL)
    store.assert_not_called()


async def test_global_swallows_connection_error():
    hass = _hass()
    with (
        patch(NETWORK_ALLOWED, return_value=True),
        patch(GET_LAST_SERIAL, new=AsyncMock(side_effect=AlexapyConnectionError())),
        patch(STORE_DISPATCH) as store,
    ):
        await _async_update_last_called_global(hass, MagicMock(), EMAIL)
    store.assert_not_called()


async def test_global_login_error_reports_relogin():
    hass = _hass()
    with (
        patch(NETWORK_ALLOWED, return_value=True),
        patch(GET_LAST_SERIAL, new=AsyncMock(side_effect=AlexapyLoginError())),
        patch(REPORT_RELOGIN) as report,
        patch(STORE_DISPATCH) as store,
    ):
        await _async_update_last_called_global(hass, MagicMock(), EMAIL)
    report.assert_called_once()
    store.assert_not_called()


async def test_global_type_error_returns():
    hass = _hass()
    with (
        patch(NETWORK_ALLOWED, return_value=True),
        patch(GET_LAST_SERIAL, new=AsyncMock(side_effect=TypeError())),
        patch(STORE_DISPATCH) as store,
    ):
        await _async_update_last_called_global(hass, MagicMock(), EMAIL)
    store.assert_not_called()


async def test_global_non_dict_response_returns():
    hass = _hass()
    with (
        patch(NETWORK_ALLOWED, return_value=True),
        patch(GET_LAST_SERIAL, new=AsyncMock(return_value=None)),
        patch(STORE_DISPATCH) as store,
    ):
        await _async_update_last_called_global(hass, MagicMock(), EMAIL)
    store.assert_not_called()


async def test_global_invalid_summary_returns():
    hass = _hass()
    with (
        patch(NETWORK_ALLOWED, return_value=True),
        patch(GET_LAST_SERIAL, new=AsyncMock(return_value={"summary": ""})),
        patch(STORE_DISPATCH) as store,
    ):
        await _async_update_last_called_global(hass, MagicMock(), EMAIL)
    store.assert_not_called()


async def test_global_happy_path_stores_and_dispatches():
    hass = _hass()
    result = {"summary": "play jazz", "serialNumber": "S", "timestamp": 1000}
    with (
        patch(NETWORK_ALLOWED, return_value=True),
        patch(GET_LAST_SERIAL, new=AsyncMock(return_value=result)),
        patch(STORE_DISPATCH) as store,
    ):
        await _async_update_last_called_global(hass, MagicMock(), EMAIL)
    store.assert_called_once_with(hass, EMAIL, result, False)


# ---------------------------------------------------------------------------
# update_last_called (decorated; first positional arg is login_obj)
# ---------------------------------------------------------------------------


async def test_update_last_called_rate_limit_returns():
    ctx = _ctx()
    with (
        patch(
            GET_LAST_SERIAL, new=AsyncMock(side_effect=AlexapyTooManyRequestsError())
        ),
        patch(STORE_DISPATCH) as store,
    ):
        await update_last_called(MagicMock(), ctx)
    store.assert_not_called()


async def test_update_last_called_login_error_reports_relogin():
    ctx = _ctx()
    with (
        patch(GET_LAST_SERIAL, new=AsyncMock(side_effect=AlexapyLoginError())),
        patch(REPORT_RELOGIN) as report,
        patch(STORE_DISPATCH) as store,
    ):
        await update_last_called(MagicMock(), ctx)
    report.assert_called_once()
    store.assert_not_called()


async def test_update_last_called_connection_error_returns():
    ctx = _ctx()
    with (
        patch(GET_LAST_SERIAL, new=AsyncMock(side_effect=AlexapyConnectionError())),
        patch(STORE_DISPATCH) as store,
    ):
        await update_last_called(MagicMock(), ctx)
    store.assert_not_called()


async def test_update_last_called_type_error_returns():
    ctx = _ctx()
    with (
        patch(GET_LAST_SERIAL, new=AsyncMock(side_effect=TypeError())),
        patch(STORE_DISPATCH) as store,
    ):
        await update_last_called(MagicMock(), ctx)
    store.assert_not_called()


async def test_update_last_called_non_dict_response_returns():
    ctx = _ctx()
    with (
        patch(GET_LAST_SERIAL, new=AsyncMock(return_value=["unexpected"])),
        patch(STORE_DISPATCH) as store,
    ):
        await update_last_called(MagicMock(), ctx)
    store.assert_not_called()


async def test_update_last_called_voice_gate_rejects_non_voice_summary():
    ctx = _ctx()
    # Valid dict but a non-voice (no alphanumeric) summary -> central voice gate.
    with (
        patch(GET_LAST_SERIAL, new=AsyncMock()) as api,
        patch(STORE_DISPATCH) as store,
    ):
        await update_last_called(MagicMock(), ctx, {"summary": "!!!"})
    api.assert_not_called()  # summary present -> no API call
    store.assert_not_called()


async def test_update_last_called_happy_path_stores_no_dnd():
    ctx = _ctx()
    lc = {"summary": "play jazz", "response": "Playing"}
    with (
        patch(STORE_DISPATCH) as store,
        patch(UPDATE_DND, new=AsyncMock()) as dnd,
    ):
        await update_last_called(MagicMock(), ctx, lc)
    store.assert_called_once_with(ctx.hass, EMAIL, lc, False)
    dnd.assert_not_awaited()


async def test_update_last_called_dnd_toggle_updates_dnd_state():
    ctx = _ctx()
    login = MagicMock()
    lc = {"summary": "Do Not Disturb", "response": "ok"}
    with (
        patch(STORE_DISPATCH) as store,
        patch(UPDATE_DND, new=AsyncMock()) as dnd,
    ):
        await update_last_called(login, ctx, lc)
    store.assert_called_once()
    dnd.assert_awaited_once_with(login, ctx)


# ---------------------------------------------------------------------------
# _async_update_last_called_background
# ---------------------------------------------------------------------------


async def test_background_records_metrics_on_success():
    hass = _hass()
    metrics = MagicMock()
    with (
        patch(GLOBAL_UPDATE, new=AsyncMock()) as glob,
        patch(GET_METRICS, return_value=metrics),
    ):
        await _async_update_last_called_background(hass, MagicMock(), EMAIL)
    glob.assert_awaited_once()
    metrics.record_boot_stage.assert_called_once()


async def test_background_no_metrics_object_is_ok():
    hass = _hass()
    with (
        patch(GLOBAL_UPDATE, new=AsyncMock()),
        patch(GET_METRICS, return_value=None),
    ):
        # Must not raise when get_metrics returns None.
        await _async_update_last_called_background(hass, MagicMock(), EMAIL)


async def test_background_swallows_generic_exception():
    hass = _hass()
    with (
        patch(GLOBAL_UPDATE, new=AsyncMock(side_effect=RuntimeError("boom"))),
        patch(GET_METRICS) as get_metrics,
    ):
        # Generic Exception is swallowed; metrics block is never reached.
        await _async_update_last_called_background(hass, MagicMock(), EMAIL)
    get_metrics.assert_not_called()


async def test_background_reraises_cancelled_error():
    hass = _hass()
    with patch(GLOBAL_UPDATE, new=AsyncMock(side_effect=asyncio.CancelledError())):
        with pytest.raises(asyncio.CancelledError):
            await _async_update_last_called_background(hass, MagicMock(), EMAIL)


# ---------------------------------------------------------------------------
# _init_last_called_probe_worker
# ---------------------------------------------------------------------------


def test_init_probe_worker_sets_defaults_and_installs_trigger():
    account: dict = {}
    ctx = _ctx(account)
    created: list = []
    ctx.hass.async_create_background_task = _fake_create_task(created)

    _init_last_called_probe_worker(ctx, account)

    assert isinstance(account["last_called_api_lock"], asyncio.Lock)
    assert account["last_called_customer_history_ts"] == 0
    assert account["last_called_probe_backoff_s"] == 0.0
    assert isinstance(account["last_called_probe_event"], asyncio.Event)
    assert account["last_called_probe_next_allowed"] == 0.0
    assert account["last_called_probe_task"] is None
    assert account["last_called_probe_trigger_cmd"] == ""
    assert account["last_called_probe_trigger_serial"] is None
    assert account["last_called_probe_trigger_ts"] == 0
    assert account["last_called_activity_queue"] == []
    assert account["last_called_last_pushed_activity"] == {}
    assert account["last_volumes"] == {}
    assert account["last_equalizer"] == {}
    assert callable(account["last_called_probe_trigger"])


def test_probe_trigger_with_push_ts_sets_state_and_wakes_worker():
    account: dict = {}
    ctx = _ctx(account)
    created: list = []
    ctx.hass.async_create_background_task = _fake_create_task(created)
    _init_last_called_probe_worker(ctx, account)

    account["last_called_probe_trigger"]("PUSH_VOLUME_CHANGE", 1234)

    assert account["last_called_probe_trigger_ts"] == 1234
    assert account["last_called_probe_trigger_cmd"] == "PUSH_VOLUME_CHANGE"
    assert account["last_called_probe_event"].is_set()
    assert account["last_called_probe_task"] is not None
    assert created  # a background worker task was scheduled


def test_probe_trigger_global_refresh_clears_watermark():
    account: dict = {}
    ctx = _ctx(account)
    created: list = []
    ctx.hass.async_create_background_task = _fake_create_task(created)
    _init_last_called_probe_worker(ctx, account)
    account["last_called_probe_trigger_ts"] = 999
    account["last_called_probe_trigger_serial"] = "OLD"

    account["last_called_probe_trigger"]("GLOBAL_REFRESH", None)

    assert account["last_called_probe_trigger_ts"] == 0
    assert account["last_called_probe_trigger_serial"] is None
    assert account["last_called_probe_trigger_cmd"] == "GLOBAL_REFRESH"
    assert account["last_called_probe_event"].is_set()


def test_probe_trigger_bad_ts_coerced_to_zero():
    account: dict = {}
    ctx = _ctx(account)
    created: list = []
    ctx.hass.async_create_background_task = _fake_create_task(created)
    _init_last_called_probe_worker(ctx, account)

    account["last_called_probe_trigger"]("PUSH", "not-an-int")

    assert account["last_called_probe_trigger_ts"] == 0  # prev 0, bad -> 0, not > 0
    assert account["last_called_probe_trigger_cmd"] == "PUSH"


def test_probe_trigger_noop_when_account_removed():
    account: dict = {}
    ctx = _ctx(account)
    created: list = []
    ctx.hass.async_create_background_task = _fake_create_task(created)
    _init_last_called_probe_worker(ctx, account)
    # Account disappears (e.g. unload) before the trigger fires.
    ctx.hass.data[DATA_ALEXAMEDIA]["accounts"].clear()

    account["last_called_probe_trigger"]("PUSH", 1)  # must not raise

    assert created == []  # no worker scheduled


def test_init_probe_worker_idempotent_when_trigger_callable_exists():
    sentinel = MagicMock()  # already a callable trigger
    account = {"last_called_probe_trigger": sentinel}
    ctx = _ctx(account)

    _init_last_called_probe_worker(ctx, account)

    # Defaults are still populated, but the existing trigger is NOT replaced.
    assert isinstance(account["last_called_probe_event"], asyncio.Event)
    assert account["last_called_probe_trigger"] is sentinel


# ---------------------------------------------------------------------------
# api_lock serialization + CancelledError propagation
# ---------------------------------------------------------------------------


async def test_global_uses_api_lock_when_present():
    # Account present, no probe trigger, but a real per-account lock -> async with.
    account = {"last_called_api_lock": asyncio.Lock()}
    hass = _hass({EMAIL: account})
    result = {"summary": "play jazz", "serialNumber": "S"}
    with (
        patch(NETWORK_ALLOWED, return_value=True),
        patch(GET_LAST_SERIAL, new=AsyncMock(return_value=result)),
        patch(STORE_DISPATCH) as store,
    ):
        await _async_update_last_called_global(hass, MagicMock(), EMAIL)
    store.assert_called_once_with(hass, EMAIL, result, False)


async def test_global_reraises_cancelled_error():
    hass = _hass()
    with (
        patch(NETWORK_ALLOWED, return_value=True),
        patch(GET_LAST_SERIAL, new=AsyncMock(side_effect=asyncio.CancelledError())),
    ):
        with pytest.raises(asyncio.CancelledError):
            await _async_update_last_called_global(hass, MagicMock(), EMAIL)


async def test_update_last_called_uses_api_lock_when_present():
    account = {"last_called_api_lock": asyncio.Lock()}
    ctx = _ctx(account)
    result = {"summary": "play jazz", "response": "Playing"}
    with (
        patch(GET_LAST_SERIAL, new=AsyncMock(return_value=result)),
        patch(STORE_DISPATCH) as store,
        patch(UPDATE_DND, new=AsyncMock()),
    ):
        await update_last_called(MagicMock(), ctx)
    store.assert_called_once_with(ctx.hass, EMAIL, result, False)


async def test_update_last_called_reraises_cancelled_error():
    ctx = _ctx()
    with patch(GET_LAST_SERIAL, new=AsyncMock(side_effect=asyncio.CancelledError())):
        with pytest.raises(asyncio.CancelledError):
            await update_last_called(MagicMock(), ctx)
