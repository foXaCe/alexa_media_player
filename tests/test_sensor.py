"""Tests for sensor module.

Tests the sensor functionality using pytest-homeassistant-custom-component.
"""

import datetime
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from homeassistant.const import CONF_EMAIL, UnitOfTemperature
from homeassistant.exceptions import ConfigEntryNotReady, NoEntitySpecifiedError
from homeassistant.util import dt
import pytest

from custom_components.alexa_media.const import DATA_ALEXAMEDIA
from custom_components.alexa_media.sensor import (
    AirQualitySensor,
    AlarmSensor,
    AlexaMediaNotificationSensor,
    ReminderSensor,
    TemperatureSensor,
    TimerSensor,
    async_setup_entry,
    async_setup_platform,
    create_air_quality_sensors,
    create_temperature_sensors,
)

UTC = datetime.UTC

_ADD = "custom_components.alexa_media.sensor.add_devices"
_TEMP = "custom_components.alexa_media.sensor.parse_temperature_from_coordinator"
_AQ = "custom_components.alexa_media.sensor.parse_air_quality_from_coordinator"
_TRACK = "custom_components.alexa_media.sensor.async_track_point_in_utc_time"
_DISPATCH = "custom_components.alexa_media.sensor.async_dispatcher_connect"
_HTTP2 = "custom_components.alexa_media.sensor.is_http2_enabled"
_NOW = "custom_components.alexa_media.sensor.dt.now"
_EMAIL = "test@example.com"


