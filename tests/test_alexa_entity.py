"""Test the alexa_entity module utility functions."""

from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, MagicMock, patch

from custom_components.alexa_media.alexa_entity import (
    get_device_bridge,
    get_device_serial,
    get_entity_data,
    get_friendliest_name,
    has_capability,
    is_air_quality_sensor,
    is_alexa_guard,
    is_cap_state_still_acceptable,
    is_contact_sensor,
    is_hue_v1,
    is_known_ha_bridge,
    is_light,
    is_local,
    is_skill,
    is_switch,
    is_temperature_sensor,
    parse_air_quality_from_coordinator,
    parse_alexa_entities,
    parse_brightness_from_coordinator,
    parse_color_from_coordinator,
    parse_color_temp_from_coordinator,
    parse_detection_state_from_coordinator,
    parse_guard_state_from_coordinator,
    parse_power_from_coordinator,
    parse_temperature_from_coordinator,
    parse_value_from_coordinator,
)


def _capability(
    interface_name,
    property_name,
    *,
    retrievable=True,
    proactively_reported=False,
    instance=None,
    **extra,
):
    """Build a minimal capability dict accepted by has_capability()."""
    cap = {
        "interfaceName": interface_name,
        "properties": {
            "retrievable": retrievable,
            "proactivelyReported": proactively_reported,
            "supported": [{"name": property_name}],
        },
    }
    if instance is not None:
        cap["instance"] = instance
    cap.update(extra)
    return cap


def _coordinator(data):
    """Build a MagicMock coordinator exposing ``.data``."""
    coordinator = MagicMock()
    coordinator.data = data
    return coordinator


def _cap_state(namespace, name, value, *, instance=None, time_of_sample=None):
    """Build a coordinator capability-state dict."""
    cap_state = {"namespace": namespace, "name": name, "value": value}
    if instance is not None:
        cap_state["instance"] = instance
    if time_of_sample is not None:
        cap_state["timeOfSample"] = time_of_sample
    return cap_state


class TestHasCapability:
    """Test the has_capability function."""

    def test_has_capability_with_valid_interface(self):
        """Test has_capability returns True when interface and property match."""
        appliance = {
            "capabilities": [
                {
                    "interfaceName": "Alexa.PowerController",
                    "properties": {
                        "retrievable": True,
                        "proactivelyReported": False,
                        "supported": [{"name": "powerState"}],
                    },
                }
            ]
        }

        assert has_capability(appliance, "Alexa.PowerController", "powerState")

    def test_has_capability_with_proactively_reported(self):
        """Test has_capability returns True when proactivelyReported is True."""
        appliance = {
            "capabilities": [
                {
                    "interfaceName": "Alexa.BrightnessController",
                    "properties": {
                        "retrievable": False,
                        "proactivelyReported": True,
                        "supported": [{"name": "brightness"}],
                    },
                }
            ]
        }

        assert has_capability(appliance, "Alexa.BrightnessController", "brightness")

    def test_has_capability_no_matching_interface(self):
        """Test has_capability returns False when interface doesn't match."""
        appliance = {
            "capabilities": [
                {
                    "interfaceName": "Alexa.PowerController",
                    "properties": {
                        "retrievable": True,
                        "proactivelyReported": False,
                        "supported": [{"name": "powerState"}],
                    },
                }
            ]
        }

        assert not has_capability(appliance, "Alexa.BrightnessController", "brightness")

    def test_has_capability_no_matching_property(self):
        """Test has_capability returns False when property doesn't match."""
        appliance = {
            "capabilities": [
                {
                    "interfaceName": "Alexa.PowerController",
                    "properties": {
                        "retrievable": True,
                        "proactivelyReported": False,
                        "supported": [{"name": "powerState"}],
                    },
                }
            ]
        }

        assert not has_capability(appliance, "Alexa.PowerController", "brightness")

    def test_has_capability_no_properties(self):
        """Test has_capability returns False when properties are missing."""
        appliance = {"capabilities": [{"interfaceName": "Alexa.PowerController"}]}

        assert not has_capability(appliance, "Alexa.PowerController", "powerState")

    def test_has_capability_not_retrievable_or_reported(self):
        """Test has_capability returns False when not retrievable or proactively reported."""
        appliance = {
            "capabilities": [
                {
                    "interfaceName": "Alexa.PowerController",
                    "properties": {
                        "retrievable": False,
                        "proactivelyReported": False,
                        "supported": [{"name": "powerState"}],
                    },
                }
            ]
        }

        assert not has_capability(appliance, "Alexa.PowerController", "powerState")

    def test_has_capability_empty_capabilities(self):
        """Test has_capability returns False when capabilities list is empty."""
        appliance = {"capabilities": []}

        assert not has_capability(appliance, "Alexa.PowerController", "powerState")

    def test_has_capability_multiple_properties(self):
        """Test has_capability with multiple supported properties."""
        appliance = {
            "capabilities": [
                {
                    "interfaceName": "Alexa.ColorController",
                    "properties": {
                        "retrievable": True,
                        "proactivelyReported": False,
                        "supported": [
                            {"name": "color"},
                            {"name": "colorTemperatureInKelvin"},
                        ],
                    },
                }
            ]
        }

        assert has_capability(appliance, "Alexa.ColorController", "color")
        assert has_capability(
            appliance, "Alexa.ColorController", "colorTemperatureInKelvin"
        )
        assert not has_capability(appliance, "Alexa.ColorController", "brightness")


