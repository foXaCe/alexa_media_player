"""Tests for the pure helpers in custom_components.alexa_media.setup.last_called."""

from custom_components.alexa_media.setup.last_called import (
    _queue_last_called_activity,
    _remove_last_called_activity_queue_entries,
    _select_last_called_payload_from_records,
    _snapshot_last_called_activity_queue,
    _valid_utterance_type,
    _valid_voice_summary,
)

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