class TestUpdateRecurringAlarm:
    """Test the _update_recurring_alarm method of AlexaMediaNotificationSensor.

    This class tests the fix for a critical bug where alarm.isoweekday was used
    instead of alarm.isoweekday() - missing the parentheses to actually call the
    method. Without the parentheses, the condition would compare a method object
    to integers, which would always be True, causing incorrect alarm scheduling.
    """

    def test_isoweekday_method_is_called_correctly(self) -> None:
        """Test that isoweekday() is called as a method, not accessed as attribute.

        This is a regression test for a bug where alarm.isoweekday was used instead
        of alarm.isoweekday(). Without the parentheses, a method object would be
        compared to integers in the recurrence set, which would never match,
        causing the while loop to run indefinitely or produce wrong results.

        The bug would manifest when:
        - An alarm is set to ON
        - The alarm has a recurring pattern (e.g., "every Monday")
        - The current alarm time is in the past
        - The current alarm day doesn't match the recurrence pattern

        With the bug, the condition `alarm.isoweekday not in recurrence` would
        always be True (method object never equals an integer), potentially
        causing infinite loops or incorrect alarm times.
        """
        # Create a minimal mock for the sensor
        sensor = object.__new__(AlexaMediaNotificationSensor)
        sensor._sensor_property = "alarmTime"

        # Create a datetime that is a Wednesday (isoweekday() == 3)
        # and set it in the past so the while loop condition is met
        wednesday_in_past = datetime.datetime(2024, 1, 3, 8, 0, 0)  # Wednesday
        assert wednesday_in_past.isoweekday() == 3  # Verify it's Wednesday

        # Create recurrence that only allows Fridays (isoweekday 5)
        # This means the alarm should advance to the next Friday
        recurrence_fridays_only = {5}

        # Create the alarm notification data
        value = (
            "alarm_id",
            {
                "status": "ON",
                "alarmTime": wednesday_in_past,
                "type": "Alarm",
                "recurringPattern": "XXXX-WXX-5",  # Every Friday
            },
        )

        # Mock dt.now() to return a time after the alarm
        # so the condition `alarm < dt.now()` is True
        future_time = datetime.datetime(2024, 1, 10, 8, 0, 0)

        with (
            patch(
                "custom_components.alexa_media.sensor.dt.now", return_value=future_time
            ),
            patch(
                "custom_components.alexa_media.sensor.RECURRING_PATTERN_ISO_SET",
                {"XXXX-WXX-5": recurrence_fridays_only},
            ),
        ):
            result = sensor._update_recurring_alarm(value)

        # The alarm should have been advanced from Wednesday (Jan 3)
        # to Friday (Jan 5) since only Fridays are in the recurrence
        result_alarm = result[1]["alarmTime"]

        # With the fix: isoweekday() returns 3, which is not in {5},
        # so days are added until isoweekday() returns 5 (Friday)
        assert result_alarm.isoweekday() == 5, (
            f"Alarm should be on Friday (isoweekday 5), "
            f"but got isoweekday {result_alarm.isoweekday()}"
        )

        # Verify the alarm moved forward (not backward)
        assert result_alarm >= wednesday_in_past

    def test_recurring_alarm_advances_to_correct_weekday(self) -> None:
        """Test that a recurring alarm advances to the correct weekday."""
        sensor = object.__new__(AlexaMediaNotificationSensor)
        sensor._sensor_property = "alarmTime"

        # Monday January 1, 2024
        monday = datetime.datetime(2024, 1, 1, 8, 0, 0)
        assert monday.isoweekday() == 1

        # Recurrence only on weekends (Saturday=6, Sunday=7)
        weekend_recurrence = {6, 7}

        value = (
            "alarm_id",
            {
                "status": "ON",
                "alarmTime": monday,
                "type": "Alarm",
                "recurringPattern": "XXXX-WE",  # Weekends
            },
        )

        future_time = datetime.datetime(2024, 1, 10, 8, 0, 0)

        with (
            patch(
                "custom_components.alexa_media.sensor.dt.now", return_value=future_time
            ),
            patch(
                "custom_components.alexa_media.sensor.RECURRING_PATTERN_ISO_SET",
                {"XXXX-WE": weekend_recurrence},
            ),
        ):
            result = sensor._update_recurring_alarm(value)

        result_alarm = result[1]["alarmTime"]

        # Should advance to Saturday (Jan 6, 2024)
        assert result_alarm.isoweekday() in {
            6,
            7,
        }, f"Alarm should be on weekend, but got isoweekday {result_alarm.isoweekday()}"
        assert result_alarm == datetime.datetime(2024, 1, 6, 8, 0, 0)

    def test_alarm_on_correct_day_not_modified(self) -> None:
        """Test that an alarm already on a correct day is not modified."""
        sensor = object.__new__(AlexaMediaNotificationSensor)
        sensor._sensor_property = "alarmTime"

        # Friday January 5, 2024
        friday = datetime.datetime(2024, 1, 5, 8, 0, 0)
        assert friday.isoweekday() == 5

        # Recurrence includes Friday
        recurrence_with_friday = {5}

        value = (
            "alarm_id",
            {
                "status": "ON",
                "alarmTime": friday,
                "type": "Alarm",
                "recurringPattern": "XXXX-WXX-5",
            },
        )

        # Even with future time, alarm should not advance if it's already on correct day
        # Note: the loop only runs if alarm < dt.now(), so if alarm is in the past
        # but on correct day, it won't advance
        past_time = datetime.datetime(2024, 1, 4, 8, 0, 0)  # Thursday before alarm

        with (
            patch(
                "custom_components.alexa_media.sensor.dt.now", return_value=past_time
            ),
            patch(
                "custom_components.alexa_media.sensor.RECURRING_PATTERN_ISO_SET",
                {"XXXX-WXX-5": recurrence_with_friday},
            ),
        ):
            result = sensor._update_recurring_alarm(value)

        # Alarm should not be modified since it's in the future relative to now
        assert result[1]["alarmTime"] == friday

    def test_alarm_off_not_advanced(self) -> None:
        """Test that an alarm with status OFF is not advanced."""
        sensor = object.__new__(AlexaMediaNotificationSensor)
        sensor._sensor_property = "alarmTime"

        wednesday = datetime.datetime(2024, 1, 3, 8, 0, 0)

        value = (
            "alarm_id",
            {
                "status": "OFF",  # Alarm is OFF
                "alarmTime": wednesday,
                "type": "Alarm",
                "recurringPattern": "XXXX-WXX-5",
            },
        )

        future_time = datetime.datetime(2024, 1, 10, 8, 0, 0)

        with (
            patch(
                "custom_components.alexa_media.sensor.dt.now", return_value=future_time
            ),
            patch(
                "custom_components.alexa_media.sensor.RECURRING_PATTERN_ISO_SET",
                {"XXXX-WXX-5": {5}},
            ),
        ):
            result = sensor._update_recurring_alarm(value)

        # Alarm should NOT be advanced since status is OFF
        assert result[1]["alarmTime"] == wednesday

    def test_alarm_without_recurrence_not_modified(self) -> None:
        """Test that an alarm without recurring pattern is not modified."""
        sensor = object.__new__(AlexaMediaNotificationSensor)
        sensor._sensor_property = "alarmTime"

        wednesday = datetime.datetime(2024, 1, 3, 8, 0, 0)

        value = (
            "alarm_id",
            {
                "status": "ON",
                "alarmTime": wednesday,
                "type": "Alarm",
                # No recurringPattern
            },
        )

        future_time = datetime.datetime(2024, 1, 10, 8, 0, 0)

        with patch(
            "custom_components.alexa_media.sensor.dt.now", return_value=future_time
        ):
            result = sensor._update_recurring_alarm(value)

        # Alarm should NOT be advanced since there's no recurrence pattern
        assert result[1]["alarmTime"] == wednesday

    def test_reminder_type_handled(self) -> None:
        """Test that reminder type alarms are handled correctly."""
        sensor = object.__new__(AlexaMediaNotificationSensor)
        sensor._sensor_property = "alarmTime"  # Reminders also use alarmTime

        wednesday = datetime.datetime(2024, 1, 3, 8, 0, 0)

        value = (
            "reminder_id",
            {
                "status": "ON",
                "alarmTime": wednesday,
                "type": "Reminder",
                "recurringPattern": "XXXX-WXX-5",
            },
        )

        future_time = datetime.datetime(2024, 1, 10, 8, 0, 0)

        with (
            patch(
                "custom_components.alexa_media.sensor.dt.now", return_value=future_time
            ),
            patch(
                "custom_components.alexa_media.sensor.RECURRING_PATTERN_ISO_SET",
                {"XXXX-WXX-5": {5}},
            ),
        ):
            result = sensor._update_recurring_alarm(value)

        result_alarm = result[1]["alarmTime"]
        # Reminders should also be advanced correctly
        assert result_alarm.isoweekday() == 5