class TestIsHueV1:
    """Test the is_hue_v1 function."""

    def test_is_hue_v1_true(self):
        """Test is_hue_v1 returns True for Royal Philips Electronics."""
        appliance = {"manufacturerName": "Royal Philips Electronics"}
        assert is_hue_v1(appliance)

    def test_is_hue_v1_false_different_manufacturer(self):
        """Test is_hue_v1 returns False for different manufacturer."""
        appliance = {"manufacturerName": "Amazon"}
        assert not is_hue_v1(appliance)

    def test_is_hue_v1_false_no_manufacturer(self):
        """Test is_hue_v1 returns False when manufacturerName is missing."""
        appliance = {}
        assert not is_hue_v1(appliance)

    def test_is_hue_v1_false_none_manufacturer(self):
        """Test is_hue_v1 returns False when manufacturerName is None."""
        appliance = {"manufacturerName": None}
        assert not is_hue_v1(appliance)


class TestIsSkill:
    """Test the is_skill function."""

    def test_is_skill_true(self):
        """Test is_skill returns True when namespace is SKILL."""
        appliance = {"driverIdentity": {"namespace": "SKILL"}}
        assert is_skill(appliance)

    def test_is_skill_false_different_namespace(self):
        """Test is_skill returns False for different namespace."""
        appliance = {"driverIdentity": {"namespace": "OTHER"}}
        assert not is_skill(appliance)

    def test_is_skill_false_no_driver_identity(self):
        """Test is_skill returns False when driverIdentity is missing."""
        appliance = {}
        assert not is_skill(appliance)

    def test_is_skill_false_no_namespace(self):
        """Test is_skill returns False when namespace is missing."""
        appliance = {"driverIdentity": {}}
        assert not is_skill(appliance)

    def test_is_skill_false_empty_namespace(self):
        """Test is_skill returns False when namespace is empty."""
        appliance = {"driverIdentity": {"namespace": ""}}
        assert not is_skill(appliance)


class TestIsKnownHaBridge:
    """Test the is_known_ha_bridge function."""

    def test_is_known_ha_bridge_none(self):
        """Test is_known_ha_bridge returns False for None input."""
        assert not is_known_ha_bridge(None)

    def test_is_known_ha_bridge_t0bst4r(self):
        """Test is_known_ha_bridge returns True for t0bst4r manufacturer."""
        appliance = {"manufacturerName": "t0bst4r"}
        assert is_known_ha_bridge(appliance)

    def test_is_known_ha_bridge_matterbridge(self):
        """Test is_known_ha_bridge returns True for Matterbridge manufacturer."""
        appliance = {"manufacturerName": "Matterbridge"}
        assert is_known_ha_bridge(appliance)

    def test_is_known_ha_bridge_false(self):
        """Test is_known_ha_bridge returns False for unknown manufacturer."""
        appliance = {"manufacturerName": "Amazon"}
        assert not is_known_ha_bridge(appliance)

    def test_is_known_ha_bridge_no_manufacturer(self):
        """Test is_known_ha_bridge returns False when manufacturerName is missing."""
        appliance = {}
        assert not is_known_ha_bridge(appliance)


class TestIsLocal:
    """Test the is_local function."""

    def test_is_local_with_connected_via(self):
        """Test is_local returns True when connectedVia is present."""
        appliance = {"connectedVia": "Echo Dot"}
        assert is_local(appliance)

    def test_is_local_voice_enabled_not_skill(self):
        """Test is_local returns True for voice enabled device that's not a skill."""
        appliance = {
            "applianceTypes": ["ALEXA_VOICE_ENABLED"],
            "driverIdentity": {"namespace": "OTHER"},
        }
        assert is_local(appliance)

    def test_is_local_voice_enabled_skill(self):
        """Test is_local returns False for voice enabled device that's a skill."""
        appliance = {
            "applianceTypes": ["ALEXA_VOICE_ENABLED"],
            "driverIdentity": {"namespace": "SKILL"},
        }
        assert not is_local(appliance)

    def test_is_local_ledvance_not_skill(self):
        """Test is_local returns True for Ledvance device that's not a skill."""
        appliance = {
            "manufacturerName": "Ledvance",
            "driverIdentity": {"namespace": "OTHER"},
        }
        assert is_local(appliance)

    def test_is_local_sengled_not_skill(self):
        """Test is_local returns True for Sengled device that's not a skill."""
        appliance = {
            "manufacturerName": "Sengled",
            "driverIdentity": {"namespace": "OTHER"},
        }
        assert is_local(appliance)

    def test_is_local_amazon_not_skill(self):
        """Test is_local returns True for Amazon device that's not a skill."""
        appliance = {
            "manufacturerName": "Amazon",
            "driverIdentity": {"namespace": "OTHER"},
        }
        assert is_local(appliance)

    def test_is_local_ledvance_skill(self):
        """Test is_local returns False for Ledvance device that's a skill."""
        appliance = {
            "manufacturerName": "Ledvance",
            "driverIdentity": {"namespace": "SKILL"},
        }
        assert not is_local(appliance)

    def test_is_local_unknown_manufacturer(self):
        """Test is_local returns False for unknown manufacturer without other flags."""
        appliance = {"manufacturerName": "Unknown"}
        # This should return False as the final zigbee pattern check will fail
        result = is_local(appliance)
        assert result is False

    def test_is_local_empty_appliance(self):
        """Test is_local returns False for empty appliance."""
        appliance = {}
        result = is_local(appliance)
        assert result is False

    def test_is_local_zigbee_pattern_match(self):
        """Test is_local returns True for valid zigbee pattern."""
        appliance = {"applianceId": "AAA_SonarCloudService_AB:CD:EF:12:34:56:78:90"}
        result = is_local(appliance)
        assert result is True

    def test_is_local_zigbee_pattern_no_match(self):
        """Test is_local returns False for invalid zigbee pattern."""
        appliance = {"applianceId": "invalid_pattern"}
        result = is_local(appliance)
        assert result is False


