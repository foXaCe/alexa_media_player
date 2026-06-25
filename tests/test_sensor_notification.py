"""Tests for AlexaMediaNotificationSensor logic (Alarm/Timer/Reminder sensors)."""

import datetime
from unittest.mock import MagicMock

from homeassistant.util import dt

from custom_components.alexa_media.sensor import (
    AlarmSensor,
    ReminderSensor,
    TimerSensor,
)


def _client(timezone=None):
    client = MagicMock()
    client.unique_id = "SN1"
    client.device_serial_number = "SN1"
    client.assumed_state = False
    client.available = True
    client._timezone = timezone
    return client


def _alarm(n_json=None, timezone=None):
    return AlarmSensor(_client(timezone), n_json or {}, "test@example.com")


def _timer(n_json=None):
    return TimerSensor(_client(), n_json or {}, "test@example.com")


def _reminder(n_json=None):
    return ReminderSensor(_client(), n_json or {}, "test@example.com")


# --------------------------------------------------------------------------- #
# construction / identity
# --------------------------------------------------------------------------- #


def test_sensor_construction_sets_identity():
    s = _alarm()
    assert s._attr_unique_id == "SN1_next Alarm"
    assert s._type == "Alarm"
    assert s._sensor_property == "date_time"
    assert s._attr_device_info["identifiers"] == {("alexa_media", "SN1")}


# --------------------------------------------------------------------------- #
# _coerce_datetime
# --------------------------------------------------------------------------- #


def test_coerce_datetime_passthrough_aware():
    s = _alarm()
    aware = datetime.datetime(2023, 1, 1, tzinfo=datetime.UTC)
    assert s._coerce_datetime(aware) == aware


def test_coerce_datetime_none_and_empty():
    s = _alarm()
    assert s._coerce_datetime(None) is None
    assert s._coerce_datetime("") is None


def test_coerce_datetime_epoch_ms_and_seconds():
    s = _alarm()
    from_ms = s._coerce_datetime(1_700_000_000_000)  # > threshold -> ms
    from_s = s._coerce_datetime(1_700_000_000)  # < threshold -> seconds
    assert from_ms.year == 2023
    assert from_s.year == 2023


def test_coerce_datetime_string():
    s = _alarm()
    assert s._coerce_datetime("not-a-date") is None
    parsed = s._coerce_datetime("2023-06-15T10:00:00+00:00")
    assert parsed is not None
    assert parsed.year == 2023


def test_coerce_datetime_naive_made_aware():
    s = _alarm(timezone="UTC")
    result = s._coerce_datetime(datetime.datetime(2023, 1, 1))
    assert result.tzinfo is not None


# --------------------------------------------------------------------------- #
# _is_active_notification
# --------------------------------------------------------------------------- #


def test_is_active_notification_on_and_off():
    s = _alarm()
    now = dt.now()
    assert s._is_active_notification(("id", {"status": "ON"}), now) is True
    assert s._is_active_notification(("id", {"status": "OFF"}), now) is False


def test_is_active_notification_snoozed():
    s = _alarm()
    now = dt.now()
    future = (now + datetime.timedelta(hours=1)).isoformat()
    past = (now - datetime.timedelta(hours=1)).isoformat()
    assert (
        s._is_active_notification(
            ("id", {"status": "SNOOZED", "snoozedToTime": future}), now
        )
        is True
    )
    assert (
        s._is_active_notification(
            ("id", {"status": "SNOOZED", "snoozedToTime": past}), now
        )
        is False
    )
    # no snoozedToTime -> still considered active
    assert s._is_active_notification(("id", {"status": "SNOOZED"}), now) is True


# --------------------------------------------------------------------------- #
# _select_next_alarm
# --------------------------------------------------------------------------- #


def test_select_next_alarm_prefers_future():
    s = _alarm()
    now = dt.now()
    future = now + datetime.timedelta(hours=1)
    past = now - datetime.timedelta(hours=1)
    s._active = [
        ("id1", {"date_time": past.isoformat(), "status": "ON"}),
        ("id2", {"date_time": future.isoformat(), "status": "ON"}),
    ]
    result = s._select_next_alarm(now)
    assert result["date_time"] == future.isoformat()


def test_select_next_alarm_falls_back_to_first_when_all_past():
    s = _alarm()
    now = dt.now()
    past = now - datetime.timedelta(hours=1)
    s._active = [("id1", {"date_time": past.isoformat(), "status": "ON"})]
    assert s._select_next_alarm(now)["date_time"] == past.isoformat()


def test_select_next_alarm_empty():
    s = _alarm()
    s._active = []
    assert s._select_next_alarm(dt.now()) is None


# --------------------------------------------------------------------------- #
# _normalize_alarm_snooze_state
# --------------------------------------------------------------------------- #


def test_normalize_alarm_snooze_future_marks_snoozed():
    s = _alarm()
    now = dt.now()
    future = now + datetime.timedelta(hours=1)
    value = (
        "id",
        {"status": "ON", "snoozedToTime": future.isoformat(), "date_time": "x"},
    )
    result = s._normalize_alarm_snooze_state(value)
    assert result[1]["status"] == "SNOOZED"
    assert result[1]["date_time"] == result[1]["snoozedToTime"]


def test_normalize_non_alarm_passthrough():
    s = _timer()  # _type == "Timer"
    value = ("id", {"status": "ON"})
    assert s._normalize_alarm_snooze_state(value) == value


# --------------------------------------------------------------------------- #
# TimerSensor / ReminderSensor properties + _process_state
# --------------------------------------------------------------------------- #


def test_timer_paused_icon_and_label():
    s = _timer()
    s._next = {"status": "PAUSED", "timerLabel": "Pasta"}
    assert s.paused is True
    assert "off" in s.icon
    assert s.timer == "Pasta"
    s._next = {"status": "ON", "timerLabel": "Pasta"}
    assert s.paused is False
    assert s.icon == s._attr_icon
    s._next = None
    assert s.paused is None
    assert s.timer is None


def test_timer_process_state():
    s = _timer()
    s._timestamp = datetime.datetime(2023, 6, 15, 10, 0, 0, tzinfo=datetime.UTC)
    assert s._process_state({"remainingTime": 60000}) is not None
    s._timestamp = None
    assert s._process_state({"remainingTime": 60000}) is None
    assert s._process_state(None) is None


def test_reminder_label_and_process_state():
    s = _reminder()
    s._next = {"reminderLabel": "Call Mom"}
    assert s.reminder == "Call Mom"
    s._next = None
    assert s.reminder is None
    result = s._process_state({"alarmTime": 1_700_000_000_000})
    assert result is not None
    assert s._process_state(None) is None