# --------------------------------------------------------------------------- #
# Shared helpers for entity-level tests
# --------------------------------------------------------------------------- #


def _client(serial="SN1", timezone="UTC"):
    """A media-player-like client used to build notification sensors."""
    client = MagicMock()
    client.unique_id = serial
    client.device_serial_number = serial
    client.name = "Echo"
    client.assumed_state = False
    client.available = True
    client._timezone = timezone
    return client


def _alarm(n_json=None, account=_EMAIL, debug=False, timezone="UTC", serial="SN1"):
    return AlarmSensor(_client(serial, timezone), n_json or {}, account, debug=debug)


def _timer(n_json=None, account=_EMAIL, debug=False):
    return TimerSensor(_client(), n_json or {}, account, debug=debug)


def _reminder(n_json=None, account=_EMAIL, debug=False):
    return ReminderSensor(_client(), n_json or {}, account, debug=debug)


def _media_player(serial="SN1"):
    mp = MagicMock()
    mp.unique_id = serial
    mp.device_serial_number = serial
    mp.assumed_state = False
    mp.available = True
    mp._timezone = "UTC"
    return mp


def _setup_account_dict(
    capabilities=("TIMERS_AND_ALARMS", "REMINDERS"), notifications=None
):
    serial = "SN1"
    return {
        "coordinator": MagicMock(),
        "devices": {
            "media_player": {serial: {"capabilities": list(capabilities)}},
        },
        "entities": {
            "media_player": {serial: _media_player(serial)},
            "sensor": {},
        },
        "notifications": notifications if notifications is not None else {},
    }


def _hass_with(account, account_dict):
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": {account: account_dict}}}
    return hass


# --------------------------------------------------------------------------- #
# async_setup_platform / async_setup_entry
# --------------------------------------------------------------------------- #


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_platform_creates_all_notification_sensors(mock_add):
    mock_add.return_value = True
    account = "a@example.com"
    account_dict = _setup_account_dict()
    hass = _hass_with(account, account_dict)
    result = await async_setup_platform(hass, {CONF_EMAIL: account}, MagicMock())
    assert result is True
    created = account_dict["entities"]["sensor"]["SN1"]
    assert set(created) == {"Alarm", "Timer", "Reminder"}
    mock_add.assert_awaited_once()


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_platform_timers_only_capability(mock_add):
    mock_add.return_value = True
    account = "a@example.com"
    account_dict = _setup_account_dict(capabilities=("TIMERS_AND_ALARMS",))
    hass = _hass_with(account, account_dict)
    await async_setup_platform(hass, {CONF_EMAIL: account}, MagicMock())
    assert set(account_dict["entities"]["sensor"]["SN1"]) == {"Alarm", "Timer"}


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_platform_reminders_only_capability(mock_add):
    mock_add.return_value = True
    account = "a@example.com"
    account_dict = _setup_account_dict(capabilities=("REMINDERS",))
    hass = _hass_with(account, account_dict)
    await async_setup_platform(hass, {CONF_EMAIL: account}, MagicMock())
    assert set(account_dict["entities"]["sensor"]["SN1"]) == {"Reminder"}


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_platform_no_capabilities_creates_nothing(mock_add):
    mock_add.return_value = True
    account = "a@example.com"
    account_dict = _setup_account_dict(capabilities=())
    hass = _hass_with(account, account_dict)
    await async_setup_platform(hass, {CONF_EMAIL: account}, MagicMock())
    assert account_dict["entities"]["sensor"]["SN1"] == {}


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_platform_uses_notifications_payload(mock_add):
    """The per-type notification dict is passed into each sensor."""
    mock_add.return_value = True
    account = "a@example.com"
    notifications = {"SN1": {"Alarm": {"al1": {"status": "ON"}}}}
    account_dict = _setup_account_dict(notifications=notifications)
    hass = _hass_with(account, account_dict)
    await async_setup_platform(hass, {CONF_EMAIL: account}, MagicMock())
    alarm = account_dict["entities"]["sensor"]["SN1"]["Alarm"]
    assert alarm._n_dict == {"al1": {"status": "ON"}}


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_platform_skips_already_added(mock_add):
    mock_add.return_value = True
    account = "a@example.com"
    account_dict = _setup_account_dict()
    existing = MagicMock()
    account_dict["entities"]["sensor"] = {"SN1": {"Alarm": existing}}
    hass = _hass_with(account, account_dict)
    result = await async_setup_platform(hass, {CONF_EMAIL: account}, MagicMock())
    assert result is True
    # The pre-existing entity is untouched (else-branch path)
    assert account_dict["entities"]["sensor"]["SN1"]["Alarm"] is existing


async def test_setup_platform_media_player_not_loaded_raises():
    account = "a@example.com"
    account_dict = _setup_account_dict()
    account_dict["entities"]["media_player"] = {}  # SN1 missing
    hass = _hass_with(account, account_dict)
    with pytest.raises(ConfigEntryNotReady):
        await async_setup_platform(hass, {CONF_EMAIL: account}, MagicMock())