class TestIsKnownHaBridgeMatter:
    """Test the Matter-hub branch of is_known_ha_bridge."""

    @staticmethod
    def _matter_hub(interface):
        return {
            "applianceTypes": ["HUB"],
            "driverIdentity": {"namespace": "AAA", "identifier": "SonarCloudService"},
            "capabilities": [{"interfaceName": interface}],
        }

    def test_fabric_management_interface(self):
        """A SonarCloudService HUB with FabricManagement is a known bridge."""
        appliance = self._matter_hub(
            "Alexa.Matter.NodeOperationalCredentials.FabricManagement"
        )
        assert is_known_ha_bridge(appliance)

    def test_commissionable_interface(self):
        """A SonarCloudService HUB with Commissionable is a known bridge."""
        assert is_known_ha_bridge(self._matter_hub("Alexa.Commissionable"))

    def test_hub_without_matter_interface(self):
        """A HUB without a Matter interface is not a known bridge."""
        assert not is_known_ha_bridge(self._matter_hub("Alexa.PowerController"))

    def test_hub_wrong_namespace(self):
        """A HUB whose driver namespace is not AAA is not a known bridge."""
        appliance = self._matter_hub("Alexa.Commissionable")
        appliance["driverIdentity"]["namespace"] = "SKILL"
        assert not is_known_ha_bridge(appliance)

    def test_hub_wrong_identifier(self):
        """A HUB whose driver identifier is not SonarCloudService is not a bridge."""
        appliance = self._matter_hub("Alexa.Commissionable")
        appliance["driverIdentity"]["identifier"] = "Other"
        assert not is_known_ha_bridge(appliance)


class TestIsAlexaGuard:
    """Test the is_alexa_guard function."""

    def test_true(self):
        """REDROCK_GUARD_PANEL with armState capability is a guard."""
        appliance = {
            "modelName": "REDROCK_GUARD_PANEL",
            "capabilities": [_capability("Alexa.SecurityPanelController", "armState")],
        }
        assert is_alexa_guard(appliance)

    def test_wrong_model(self):
        """A non-REDROCK model is not a guard."""
        appliance = {
            "modelName": "SOMETHING_ELSE",
            "capabilities": [_capability("Alexa.SecurityPanelController", "armState")],
        }
        assert not is_alexa_guard(appliance)

    def test_missing_capability(self):
        """REDROCK without the armState capability is not a guard."""
        appliance = {"modelName": "REDROCK_GUARD_PANEL", "capabilities": []}
        assert not is_alexa_guard(appliance)


class TestIsTemperatureSensor:
    """Test the is_temperature_sensor function."""

    def test_true(self):
        """A local temperature sensor (not AIAQM) is a temperature sensor."""
        appliance = {
            "connectedVia": "Echo",
            "friendlyDescription": "Temp Sensor",
            "capabilities": [_capability("Alexa.TemperatureSensor", "temperature")],
        }
        assert is_temperature_sensor(appliance)

    def test_false_not_local(self):
        """A non-local device is not a temperature sensor."""
        appliance = {
            "friendlyDescription": "Temp Sensor",
            "capabilities": [_capability("Alexa.TemperatureSensor", "temperature")],
        }
        assert not is_temperature_sensor(appliance)

    def test_false_aiaqm_description(self):
        """The AIAQM is excluded from the plain temperature sensors."""
        appliance = {
            "connectedVia": "Echo",
            "friendlyDescription": "Amazon Indoor Air Quality Monitor",
            "capabilities": [_capability("Alexa.TemperatureSensor", "temperature")],
        }
        assert not is_temperature_sensor(appliance)


class TestIsAirQualitySensor:
    """Test the is_air_quality_sensor function."""

    def test_true(self):
        """The AIAQM with a RangeController is an air quality sensor."""
        appliance = {
            "friendlyDescription": "Amazon Indoor Air Quality Monitor",
            "applianceTypes": ["AIR_QUALITY_MONITOR"],
            "capabilities": [_capability("Alexa.RangeController", "rangeValue")],
        }
        assert is_air_quality_sensor(appliance)

    def test_false_wrong_description(self):
        """A non-AIAQM friendlyDescription is not an air quality sensor."""
        appliance = {
            "friendlyDescription": "Other",
            "applianceTypes": ["AIR_QUALITY_MONITOR"],
            "capabilities": [_capability("Alexa.RangeController", "rangeValue")],
        }
        assert not is_air_quality_sensor(appliance)

    def test_false_missing_capability(self):
        """An AIAQM-named device without RangeController is not detected."""
        appliance = {
            "friendlyDescription": "Amazon Indoor Air Quality Monitor",
            "applianceTypes": ["AIR_QUALITY_MONITOR"],
            "capabilities": [],
        }
        assert not is_air_quality_sensor(appliance)


class TestIsLight:
    """Test the is_light function."""

    def test_true_light_type(self):
        """A local LIGHT with PowerController is a light."""
        appliance = {
            "connectedVia": "Echo",
            "applianceTypes": ["LIGHT"],
            "capabilities": [_capability("Alexa.PowerController", "powerState")],
        }
        assert is_light(appliance)

    def test_true_smartplug_defined_light(self):
        """A SMARTPLUG defined as a LIGHT is a light."""
        appliance = {
            "connectedVia": "Echo",
            "applianceTypes": ["SMARTPLUG"],
            "customerDefinedDeviceType": "LIGHT",
            "capabilities": [_capability("Alexa.PowerController", "powerState")],
        }
        assert is_light(appliance)

    def test_false_no_power_controller(self):
        """A LIGHT without PowerController is not a light."""
        appliance = {
            "connectedVia": "Echo",
            "applianceTypes": ["LIGHT"],
            "capabilities": [],
        }
        assert not is_light(appliance)

    def test_false_not_local(self):
        """A non-local LIGHT is not a light."""
        appliance = {
            "applianceTypes": ["LIGHT"],
            "capabilities": [_capability("Alexa.PowerController", "powerState")],
        }
        assert not is_light(appliance)