async def test_setup_platform_without_account_raises():
    hass = _hass_with("x", {})
    with pytest.raises(ConfigEntryNotReady):
        await async_setup_platform(hass, {}, MagicMock(), discovery_info=None)


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_platform_builds_temperature_and_air_quality(mock_add):
    mock_add.return_value = True
    account = "a@example.com"
    account_dict = _setup_account_dict()
    account_dict["devices"]["temperature"] = [
        {"id": "t1", "name": "Echo Temp", "device_serial": "SN1"}
    ]
    account_dict["devices"]["aiaqm"] = [
        {
            "id": "a1",
            "name": "AQM",
            "device_serial": "HW1",
            "sensors": [
                {
                    "sensorType": "Alexa.AirQuality.Humidity",
                    "instance": "1",
                    "unit": "Alexa.Unit.Percent",
                }
            ],
        }
    ]
    hass = _hass_with(account, account_dict)
    with (
        patch(_TEMP, return_value={"value": 20, "scale": "CELSIUS"}),
        patch(_AQ, return_value=42),
    ):
        result = await async_setup_platform(
            hass, {CONF_EMAIL: account, "debug": True}, MagicMock()
        )
    assert result is True
    # Both the temperature and AQM buckets were populated
    assert "Temperature" in account_dict["entities"]["sensor"]["SN1"]
    assert "Air_Quality" in account_dict["entities"]["sensor"]["HW1"]
    # add_devices received notification + temperature + air-quality entities
    added = mock_add.await_args.args[1]
    assert len(added) >= 5


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_entry_delegates_to_platform(mock_add):
    mock_add.return_value = True
    account = "a@example.com"
    account_dict = _setup_account_dict()
    hass = _hass_with(account, account_dict)
    entry = MagicMock()
    entry.data = {CONF_EMAIL: account}
    result = await async_setup_entry(hass, entry, MagicMock())
    assert result is True


# --------------------------------------------------------------------------- #
# create_temperature_sensors
# --------------------------------------------------------------------------- #


async def test_create_temperature_sensors_aiaqm_and_echo():
    mp = MagicMock()
    mp.device_info = {"identifiers": {("alexa_media", "SN1")}}
    account_dict = {
        "coordinator": MagicMock(),
        "entities": {"media_player": {"SN1": mp}, "sensor": {}},
    }
    temps = [
        {"id": "t1", "name": "AQ Temp", "device_serial": "HW1", "is_aiaqm": True},
        {"id": "t2", "name": "Echo Temp", "device_serial": "SN1"},
    ]
    with patch(_TEMP, return_value={"value": 20, "scale": "CELSIUS"}):
        devices = await create_temperature_sensors(account_dict, temps, debug=True)
    assert len(devices) == 2
    # AIAQM temperature exposes a rich HA device
    assert devices[0].device_info["serial_number"] == "HW1"
    assert devices[0].device_info["model"] == "Indoor Air Quality Monitor"
    # Echo-attached temperature binds to the looked-up identifier only
    assert devices[1].device_info["identifiers"] == {("alexa_media", "SN1")}
    assert account_dict["entities"]["sensor"]["HW1"]["Temperature"] is devices[0]
    assert account_dict["entities"]["sensor"]["SN1"]["Temperature"] is devices[1]


# --------------------------------------------------------------------------- #
# create_air_quality_sensors
# --------------------------------------------------------------------------- #


async def test_create_air_quality_sensors_filters_invalid_subsensors():
    account_dict = {"coordinator": MagicMock(), "entities": {"sensor": {}}}
    entities = [
        {"id": "a0", "name": "NoSensors"},  # missing 'sensors' -> skip
        {"id": "a1", "name": "BadSensors", "sensors": "nope"},  # not a list -> skip
        {
            "id": "a2",
            "name": "NoSerial",
            "sensors": [
                {
                    "sensorType": "Alexa.AirQuality.Humidity",
                    "instance": "1",
                    "unit": "",
                }
            ],
        },  # missing device_serial -> skip
        {
            "id": "a3",
            "name": "Good",
            "device_serial": "HW1",
            "sensors": [
                {
                    "sensorType": "Alexa.AirQuality.Humidity",
                    "instance": "1",
                    "unit": "Alexa.Unit.Percent",
                },
                {
                    "sensorType": "Alexa.AirQuality.Humidity",  # duplicate type -> skip
                    "instance": "2",
                    "unit": "Alexa.Unit.Percent",
                },
                {"sensorType": None, "instance": "3", "unit": ""},  # no type -> skip
                {
                    "sensorType": "Alexa.AirQuality.ParticulateMatter",
                    "instance": None,  # no instance -> skip
                    "unit": "",
                },
                {
                    "sensorType": "Alexa.AirQuality.CarbonMonoxide",
                    "instance": "5",
                    "unit": "Alexa.Unit.PartsPerMillion",
                },
            ],
        },
    ]
    with patch(_AQ, return_value=42):
        devices = await create_air_quality_sensors(account_dict, entities, debug=True)
    # Only Humidity(inst1) and CarbonMonoxide(inst5) survive
    assert len(devices) == 2
    bucket = account_dict["entities"]["sensor"]["HW1"]["Air_Quality"]
    assert set(bucket) == {d.unique_id for d in devices}


# --------------------------------------------------------------------------- #
# TemperatureSensor / AirQualitySensor small branches
# --------------------------------------------------------------------------- #


def test_temperature_value_and_scale_debug_branches():
    with patch(_TEMP, return_value={"value": 1, "scale": "CELSIUS"}):
        s = TemperatureSensor(MagicMock(), "e", "N", ("alexa_media", "SN1"), debug=True)
    assert s._get_temperature_value({"value": 7}) == 7
    assert (
        s._get_temperature_scale({"scale": "FAHRENHEIT"})
        == UnitOfTemperature.FAHRENHEIT
    )


def test_air_quality_sensor_without_device_ident():
    with patch(_AQ, return_value=1):
        s = AirQualitySensor(
            MagicMock(),
            "e",
            "N",
            None,  # no device_ident -> device_info None
            "Alexa.AirQuality.Humidity",
            "inst",
            "Alexa.Unit.Percent",
        )
    assert s._attr_device_info is None


# --------------------------------------------------------------------------- #
# AlexaMediaNotificationSensor: small helpers / properties
# --------------------------------------------------------------------------- #


def test_unit_of_measurement_translation_key_returns_none():
    s = _alarm()
    if hasattr(type(s), "_unit_of_measurement_translation_key"):
        assert s._unit_of_measurement_translation_key is None


def test_coerce_datetime_unhandled_type_returns_none():
    s = _alarm()
    assert s._coerce_datetime([]) is None


def test_normalize_alarm_snooze_non_dict_item_passthrough():
    s = _alarm()  # _type == "Alarm"
    value = ("id", ["not-a-dict"])
    assert s._normalize_alarm_snooze_state(value) is value


def test_select_next_alarm_debug_logs_skipped_past():
    s = _alarm(debug=True)
    now = dt.now()
    past = now - datetime.timedelta(hours=1)
    s._active = [("id1", {"id": "id1", "date_time": past.isoformat(), "status": "ON"})]
    result = s._select_next_alarm(now)
    assert result["id"] == "id1"


def test_should_poll_reflects_http2():
    s = _alarm()
    s.hass = MagicMock()
    with patch(_HTTP2, return_value=True):
        assert s.should_poll is False
    with patch(_HTTP2, return_value=False):
        assert s.should_poll is True


def test_recurrence_property():
    s = _alarm()
    s._next = None
    assert s.recurrence is None
    s._next = {"recurringPattern": "P1D"}
    assert s.recurrence == "Every day"


def test_process_state_base_alarm():
    s = _alarm()
    aware = dt.now()
    assert s._process_state({"date_time": aware}) is not None
    assert s._process_state(None) is None


# --------------------------------------------------------------------------- #
# _fix_alarm_date_time
# --------------------------------------------------------------------------- #


def test_fix_alarm_date_time_non_date_time_passthrough():
    s = _timer()  # _sensor_property != "date_time"
    value = ("t1", {"remainingTime": 5000})
    assert s._fix_alarm_date_time(value) is value


def test_fix_alarm_date_time_parses_string():
    s = _alarm(timezone="UTC")
    value = ("a1", {"date_time": "2024-06-01T08:00:00", "alarmTime": 1717228800000})
    result = s._fix_alarm_date_time(value)
    assert isinstance(result[1]["date_time"], datetime.datetime)
    assert result[1]["date_time"].tzinfo is not None


def test_fix_alarm_date_time_old_format_fallback():
    s = _alarm(timezone="UTC")
    value = ("a1", {"date_time": "garbage", "alarmTime": 1717228800000})
    with patch(
        "custom_components.alexa_media.sensor.dt.parse_datetime", return_value=None
    ):
        result = s._fix_alarm_date_time(value)
    # Falls back to epoch alarmTime conversion
    assert isinstance(result[1]["date_time"], datetime.datetime)


def test_fix_alarm_date_time_missing_timezone_warns_and_keeps_value():
    s = _alarm(timezone=None)
    value = ("a1", {"date_time": "2024-06-01T08:00:00", "alarmTime": 1717228800000})
    with patch(
        "custom_components.alexa_media.sensor.dt.get_time_zone", return_value=None
    ):
        result = s._fix_alarm_date_time(value)
    # Neither branch updated it -> original string preserved
    assert result[1]["date_time"] == "2024-06-01T08:00:00"


# --------------------------------------------------------------------------- #
# _update_recurring_alarm: reminder / rRuleData / debug branches
# --------------------------------------------------------------------------- #


def test_update_recurring_reminder_epoch_roundtrip():
    s = _reminder()  # _sensor_property == "alarmTime"
    value = ("r1", {"alarmTime": 1717228800500, "status": "ON", "type": "Reminder"})
    with patch(_NOW, return_value=datetime.datetime(2024, 6, 1, tzinfo=UTC)):
        result = s._update_recurring_alarm(value)
    # Converted to local for processing, then back to an epoch-ms number
    assert isinstance(result[1]["alarmTime"], (int, float))