class TestIsContactSensor:
    """Test the is_contact_sensor function."""

    def test_true(self):
        """A local CONTACT_SENSOR with detectionState is a contact sensor."""
        appliance = {
            "connectedVia": "Echo",
            "applianceTypes": ["CONTACT_SENSOR"],
            "capabilities": [_capability("Alexa.ContactSensor", "detectionState")],
        }
        assert is_contact_sensor(appliance)

    def test_false_missing_type(self):
        """A device without CONTACT_SENSOR type is not a contact sensor."""
        appliance = {
            "connectedVia": "Echo",
            "applianceTypes": [],
            "capabilities": [_capability("Alexa.ContactSensor", "detectionState")],
        }
        assert not is_contact_sensor(appliance)


class TestIsSwitch:
    """Test the is_switch function."""

    def test_true_smartplug(self):
        """A local SMARTPLUG with PowerController is a switch."""
        appliance = {
            "connectedVia": "Echo",
            "applianceTypes": ["SMARTPLUG"],
            "capabilities": [_capability("Alexa.PowerController", "powerState")],
        }
        assert is_switch(appliance)

    def test_true_switch(self):
        """A local SWITCH with PowerController is a switch."""
        appliance = {
            "connectedVia": "Echo",
            "applianceTypes": ["SWITCH"],
            "capabilities": [_capability("Alexa.PowerController", "powerState")],
        }
        assert is_switch(appliance)

    def test_false_defined_as_light(self):
        """A SMARTPLUG defined as a LIGHT is not a switch."""
        appliance = {
            "connectedVia": "Echo",
            "applianceTypes": ["SMARTPLUG"],
            "customerDefinedDeviceType": "LIGHT",
            "capabilities": [_capability("Alexa.PowerController", "powerState")],
        }
        assert not is_switch(appliance)


class TestGetFriendliestName:
    """Test the get_friendliest_name function."""

    def test_alias_preferred(self):
        """A non-empty alias friendlyName is preferred over the top-level name."""
        appliance = {
            "friendlyName": "Original",
            "aliases": [{"friendlyName": "Renamed"}],
        }
        assert get_friendliest_name(appliance) == "Renamed"

    def test_falls_back_to_friendly_name(self):
        """An empty aliases list falls back to the top-level friendlyName."""
        appliance = {"friendlyName": "Original", "aliases": []}
        assert get_friendliest_name(appliance) == "Original"

    def test_skips_empty_alias(self):
        """An alias with an empty friendlyName is skipped."""
        appliance = {
            "friendlyName": "Original",
            "aliases": [{"friendlyName": ""}, {"friendlyName": "Good"}],
        }
        assert get_friendliest_name(appliance) == "Good"

    def test_no_aliases_key(self):
        """A missing aliases key falls back to the top-level friendlyName."""
        assert get_friendliest_name({"friendlyName": "Original"}) == "Original"


class TestGetDeviceSerial:
    """Test the get_device_serial function."""

    def test_returns_serial(self):
        """The dmsDeviceSerialNumber of the first dict entry is returned."""
        appliance = {"alexaDeviceIdentifierList": [{"dmsDeviceSerialNumber": "SER123"}]}
        assert get_device_serial(appliance) == "SER123"

    def test_empty_list(self):
        """An empty identifier list returns None."""
        assert get_device_serial({"alexaDeviceIdentifierList": []}) is None

    def test_missing_key(self):
        """A missing identifier list returns None."""
        assert get_device_serial({}) is None

    def test_first_dict_missing_serial(self):
        """A first dict without the serial key returns None."""
        appliance = {"alexaDeviceIdentifierList": [{"foo": "bar"}]}
        assert get_device_serial(appliance) is None

    def test_skips_non_dict_entries(self):
        """Non-dict entries are skipped until the first dict is found."""
        appliance = {
            "alexaDeviceIdentifierList": ["str", {"dmsDeviceSerialNumber": "SER1"}]
        }
        assert get_device_serial(appliance) == "SER1"


class TestGetDeviceBridge:
    """Test the get_device_bridge function."""

    def test_no_appliance_id(self):
        """A missing applianceId returns None."""
        assert get_device_bridge({}, {}) is None

    def test_non_string_appliance_id(self):
        """A non-string applianceId returns None."""
        assert get_device_bridge({"applianceId": 123}, {}) is None

    def test_no_hash(self):
        """An applianceId without '#' returns None."""
        appliance = {"applianceId": "AAA_SonarCloudService_x"}
        assert get_device_bridge(appliance, {}) is None

    def test_wrong_prefix(self):
        """A bridge id without the SonarCloudService prefix returns None."""
        appliance = {"applianceId": "OTHER_BRIDGE#child"}
        assert get_device_bridge(appliance, {"OTHER_BRIDGE": {}}) is None

    def test_bridge_found(self):
        """A resolvable bridge id returns the bridge appliance."""
        bridge = {"applianceId": "AAA_SonarCloudService_B1", "friendlyName": "Bridge"}
        appliance = {"applianceId": "AAA_SonarCloudService_B1#child"}
        result = get_device_bridge(appliance, {"AAA_SonarCloudService_B1": bridge})
        assert result is bridge

    def test_bridge_not_in_map(self):
        """A bridge id absent from the appliance map returns None."""
        appliance = {"applianceId": "AAA_SonarCloudService_B1#child"}
        assert get_device_bridge(appliance, {}) is None


_EMPTY_ENTITIES = {
    "light": [],
    "guard": [],
    "temperature": [],
    "air_quality": [],
    "aiaqm": [],
    "binary_sensor": [],
    "smart_switch": [],
}


def _guard_appliance():
    return {
        "entityId": "guard-entity",
        "applianceId": "guard-app",
        "friendlyName": "Home",
        "modelName": "REDROCK_GUARD_PANEL",
        "capabilities": [_capability("Alexa.SecurityPanelController", "armState")],
    }