def test_update_recurring_reminder_snoozed_epoch_roundtrip():
    s = _reminder()
    value = (
        "r1",
        {"alarmTime": 1717228800500, "status": "SNOOZED", "type": "Reminder"},
    )
    result = s._update_recurring_alarm(value)
    assert isinstance(result[1]["alarmTime"], (int, float))


def test_update_recurring_rrule_next_trigger_times():
    s = _alarm()
    trigger = datetime.datetime(2024, 6, 10, 8, 0, tzinfo=UTC)
    value = (
        "a1",
        {
            "date_time": datetime.datetime(2024, 6, 1, 8, 0, tzinfo=UTC),
            "status": "ON",
            "type": "Alarm",
            "rRuleData": {"nextTriggerTimes": [trigger]},
        },
    )
    with patch(_NOW, return_value=datetime.datetime(2024, 6, 5, tzinfo=UTC)):
        result = s._update_recurring_alarm(value)
    assert result[1]["date_time"] == trigger


def test_update_recurring_rrule_by_weekdays():
    s = _alarm()
    monday = datetime.datetime(2024, 6, 3, 8, 0, tzinfo=UTC)  # isoweekday 1
    value = (
        "a1",
        {
            "date_time": monday,
            "status": "ON",
            "type": "Alarm",
            "rRuleData": {"byWeekDays": ["WE"]},  # Wednesday -> 3
        },
    )
    with patch(_NOW, return_value=datetime.datetime(2024, 6, 10, tzinfo=UTC)):
        result = s._update_recurring_alarm(value)
    assert result[1]["date_time"].isoweekday() == 3


def test_update_recurring_debug_branch():
    s = _alarm(debug=True)
    value = (
        "a1",
        {
            "date_time": datetime.datetime(2024, 6, 1, tzinfo=UTC),
            "status": "OFF",
            "type": "Alarm",
        },
    )
    assert s._update_recurring_alarm(value) is value


# --------------------------------------------------------------------------- #
# _process_raw_notifications
# --------------------------------------------------------------------------- #


def test_process_raw_notifications_schedules_future_alarm():
    s = _alarm()
    s.hass = MagicMock()
    future = dt.now() + datetime.timedelta(hours=2)
    s._n_dict = {
        "a1": {
            "id": "a1",
            "status": "ON",
            "date_time": future,
            "version": "1",
            "type": "Alarm",
        }
    }
    with patch(_TRACK) as track:
        s._process_raw_notifications()
    assert s._next["id"] == "a1"
    assert s._attr_native_value is not None
    assert s._status == "ON"
    assert s._amz_id == "a1"
    track.assert_called_once()


def test_process_raw_notifications_debug_with_and_without_data():
    s = _alarm(debug=True)
    s.hass = MagicMock()
    future = dt.now() + datetime.timedelta(hours=1)
    s._n_dict = {
        "a1": {
            "id": "a1",
            "status": "ON",
            "date_time": future,
            "version": "1",
            "type": "Alarm",
            "snoozedToTime": None,
            "lastUpdatedDate": 111,
        }
    }
    with patch(_TRACK):
        s._process_raw_notifications()
    assert s._next["id"] == "a1"

    # Now an empty dict exercises the "no notifications" debug paths
    s._n_dict = {}
    with patch(_TRACK):
        s._process_raw_notifications()
    assert s._all == []
    assert s._active == []
    assert s._next is None


def test_process_raw_notifications_cancels_tracker_when_no_active():
    s = _alarm()
    s.hass = MagicMock()
    old_tracker = MagicMock()
    s._tracker = old_tracker
    s._n_dict = {
        "a1": {
            "id": "a1",
            "status": "OFF",
            "date_time": dt.now() + datetime.timedelta(hours=1),
            "version": "1",
            "type": "Alarm",
        }
    }
    with patch(_TRACK) as track:
        s._process_raw_notifications()
    assert s._next is None
    assert s._attr_native_value is None
    assert s._status == "OFF"
    old_tracker.assert_called_once()  # stale event cancelled
    track.assert_not_called()


def test_process_raw_notifications_snoozed_not_scheduled():
    s = _alarm()
    s.hass = MagicMock()
    now = dt.now()
    snooze = now + datetime.timedelta(hours=1)
    s._n_dict = {
        "a1": {
            "id": "a1",
            "status": "ON",
            "date_time": now - datetime.timedelta(minutes=5),
            "snoozedToTime": snooze,
            "version": "1",
            "type": "Alarm",
        }
    }
    with patch(_TRACK) as track:
        s._process_raw_notifications()
    assert s._status == "SNOOZED"
    track.assert_not_called()


def test_process_raw_notifications_detects_dismissal():
    s = _alarm()
    s.hass = MagicMock()
    s._amz_id = "a1"
    s._status = "ON"
    s._version = "1"
    s._n_dict = {
        "a1": {
            "id": "a1",
            "status": "OFF",
            "date_time": dt.now() - datetime.timedelta(hours=1),
            "version": "2",
            "type": "Alarm",
        }
    }
    with patch(_TRACK):
        s._process_raw_notifications()
    assert s._dismissed is not None


# --------------------------------------------------------------------------- #
# _trigger_event
# --------------------------------------------------------------------------- #


def test_trigger_event_fires_bus_event():
    s = _alarm()
    s.hass = MagicMock()
    s.entity_id = "sensor.next_alarm"
    s._active = [("a1", {"id": "a1"})]
    with patch.object(
        AlarmSensor, "name", new_callable=PropertyMock, return_value="next Alarm"
    ):
        s._trigger_event(dt.now())
    s.hass.bus.fire.assert_called_once()
    assert s.hass.bus.fire.call_args.args[0] == "alexa_media_notification_event"


# --------------------------------------------------------------------------- #
# _handle_event
# --------------------------------------------------------------------------- #


def test_handle_event_dispatch_paths():
    s = _alarm()
    s.schedule_update_ha_state = MagicMock()

    s._handle_event({"notifications_refreshed": True})
    s.schedule_update_ha_state.assert_called_once_with(True)

    s.schedule_update_ha_state.reset_mock()
    s._handle_event(
        {"notification_update": {"dopplerId": {"deviceSerialNumber": "SN1"}}}
    )
    s.schedule_update_ha_state.assert_called_once_with(True)

    s.schedule_update_ha_state.reset_mock()
    s._handle_event(
        {"notification_update": {"dopplerId": {"deviceSerialNumber": "OTHER"}}}
    )
    s.schedule_update_ha_state.assert_not_called()

    s.schedule_update_ha_state.reset_mock()
    s._handle_event({"push_activity": {"key": {"serialNumber": "SN1"}}})
    s.schedule_update_ha_state.assert_called_once_with(True)

    s.schedule_update_ha_state.reset_mock()
    s._handle_event({"push_activity": {"key": {"serialNumber": "OTHER"}}})
    s.schedule_update_ha_state.assert_not_called()


# --------------------------------------------------------------------------- #
# async_update / async_added_to_hass / async_will_remove_from_hass
# --------------------------------------------------------------------------- #


async def test_async_update_no_notifications():
    account = "t@e.com"
    s = _alarm(account=account)
    s.hass = MagicMock()
    s.hass.data = {DATA_ALEXAMEDIA: {"accounts": {account: {"notifications": None}}}}
    # The startup-race NoEntitySpecifiedError must be swallowed.
    s.schedule_update_ha_state = MagicMock(side_effect=NoEntitySpecifiedError)
    with patch(_TRACK):
        await s.async_update()
    assert s._n_dict is None
    assert s._timestamp is None


async def test_async_update_with_notifications():
    account = "t@e.com"
    s = _alarm(account=account)
    future = dt.now() + datetime.timedelta(hours=1)
    notifications = {
        "process_timestamp": dt.now(),
        "SN1": {
            "Alarm": {
                "a1": {
                    "id": "a1",
                    "status": "ON",
                    "date_time": future,
                    "version": "1",
                    "type": "Alarm",
                }
            }
        },
    }
    s.hass = MagicMock()
    s.hass.data = {
        DATA_ALEXAMEDIA: {"accounts": {account: {"notifications": notifications}}}
    }
    # The startup-race NoEntitySpecifiedError must be swallowed.
    s.schedule_update_ha_state = MagicMock(side_effect=NoEntitySpecifiedError)
    with patch(_TRACK):
        await s.async_update()
    assert s._next["id"] == "a1"
    assert s._timestamp is not None


async def test_async_added_to_hass_registers_listener_and_updates():
    account = "t@e.com"
    s = _alarm(account=account)
    future = dt.now() + datetime.timedelta(hours=1)
    notifications = {
        "process_timestamp": dt.now(),
        "SN1": {
            "Alarm": {
                "a1": {
                    "id": "a1",
                    "status": "ON",
                    "date_time": future,
                    "version": "1",
                    "type": "Alarm",
                }
            }
        },
    }
    s.hass = MagicMock()
    s.hass.data = {
        DATA_ALEXAMEDIA: {"accounts": {account: {"notifications": notifications}}}
    }
    listener = MagicMock()
    with patch(_DISPATCH, return_value=listener) as dispatch, patch(_TRACK):
        await s.async_added_to_hass()
    assert s._listener is listener
    dispatch.assert_called_once()
    assert s._next["id"] == "a1"


async def test_async_will_remove_from_hass_cleans_up():
    s = _alarm()
    s._listener = MagicMock()
    s._tracker = MagicMock()
    await s.async_will_remove_from_hass()
    s._listener.assert_called_once()
    s._tracker.assert_called_once()


# --------------------------------------------------------------------------- #
# extra_state_attributes (base / Timer / Reminder)
# --------------------------------------------------------------------------- #


def test_extra_state_attributes_empty():
    s = _alarm()
    s._status = "OFF"
    attr = s.extra_state_attributes
    assert attr["total_all"] == 0
    assert attr["total_active"] == 0
    assert attr["status"] == "OFF"
    assert attr["recurrence"] is None
    assert "brief" not in attr