def _temperature_appliance(entity_id="temp-entity", with_serial=True):
    appliance = {
        "entityId": entity_id,
        "applianceId": f"{entity_id}-app",
        "friendlyName": "Thermostat",
        "modelName": "THERMO",
        "connectedVia": "Echo",
        "friendlyDescription": "A Temperature Sensor",
        "capabilities": [_capability("Alexa.TemperatureSensor", "temperature")],
    }
    if with_serial:
        appliance["alexaDeviceIdentifierList"] = [{"dmsDeviceSerialNumber": "TEMP-SER"}]
    return appliance


def _aiaqm_appliance(with_serial=True):
    appliance = {
        "entityId": "aiaqm-entity",
        "applianceId": "aiaqm-app",
        "friendlyName": "Air Monitor",
        "modelName": "AIAQM",
        "friendlyDescription": "Amazon Indoor Air Quality Monitor",
        "applianceTypes": ["AIR_QUALITY_MONITOR"],
        "capabilities": [
            {
                "interfaceName": "Alexa.RangeController",
                "instance": "1",
                "properties": {
                    "retrievable": True,
                    "proactivelyReported": False,
                    "supported": [{"name": "rangeValue"}],
                },
                "configuration": {"unitOfMeasure": "Alexa.Unit.Percent"},
                "resources": {
                    "friendlyNames": [
                        {"value": {"assetId": "Alexa.AirQuality.Humidity"}}
                    ]
                },
            }
        ],
    }
    if with_serial:
        appliance["alexaDeviceIdentifierList"] = [
            {"dmsDeviceSerialNumber": "AQ-SERIAL"}
        ]
    return appliance


def _switch_appliance():
    return {
        "entityId": "switch-entity",
        "applianceId": "switch-app",
        "friendlyName": "Plug",
        "modelName": "PLUG",
        "connectedVia": "Echo",
        "applianceTypes": ["SMARTPLUG"],
        "capabilities": [_capability("Alexa.PowerController", "powerState")],
    }


def _light_appliance():
    return {
        "entityId": "light-entity",
        "applianceId": "light-app",
        "friendlyName": "Lamp",
        "modelName": "BULB",
        "connectedVia": "Echo",
        "applianceTypes": ["LIGHT"],
        "capabilities": [
            _capability("Alexa.PowerController", "powerState"),
            _capability("Alexa.BrightnessController", "brightness"),
            _capability("Alexa.ColorController", "color"),
            _capability("Alexa.ColorTemperatureController", "colorTemperatureInKelvin"),
        ],
    }


def _contact_appliance():
    return {
        "entityId": "contact-entity",
        "applianceId": "contact-app",
        "friendlyName": "Door",
        "modelName": "SENSOR",
        "connectedVia": "Echo",
        "applianceTypes": ["CONTACT_SENSOR"],
        "capabilities": [
            _capability("Alexa.ContactSensor", "detectionState"),
            _capability("Alexa.BatteryLevelSensor", "batteryLevel"),
        ],
    }