def test_extra_state_attributes_with_entries():
    s = _alarm()
    aware = dt.now()
    entry = {
        "id": "a1",
        "alarmLabel": "Wake",
        "status": "ON",
        "date_time": aware,
        "type": "Alarm",
        "version": "1",
        "lastUpdatedDate": 111,
    }
    string_entry = {"id": "a2", "status": "ON", "date_time": "raw", "type": "Alarm"}
    s._all = [("a1", entry), ("a2", string_entry), ("a3", {})]
    s._active = [("a1", entry), ("a3", {})]
    s._next = entry
    s._timestamp = aware
    s._prior_value = entry
    s._status = "ON"
    attr = s.extra_state_attributes
    assert attr["sorted_all"] == [entry, string_entry, {}]
    assert attr["sorted_active"] == [entry, {}]
    assert attr["alarm"] == "Wake"  # _type.lower() label alias
    assert attr["process_timestamp"] is not None
    brief_all = attr["brief"]["all"]
    assert brief_all[0]["label"] == "Wake"
    assert brief_all[1]["date_time"] == "raw"  # non-datetime passthrough
    assert brief_all[2] == {}  # empty entry serialized to {}


def test_timer_extra_state_attributes():
    s = _timer()
    s._next = {"timerLabel": "Pasta", "status": "ON"}
    s._status = "ON"
    attr = s.extra_state_attributes
    assert attr["timer"] == "Pasta"


def test_reminder_extra_state_attributes_includes_sub_label():
    s = _reminder()
    aware = dt.now()
    entry = {
        "id": "r1",
        "reminderLabel": "Call",
        "reminderSubLabel": "Mom",
        "status": "ON",
        "alarmTime": 1717228800000,
        "type": "Reminder",
    }
    s._all = [("r1", entry)]
    s._active = [("r1", entry)]
    s._next = entry
    s._timestamp = aware
    s._status = "ON"
    attr = s.extra_state_attributes
    assert attr["reminder"] == "Call"
    assert attr["reminder_sub_label"] == "Mom"


# --------------------------------------------------------------------------- #
# Remaining branch coverage
# --------------------------------------------------------------------------- #


@patch(_ADD, new_callable=AsyncMock)
async def test_setup_platform_initializes_sensor_bucket(mock_add):
    """entities has no 'sensor' key -> the bucket is created."""
    mock_add.return_value = True
    account = "a@example.com"
    account_dict = _setup_account_dict()
    del account_dict["entities"]["sensor"]
    hass = _hass_with(account, account_dict)
    result = await async_setup_platform(hass, {CONF_EMAIL: account}, MagicMock())
    assert result is True
    assert "sensor" in account_dict["entities"]


def test_process_raw_notifications_timer_selects_first_active():
    """Non-Alarm sensors take the first active notification as 'next'."""
    s = _timer()
    s.hass = MagicMock()
    s._timestamp = dt.now()
    s._n_dict = {
        "t1": {
            "id": "t1",
            "status": "ON",
            "remainingTime": 3600000,
            "version": "1",
            "type": "Timer",
        }
    }
    with patch(_TRACK):
        s._process_raw_notifications()
    assert s._next["id"] == "t1"


def test_handle_event_disabled_returns_early():
    s = _alarm()
    s.schedule_update_ha_state = MagicMock()
    with patch.object(
        type(s), "enabled", new_callable=PropertyMock, return_value=False
    ):
        s._handle_event({"notifications_refreshed": True})
    s.schedule_update_ha_state.assert_not_called()


def test_handle_event_enabled_attribute_error_is_ignored():
    s = _alarm()
    s.schedule_update_ha_state = MagicMock()
    with patch.object(
        type(s), "enabled", new_callable=PropertyMock, side_effect=AttributeError
    ):
        s._handle_event({"notifications_refreshed": True})
    s.schedule_update_ha_state.assert_called_once_with(True)


async def test_async_update_disabled_returns_early():
    s = _alarm()
    s._timestamp = "sentinel"
    with patch.object(
        type(s), "enabled", new_callable=PropertyMock, return_value=False
    ):
        await s.async_update()
    assert s._timestamp == "sentinel"  # returned before touching hass.data


async def test_async_update_enabled_attribute_error_continues():
    account = "t@e.com"
    s = _alarm(account=account)
    s.hass = MagicMock()
    s.hass.data = {DATA_ALEXAMEDIA: {"accounts": {account: {"notifications": None}}}}
    with (
        patch.object(
            type(s), "enabled", new_callable=PropertyMock, side_effect=AttributeError
        ),
        patch(_TRACK),
    ):
        await s.async_update()
    assert s._n_dict is None


async def test_async_added_to_hass_disabled_returns_early():
    s = _alarm()
    with (
        patch.object(type(s), "enabled", new_callable=PropertyMock, return_value=False),
        patch(_DISPATCH) as dispatch,
    ):
        await s.async_added_to_hass()
    dispatch.assert_not_called()
    assert not hasattr(s, "_listener")


async def test_async_added_to_hass_enabled_attribute_error_continues():
    account = "t@e.com"
    s = _alarm(account=account)
    s.hass = MagicMock()
    s.hass.data = {DATA_ALEXAMEDIA: {"accounts": {account: {"notifications": None}}}}
    listener = MagicMock()
    with (
        patch.object(
            type(s), "enabled", new_callable=PropertyMock, side_effect=AttributeError
        ),
        patch(_DISPATCH, return_value=listener),
        patch(_TRACK),
    ):
        await s.async_added_to_hass()
    assert s._listener is listener