class TestParseAlexaEntities:
    """Test the parse_alexa_entities function."""

    def test_none_network_details(self):
        """None network details returns the empty entities structure."""
        assert parse_alexa_entities(None) == _EMPTY_ENTITIES

    def test_empty_network_details(self):
        """An empty list returns the empty entities structure."""
        assert parse_alexa_entities([]) == _EMPTY_ENTITIES

    def test_guard_entity(self):
        """A REDROCK guard appliance is parsed into the guard list."""
        result = parse_alexa_entities([_guard_appliance()])
        assert len(result["guard"]) == 1
        guard = result["guard"][0]
        assert guard["id"] == "guard-entity"
        assert guard["appliance_id"] == "guard-app"
        assert guard["name"] == "Home"
        assert guard["is_hue_v1"] is False

    def test_temperature_entity_with_serial(self):
        """A temperature sensor uses its device serial number."""
        result = parse_alexa_entities([_temperature_appliance()])
        assert len(result["temperature"]) == 1
        assert result["temperature"][0]["device_serial"] == "TEMP-SER"

    def test_temperature_entity_serial_fallback(self):
        """A temperature sensor without a serial falls back to the entity id."""
        result = parse_alexa_entities([_temperature_appliance(with_serial=False)])
        assert result["temperature"][0]["device_serial"] == "temp-entity"

    def test_aiaqm_entity(self):
        """An AIAQM is added to aiaqm, air_quality and temperature lists."""
        result = parse_alexa_entities([_aiaqm_appliance()])
        assert len(result["aiaqm"]) == 1
        assert len(result["air_quality"]) == 1
        assert len(result["temperature"]) == 1
        aiaqm = result["aiaqm"][0]
        assert aiaqm["device_serial"] == "AQ-SERIAL"
        assert aiaqm["sensors"] == [
            {
                "sensorType": "Alexa.AirQuality.Humidity",
                "instance": "1",
                "unit": "Alexa.Unit.Percent",
            }
        ]
        assert result["air_quality"][0]["device_serial"] == "AQ-SERIAL"
        assert result["temperature"][0]["is_aiaqm"] is True

    def test_aiaqm_serial_fallback(self):
        """An AIAQM without a serial falls back to the entity id."""
        result = parse_alexa_entities([_aiaqm_appliance(with_serial=False)])
        assert result["aiaqm"][0]["device_serial"] == "aiaqm-entity"

    def test_aiaqm_sensor_parsing_variants(self):
        """Only valid Alexa.AirQuality RangeController instances become sensors."""
        appliance = {
            "entityId": "aiaqm2",
            "applianceId": "aiaqm2-app",
            "friendlyName": "Air Monitor 2",
            "modelName": "AIAQM",
            "friendlyDescription": "Amazon Indoor Air Quality Monitor",
            "applianceTypes": ["AIR_QUALITY_MONITOR"],
            "capabilities": [
                # valid: value-dict assetId, string instance; non-dict and
                # non-air-quality friendlyName entries are skipped.
                {
                    "interfaceName": "Alexa.RangeController",
                    "instance": "humidity",
                    "properties": {
                        "retrievable": True,
                        "proactivelyReported": False,
                        "supported": [{"name": "rangeValue"}],
                    },
                    "configuration": {"unitOfMeasure": "Alexa.Unit.Percent"},
                    "resources": {
                        "friendlyNames": [
                            "not-a-dict",
                            {"value": {"assetId": "text-only"}},
                            {"value": {"assetId": "Alexa.AirQuality.Humidity"}},
                        ]
                    },
                },
                # valid: int instance -> "2", assetId on the entry, no configuration.
                {
                    "interfaceName": "Alexa.RangeController",
                    "instance": 2,
                    "properties": {
                        "retrievable": True,
                        "proactivelyReported": False,
                        "supported": [{"name": "rangeValue"}],
                    },
                    "resources": {
                        "friendlyNames": [
                            {"assetId": "Alexa.AirQuality.IndoorAirQuality"}
                        ]
                    },
                },
                # skipped: not a RangeController.
                _capability("Alexa.TemperatureSensor", "temperature"),
                # skipped: RangeController without rangeValue support.
                {
                    "interfaceName": "Alexa.RangeController",
                    "instance": "x",
                    "properties": {
                        "retrievable": True,
                        "proactivelyReported": False,
                        "supported": [{"name": "other"}],
                    },
                },
                # skipped: instance is None.
                {
                    "interfaceName": "Alexa.RangeController",
                    "instance": None,
                    "properties": {
                        "retrievable": True,
                        "proactivelyReported": False,
                        "supported": [{"name": "rangeValue"}],
                    },
                    "resources": {
                        "friendlyNames": [
                            {"value": {"assetId": "Alexa.AirQuality.PM25"}}
                        ]
                    },
                },
                # skipped: assetId not in the Alexa.AirQuality namespace.
                {
                    "interfaceName": "Alexa.RangeController",
                    "instance": "txt",
                    "properties": {
                        "retrievable": True,
                        "proactivelyReported": False,
                        "supported": [{"name": "rangeValue"}],
                    },
                    "resources": {
                        "friendlyNames": [
                            {"value": {"assetId": "Alexa.Setting.Something"}}
                        ]
                    },
                },
                # skipped: instance type is neither str nor int/float.
                {
                    "interfaceName": "Alexa.RangeController",
                    "instance": ["bad"],
                    "properties": {
                        "retrievable": True,
                        "proactivelyReported": False,
                        "supported": [{"name": "rangeValue"}],
                    },
                    "resources": {
                        "friendlyNames": [{"value": {"assetId": "Alexa.AirQuality.CO"}}]
                    },
                },
            ],
        }
        result = parse_alexa_entities([appliance])
        assert result["aiaqm"][0]["sensors"] == [
            {
                "sensorType": "Alexa.AirQuality.Humidity",
                "instance": "humidity",
                "unit": "Alexa.Unit.Percent",
            },
            {
                "sensorType": "Alexa.AirQuality.IndoorAirQuality",
                "instance": "2",
                "unit": "",
            },
        ]

    def test_switch_entity(self):
        """A local smart plug is parsed into the smart_switch list."""
        result = parse_alexa_entities([_switch_appliance()])
        assert len(result["smart_switch"]) == 1
        assert result["smart_switch"][0]["name"] == "Plug"

    def test_light_entity(self):
        """A light captures its brightness/color/color-temperature capabilities."""
        result = parse_alexa_entities([_light_appliance()])
        assert len(result["light"]) == 1
        light = result["light"][0]
        assert light["brightness"] is True
        assert light["color"] is True
        assert light["color_temperature"] is True

    def test_contact_sensor_entity(self):
        """A contact sensor captures its battery-level capability."""
        result = parse_alexa_entities([_contact_appliance()])
        assert len(result["binary_sensor"]) == 1
        assert result["binary_sensor"][0]["battery_level"] is True

    def test_unsupported_entity_ignored(self):
        """An appliance that matches nothing is dropped."""
        appliance = {
            "entityId": "u",
            "applianceId": "u-app",
            "friendlyName": "Mystery",
            "modelName": "UNKNOWN",
        }
        result = parse_alexa_entities([appliance])
        assert all(len(value) == 0 for value in result.values())

    def test_bridged_device_via_known_bridge_skipped(self):
        """A device bridged through a known HA bridge is skipped."""
        bridge = {
            "entityId": "bridge-entity",
            "applianceId": "AAA_SonarCloudService_B1",
            "friendlyName": "Matter Bridge",
            "modelName": "BRIDGE",
            "manufacturerName": "t0bst4r",
        }
        child = {
            "entityId": "child-entity",
            "applianceId": "AAA_SonarCloudService_B1#1",
            "friendlyName": "Bridged Light",
            "modelName": "BULB",
            "connectedVia": "Echo",
            "applianceTypes": ["LIGHT"],
            "capabilities": [_capability("Alexa.PowerController", "powerState")],
        }
        result = parse_alexa_entities([bridge, child])
        assert result["light"] == []

    def test_debug_mode_bridged_device(self):
        """Debug logging for bridged devices does not change the result."""
        bridge = {
            "entityId": "bridge-entity",
            "applianceId": "AAA_SonarCloudService_B1",
            "friendlyName": "Matter Bridge",
            "modelName": "BRIDGE",
            "manufacturerName": "t0bst4r",
        }
        child = {
            "entityId": "child-entity",
            "applianceId": "AAA_SonarCloudService_B1#1",
            "friendlyName": "Bridged Light",
            "modelName": "BULB",
            "connectedVia": "Echo",
            "applianceTypes": ["LIGHT"],
            "capabilities": [_capability("Alexa.PowerController", "powerState")],
        }
        result = parse_alexa_entities([bridge, child], debug=True)
        assert result["light"] == []

    def test_debug_mode_processes_all_types(self):
        """Debug mode parses every supported entity type."""
        appliances = [
            _guard_appliance(),
            _temperature_appliance(),
            _aiaqm_appliance(),
            _switch_appliance(),
            _light_appliance(),
            _contact_appliance(),
            {
                "entityId": "u",
                "applianceId": "u-app",
                "friendlyName": "U",
                "modelName": "X",
            },
        ]
        result = parse_alexa_entities(appliances, debug=True)
        assert len(result["guard"]) == 1
        assert len(result["temperature"]) == 2  # plain sensor + AIAQM temperature
        assert len(result["aiaqm"]) == 1
        assert len(result["air_quality"]) == 1
        assert len(result["smart_switch"]) == 1
        assert len(result["light"]) == 1
        assert len(result["binary_sensor"]) == 1


class TestGetEntityData:
    """Test the async get_entity_data network helper."""

    _TARGET = "custom_components.alexa_media.alexa_entity.AlexaAPI.get_entity_state"

    async def test_empty_entity_ids_skips_api(self):
        """An empty entity id list short-circuits without calling the API."""
        with patch(self._TARGET, new=AsyncMock()) as mock_get:
            result = await get_entity_data(MagicMock(), [])
        assert result == {}
        mock_get.assert_not_awaited()

    async def test_parses_device_states(self):
        """capabilityStates JSON strings are decoded per entity."""
        login = MagicMock()
        raw = {
            "deviceStates": [
                {
                    "entity": {"entityId": "e1"},
                    "capabilityStates": [
                        '{"namespace": "Alexa.PowerController",'
                        ' "name": "powerState", "value": "ON"}',
                        '{"namespace": "Alexa.BrightnessController",'
                        ' "name": "brightness", "value": 50}',
                    ],
                }
            ]
        }
        with patch(self._TARGET, new=AsyncMock(return_value=raw)) as mock_get:
            result = await get_entity_data(login, ["e1"])
        mock_get.assert_awaited_once_with(login, entity_ids=["e1"])
        assert result == {
            "e1": [
                {
                    "namespace": "Alexa.PowerController",
                    "name": "powerState",
                    "value": "ON",
                },
                {
                    "namespace": "Alexa.BrightnessController",
                    "name": "brightness",
                    "value": 50,
                },
            ]
        }

    async def test_malformed_capability_state_is_skipped(self):
        """Malformed capability JSON is skipped without dropping the rest."""
        raw = {
            "deviceStates": [
                {
                    "entity": {"entityId": "e1"},
                    "capabilityStates": [
                        "not-json{",
                        None,
                        (
                            '{"namespace": "Alexa.PowerController",'
                            ' "name": "powerState", "value": "ON"}'
                        ),
                    ],
                }
            ]
        }
        with patch(self._TARGET, new=AsyncMock(return_value=raw)):
            result = await get_entity_data(MagicMock(), ["e1"])
        assert result == {
            "e1": [
                {
                    "namespace": "Alexa.PowerController",
                    "name": "powerState",
                    "value": "ON",
                }
            ]
        }

    async def test_raw_not_dict_returns_empty(self):
        """A non-dict response yields an empty mapping."""
        with patch(self._TARGET, new=AsyncMock(return_value=None)):
            result = await get_entity_data(MagicMock(), ["e1"])
        assert result == {}

    async def test_no_device_states_returns_empty(self):
        """A response without device states yields an empty mapping."""
        with patch(self._TARGET, new=AsyncMock(return_value={"deviceStates": []})):
            result = await get_entity_data(MagicMock(), ["e1"])
        assert result == {}

    async def test_device_state_without_entity_id_skipped(self):
        """A device state lacking an entity id is skipped."""
        raw = {"deviceStates": [{"entity": {}, "capabilityStates": ['{"a": 1}']}]}
        with patch(self._TARGET, new=AsyncMock(return_value=raw)):
            result = await get_entity_data(MagicMock(), ["e1"])
        assert result == {}

    async def test_empty_capability_states(self):
        """An entity with no capability states maps to an empty list."""
        raw = {"deviceStates": [{"entity": {"entityId": "e1"}, "capabilityStates": []}]}
        with patch(self._TARGET, new=AsyncMock(return_value=raw)):
            result = await get_entity_data(MagicMock(), ["e1"])
        assert result == {"e1": []}


class TestParseCoordinatorWrappers:
    """Test the parse_*_from_coordinator helper functions."""

    def test_temperature(self):
        """Temperature values are read from the coordinator data."""
        value = {"value": 20.5, "scale": "CELSIUS"}
        coordinator = _coordinator(
            {"e1": [_cap_state("Alexa.TemperatureSensor", "temperature", value)]}
        )
        assert parse_temperature_from_coordinator(coordinator, "e1") == value

    def test_temperature_missing_with_debug(self):
        """A missing temperature returns None (debug path)."""
        coordinator = _coordinator({"e1": []})
        assert parse_temperature_from_coordinator(coordinator, "e1", debug=True) is None

    def test_air_quality_instance_match(self):
        """Air quality reads the value for a matching instance."""
        coordinator = _coordinator(
            {
                "e1": [
                    _cap_state("Alexa.RangeController", "rangeValue", 42, instance="3")
                ]
            }
        )
        assert parse_air_quality_from_coordinator(coordinator, "e1", "3") == 42

    def test_air_quality_instance_mismatch(self):
        """Air quality returns None when the instance does not match."""
        coordinator = _coordinator(
            {
                "e1": [
                    _cap_state("Alexa.RangeController", "rangeValue", 42, instance="3")
                ]
            }
        )
        assert parse_air_quality_from_coordinator(coordinator, "e1", "9") is None

    def test_brightness(self):
        """Brightness values are read from the coordinator data."""
        coordinator = _coordinator(
            {"e1": [_cap_state("Alexa.BrightnessController", "brightness", 75)]}
        )
        assert parse_brightness_from_coordinator(coordinator, "e1", None) == 75

    def test_color_temp(self):
        """Color temperature values are read from the coordinator data."""
        coordinator = _coordinator(
            {
                "e1": [
                    _cap_state(
                        "Alexa.ColorTemperatureController",
                        "colorTemperatureInKelvin",
                        3000,
                    )
                ]
            }
        )
        assert parse_color_temp_from_coordinator(coordinator, "e1", None) == 3000

    def test_color(self):
        """Color is returned as a (hue, saturation, 1) tuple."""
        value = {"hue": 120, "saturation": 0.5, "brightness": 1.0}
        coordinator = _coordinator(
            {"e1": [_cap_state("Alexa.ColorController", "color", value)]}
        )
        assert parse_color_from_coordinator(coordinator, "e1", None) == (120, 0.5, 1)

    def test_color_missing_keys_defaults(self):
        """Missing hue/saturation default to zero."""
        coordinator = _coordinator(
            {"e1": [_cap_state("Alexa.ColorController", "color", {})]}
        )
        assert parse_color_from_coordinator(coordinator, "e1", None) == (0, 0, 1)

    def test_color_none(self):
        """A missing color value returns None."""
        coordinator = _coordinator({"e1": []})
        assert parse_color_from_coordinator(coordinator, "e1", None) is None

    def test_power(self):
        """Power state is read from the coordinator data."""
        coordinator = _coordinator(
            {"e1": [_cap_state("Alexa.PowerController", "powerState", "ON")]}
        )
        assert parse_power_from_coordinator(coordinator, "e1", None) == "ON"

    def test_guard_state(self):
        """Guard arm state is read from the coordinator data."""
        coordinator = _coordinator(
            {
                "e1": [
                    _cap_state(
                        "Alexa.SecurityPanelController", "armState", "ARMED_AWAY"
                    )
                ]
            }
        )
        assert parse_guard_state_from_coordinator(coordinator, "e1") == "ARMED_AWAY"

    def test_detection_state(self):
        """Contact detection state is read from the coordinator data."""
        coordinator = _coordinator(
            {"e1": [_cap_state("Alexa.ContactSensor", "detectionState", "DETECTED")]}
        )
        assert parse_detection_state_from_coordinator(coordinator, "e1") == "DETECTED"


class TestParseValueFromCoordinator:
    """Test the parse_value_from_coordinator core function."""

    def test_data_none_with_debug(self):
        """No coordinator data returns None (debug path)."""
        coordinator = _coordinator(None)
        assert (
            parse_value_from_coordinator(coordinator, "e1", "ns", "name", debug=True)
            is None
        )

    def test_entity_not_in_data(self):
        """An entity absent from the data returns None."""
        coordinator = _coordinator({"other": []})
        assert parse_value_from_coordinator(coordinator, "e1", "ns", "name") is None

    def test_match_returns_value(self):
        """A matching namespace/name returns the value."""
        coordinator = _coordinator({"e1": [_cap_state("ns", "name", "V")]})
        assert parse_value_from_coordinator(coordinator, "e1", "ns", "name") == "V"

    def test_no_matching_cap_state(self):
        """A non-matching name returns None."""
        coordinator = _coordinator({"e1": [_cap_state("ns", "other", "V")]})
        assert parse_value_from_coordinator(coordinator, "e1", "ns", "name") is None

    def test_too_old_returns_none_with_debug(self):
        """A stale sample (within TTL) is rejected and returns None."""
        since = datetime.now(UTC) - timedelta(seconds=5)
        cap_state = _cap_state(
            "ns", "name", "OLD", time_of_sample="2000-01-01T00:00:00+00:00"
        )
        coordinator = _coordinator({"e1": [cap_state]})
        assert (
            parse_value_from_coordinator(
                coordinator, "e1", "ns", "name", since=since, debug=True
            )
            is None
        )

    def test_skips_old_returns_newer(self):
        """A newer matching sample is returned even after an older one."""
        since = datetime.now(UTC) - timedelta(seconds=5)
        old = _cap_state(
            "ns", "name", "OLD", time_of_sample="2000-01-01T00:00:00+00:00"
        )
        new = _cap_state(
            "ns", "name", "NEW", time_of_sample="2099-01-01T00:00:00+00:00"
        )
        coordinator = _coordinator({"e1": [old, new]})
        assert (
            parse_value_from_coordinator(coordinator, "e1", "ns", "name", since=since)
            == "NEW"
        )


class TestIsCapStateStillAcceptable:
    """Test the is_cap_state_still_acceptable function."""

    def test_since_none(self):
        """No 'since' constraint always accepts the sample."""
        assert is_cap_state_still_acceptable({}, None)

    def test_ttl_expired(self):
        """Once the requested-state TTL has elapsed, the sample is accepted."""
        since = datetime.now(UTC) - timedelta(seconds=30)
        assert is_cap_state_still_acceptable({}, since)

    def test_recent_no_time_of_sample(self):
        """Within the TTL and without a timeOfSample the sample is rejected."""
        since = datetime.now(UTC) - timedelta(seconds=5)
        assert not is_cap_state_still_acceptable({}, since)

    def test_recent_future_sample(self):
        """A sample newer than 'since' is accepted."""
        since = datetime.now(UTC) - timedelta(seconds=5)
        cap_state = {"timeOfSample": "2099-01-01T00:00:00+00:00"}
        assert is_cap_state_still_acceptable(cap_state, since)

    def test_recent_past_sample(self):
        """A sample older than 'since' is rejected."""
        since = datetime.now(UTC) - timedelta(seconds=5)
        cap_state = {"timeOfSample": "2000-01-01T00:00:00+00:00"}
        assert not is_cap_state_still_acceptable(cap_state, since)

    def test_recent_unparsable_sample(self):
        """An unparsable timeOfSample is rejected."""
        since = datetime.now(UTC) - timedelta(seconds=5)
        cap_state = {"timeOfSample": "not-a-date"}
        assert not is_cap_state_still_acceptable(cap_state, since)
