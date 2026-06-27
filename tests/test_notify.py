"""Tests for notify module.

Tests the notification service using pytest-homeassistant-custom-component.
"""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.components.notify import SERVICE_NOTIFY
from homeassistant.const import CONF_EMAIL
import pytest
import voluptuous as vol

from custom_components.alexa_media.const import (
    CONF_QUEUE_DELAY,
    DATA_ALEXAMEDIA,
    DEFAULT_QUEUE_DELAY,
    DOMAIN,
)
from custom_components.alexa_media.notify import (
    AlexaNotificationService,
    async_get_service,
    async_unload_entry,
)

# =============================================================================
# Tests for AlexaNotificationService.devices property
# =============================================================================


class TestNotifyDevicesProperty:
    """Test the devices property of AlexaNotificationService.

    These tests cover a critical bug fix where the `devices` property raised
    KeyError when the 'accounts' key was missing from hass.data[DATA_ALEXAMEDIA].

    The Bug (BEFORE fix):
        if "accounts" not in data and not data["accounts"].items():

    Python evaluates BOTH sides of `and`. When "accounts" is missing, accessing
    data["accounts"] raises KeyError.

    The Fix (AFTER):
        if "accounts" not in data or not data["accounts"].items():

    With `or`, Python short-circuits and skips data["accounts"] when the first
    condition is True.
    """

    def _create_service(self, hass_data: dict) -> AlexaNotificationService:
        """Create a notification service with the given hass.data."""
        service = object.__new__(AlexaNotificationService)
        service.hass = MagicMock()
        service.hass.data = hass_data
        return service

    def test_devices_keyerror_when_accounts_missing(self):
        """Test that devices property does NOT raise KeyError when accounts missing.

        This is the PRIMARY regression test for the and->or bug fix.

        The bug: Using `and` instead of `or` caused Python to evaluate
        `data["accounts"]` even when "accounts" was not in the dict.
        """
        service = self._create_service(
            {
                DATA_ALEXAMEDIA: {
                    # 'accounts' key is intentionally MISSING
                    "config_flows": {},
                }
            }
        )

        # This MUST NOT raise KeyError
        try:
            result = service.devices
        except KeyError as exc:
            pytest.fail(
                f"BUG DETECTED: KeyError raised: {exc}\n\n"
                "CAUSE: The condition uses 'and' instead of 'or':\n"
                "  WRONG: 'accounts' not in data AND data['accounts'].items()\n"
                "  RIGHT: 'accounts' not in data OR  data['accounts'].items()\n\n"
                "With 'and', Python evaluates BOTH conditions. When 'accounts'\n"
                "is missing, accessing data['accounts'] raises KeyError.\n"
                "With 'or', Python short-circuits and never accesses the key."
            )

        assert result == []

    def test_devices_empty_when_accounts_key_missing(self):
        """Test devices returns empty list when accounts key is missing."""
        service = self._create_service({DATA_ALEXAMEDIA: {"config_flows": {}}})

        result = service.devices

        assert result == [], f"Expected empty list, got: {result}"

    def test_devices_empty_when_accounts_is_empty_dict(self):
        """Test devices returns empty list when accounts exists but is empty."""
        service = self._create_service(
            {
                DATA_ALEXAMEDIA: {
                    "accounts": {},
                    "config_flows": {},
                }
            }
        )

        result = service.devices

        assert result == [], f"Expected empty list, got: {result}"

    def test_devices_empty_when_data_alexamedia_missing(self):
        """Test devices handles missing DATA_ALEXAMEDIA gracefully."""
        service = self._create_service({})

        # Should raise KeyError for DATA_ALEXAMEDIA - this is expected behavior
        # The fix only addresses the accounts key, not DATA_ALEXAMEDIA itself
        with pytest.raises(KeyError):
            _ = service.devices

    def test_devices_returns_media_players(self):
        """Test devices returns media players when accounts exist."""
        mock_player_1 = MagicMock()
        mock_player_1.name = "Living Room Echo"
        mock_player_2 = MagicMock()
        mock_player_2.name = "Kitchen Echo"

        service = self._create_service(
            {
                DATA_ALEXAMEDIA: {
                    "accounts": {
                        "test@example.com": {
                            "entities": {
                                "media_player": {
                                    "serial1": mock_player_1,
                                    "serial2": mock_player_2,
                                }
                            }
                        }
                    },
                    "config_flows": {},
                }
            }
        )

        result = service.devices

        assert len(result) == 2
        assert mock_player_1 in result
        assert mock_player_2 in result

    def test_devices_aggregates_multiple_accounts(self):
        """Test devices aggregates media players from all accounts."""
        mock_player_1 = MagicMock()
        mock_player_2 = MagicMock()

        service = self._create_service(
            {
                DATA_ALEXAMEDIA: {
                    "accounts": {
                        "user1@example.com": {
                            "entities": {"media_player": {"serial1": mock_player_1}}
                        },
                        "user2@example.com": {
                            "entities": {"media_player": {"serial2": mock_player_2}}
                        },
                    },
                    "config_flows": {},
                }
            }
        )

        result = service.devices

        assert len(result) == 2
        assert mock_player_1 in result
        assert mock_player_2 in result


# =============================================================================
# DRAFT: Tests for async_send_message group expansion (PR #3446)
#
# These tests cover the two-stage target preprocessing introduced in PR #3446:
#   1. Normalisation: JSON / comma-delimited / bare string → list of strings
#   2. Expansion: media_player.* helper groups and old-style group.* YAML groups
#
# =============================================================================


class TestAsyncSendMessageGroupExpansion:
    """Draft tests for target normalisation and group expansion in async_send_message.

    Covers the fix introduced in PR #3446 (restore old-style YAML groups expansion):
    - UI media_player.* helper groups are expanded via state.attributes["entity_id"]
    - Legacy group.* YAML entities are expanded via expand_entity_ids()
    - Targets that are not groups are passed through unchanged
    - Non-string targets are passed through unchanged
    - Comma-delimited and JSON string targets are normalised before expansion
    """

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _make_hass(self, states: dict | None = None) -> MagicMock:
        """Return a minimal hass mock.

        Parameters
        ----------
        states:
            Mapping of entity_id → dict of attributes, used to drive
            hass.states.get().
        """
        hass = MagicMock()
        hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {
                    "test@example.com": {
                        "entities": {"media_player": {}},
                        "options": {},
                    }
                }
            }
        }

        def _states_get(entity_id):
            if states and entity_id in states:
                state = MagicMock()
                state.attributes = states[entity_id]
                return state
            return None

        hass.states.get.side_effect = _states_get
        return hass

    def _create_service(self, hass: MagicMock):
        """Instantiate AlexaNotificationService without calling __init__."""
        from custom_components.alexa_media.notify import AlexaNotificationService

        service = object.__new__(AlexaNotificationService)
        service.hass = hass
        service.last_called = True
        # Stub convert() so tests focus purely on expansion logic.
        service.convert = MagicMock(return_value=[])
        return service

    # ------------------------------------------------------------------
    # Target normalisation tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_bare_string_target_is_appended(self):
        """A single bare string target (no comma, not JSON) is kept as-is."""
        hass = self._make_hass()
        service = self._create_service(hass)

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=["media_player.echo1"],
        ):
            await service.async_send_message(
                "hello", **{"target": ["media_player.echo1"]}
            )

        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "media_player.echo1" in expanded

    @pytest.mark.asyncio
    async def test_comma_delimited_target_is_split(self):
        """A comma-delimited target string is split into individual targets."""
        hass = self._make_hass()
        service = self._create_service(hass)

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=[],
        ):
            await service.async_send_message(
                "hello",
                **{"target": ["media_player.echo1, media_player.echo2"]},
            )

        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "media_player.echo1" in expanded
        assert "media_player.echo2" in expanded

    @pytest.mark.asyncio
    async def test_json_string_target_is_parsed(self):
        """A JSON-encoded list target is decoded into individual targets."""
        import json

        hass = self._make_hass()
        service = self._create_service(hass)

        json_target = json.dumps(["media_player.echo1", "media_player.echo2"])

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=[],
        ):
            await service.async_send_message(
                "hello",
                **{"target": [json_target]},
            )

        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "media_player.echo1" in expanded
        assert "media_player.echo2" in expanded

    @pytest.mark.asyncio
    async def test_non_string_dict_target_does_not_raise(self):
        """A dict target must not raise TypeError from json.loads.

        Regression test for issue #3453: ``json.loads`` only accepts
        ``str | bytes | bytearray``. A dict (or any non-string) raised
        ``TypeError`` outside the ``json.JSONDecodeError`` handler, aborting
        ``async_send_message``.
        """
        hass = self._make_hass()
        service = self._create_service(hass)

        dict_target = {"entity_id": "media_player.echo1"}

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=[],
        ):
            await service.async_send_message("hello", **{"target": [dict_target]})

        service.convert.assert_called_once()
        expanded = service.convert.call_args[0][0]
        assert dict_target in expanded

    @pytest.mark.asyncio
    async def test_non_string_int_target_does_not_raise(self):
        """An int target is appended verbatim without TypeError."""
        hass = self._make_hass()
        service = self._create_service(hass)

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=[],
        ):
            await service.async_send_message("hello", **{"target": [42]})

        service.convert.assert_called_once()
        expanded = service.convert.call_args[0][0]
        assert 42 in expanded

    @pytest.mark.asyncio
    async def test_json_scalar_string_target_appended_not_extended(self):
        """JSON that decodes to a scalar (non-list) is appended, not extended.

        Previous code used ``processed_targets += json.loads(target)`` which
        would iterate a string into individual characters. The fix appends a
        non-list parse result instead.
        """
        hass = self._make_hass()
        service = self._create_service(hass)

        # ``json.loads('"echo1"')`` returns the string "echo1". Under the old
        # code, ``processed_targets += "echo1"`` would produce
        # ``["e", "c", "h", "o", "1"]``.
        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=[],
        ):
            await service.async_send_message("hello", **{"target": ['"echo1"']})

        service.convert.assert_called_once()
        expanded = service.convert.call_args[0][0]
        assert "echo1" in expanded
        assert "e" not in expanded

    # ------------------------------------------------------------------
    # media_player.* group expansion tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_media_player_group_with_list_members_is_expanded(self):
        """A media_player.* group whose entity_id attribute is a list is expanded."""
        hass = self._make_hass(
            states={
                "media_player.echo_group": {
                    "entity_id": ["media_player.echo1", "media_player.echo2"]
                }
            }
        )
        service = self._create_service(hass)

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=[],
        ):
            await service.async_send_message(
                "hello", **{"target": ["media_player.echo_group"]}
            )

        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "media_player.echo1" in expanded
        assert "media_player.echo2" in expanded
        assert "media_player.echo_group" not in expanded

    @pytest.mark.asyncio
    async def test_media_player_group_with_tuple_members_is_expanded(self):
        """A media_player.* group whose entity_id is a tuple is expanded."""
        hass = self._make_hass(
            states={
                "media_player.echo_group": {
                    "entity_id": ("media_player.echo1", "media_player.echo2")
                }
            }
        )
        service = self._create_service(hass)

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=[],
        ):
            await service.async_send_message(
                "hello", **{"target": ["media_player.echo_group"]}
            )

        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "media_player.echo1" in expanded
        assert "media_player.echo2" in expanded

    @pytest.mark.asyncio
    async def test_media_player_group_with_non_list_entity_id_kept_as_is(self):
        """A media_player.* group whose entity_id is not a list is kept unchanged.

        The code appends the group target itself rather than the scalar entity_id
        value, matching the intent described in the PR comments.
        """
        hass = self._make_hass(
            states={
                "media_player.echo_group": {
                    "entity_id": "media_player.echo1"  # scalar, not a list
                }
            }
        )
        service = self._create_service(hass)

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=[],
        ):
            await service.async_send_message(
                "hello", **{"target": ["media_player.echo_group"]}
            )

        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "media_player.echo_group" in expanded

    @pytest.mark.asyncio
    async def test_media_player_entity_without_entity_id_attr_passes_through(self):
        """A media_player.* entity that is NOT a group (no entity_id attr) passes through."""
        hass = self._make_hass(
            states={
                "media_player.echo1": {"friendly_name": "Echo"}  # no entity_id attr
            }
        )
        service = self._create_service(hass)

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=[],
        ):
            await service.async_send_message(
                "hello", **{"target": ["media_player.echo1"]}
            )

        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "media_player.echo1" in expanded

    # ------------------------------------------------------------------
    # group.* (old-style YAML) expansion tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_yaml_group_is_expanded_via_expand_entity_ids(self):
        """A group.* target is expanded using expand_entity_ids().

        This is the PRIMARY regression test for PR #3446. Prior to this fix,
        group.* targets were not handled and silently passed through without
        expansion, so notify.alexa_media failed to reach group members.
        """
        hass = self._make_hass()
        service = self._create_service(hass)

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=["media_player.echo1", "media_player.echo2"],
        ) as mock_expand:
            await service.async_send_message(
                "hello", **{"target": ["group.echo_players"]}
            )

        mock_expand.assert_called_once_with(hass, ["group.echo_players"])
        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "media_player.echo1" in expanded
        assert "media_player.echo2" in expanded
        assert "group.echo_players" not in expanded

    @pytest.mark.asyncio
    async def test_yaml_group_expansion_falls_back_on_value_error(self):
        """When expand_entity_ids raises ValueError the original target is kept."""
        hass = self._make_hass()
        service = self._create_service(hass)

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            side_effect=ValueError("invalid group"),
        ):
            await service.async_send_message("hello", **{"target": ["group.bad_group"]})

        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "group.bad_group" in expanded

    @pytest.mark.asyncio
    async def test_yaml_group_original_not_in_expanded_when_successfully_expanded(
        self,
    ):
        """group.* target is removed from the list after successful expansion."""
        hass = self._make_hass()
        service = self._create_service(hass)

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=["media_player.echo1"],
        ):
            await service.async_send_message(
                "hello", **{"target": ["group.echo_players"]}
            )

        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "group.echo_players" not in expanded

    # ------------------------------------------------------------------
    # Pass-through tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_non_group_string_target_passes_through(self):
        """A plain string target that is neither media_player.* nor group.* passes through."""
        hass = self._make_hass()
        service = self._create_service(hass)

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=[],
        ):
            await service.async_send_message(
                "hello", **{"target": ["Living Room Echo"]}
            )

        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "Living Room Echo" in expanded

    @pytest.mark.asyncio
    async def test_sensor_domain_target_passes_through_unchanged(self):
        """A sensor.* target (not a group or media_player group) passes through unchanged."""
        hass = self._make_hass()
        service = self._create_service(hass)

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            return_value=[],
        ):
            await service.async_send_message(
                "hello", **{"target": ["sensor.temperature"]}
            )

        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "sensor.temperature" in expanded

    # ------------------------------------------------------------------
    # Mixed target list tests
    # ------------------------------------------------------------------

    @pytest.mark.asyncio
    async def test_mixed_group_and_plain_targets_all_resolved(self):
        """A mix of group.*, media_player group, and plain targets is fully resolved."""
        hass = self._make_hass(
            states={"media_player.echo_group": {"entity_id": ["media_player.echo1"]}}
        )
        service = self._create_service(hass)

        def _expand(hass_arg, entity_ids):
            if "group.echo_players" in entity_ids:
                return ["media_player.echo2", "media_player.echo3"]
            return entity_ids

        with patch(
            "custom_components.alexa_media.notify.expand_entity_ids",
            side_effect=_expand,
        ):
            await service.async_send_message(
                "hello",
                **{
                    "target": [
                        "media_player.echo_group",
                        "group.echo_players",
                        "Living Room Echo",
                    ]
                },
            )

        service.convert.assert_called_once()
        assert service.convert.call_args.kwargs["type_"] == "entities"
        expanded = service.convert.call_args[0][0]
        assert "media_player.echo1" in expanded  # from media_player group
        assert "media_player.echo2" in expanded  # from YAML group
        assert "media_player.echo3" in expanded  # from YAML group
        assert "Living Room Echo" in expanded  # plain target
        assert "media_player.echo_group" not in expanded
        assert "group.echo_players" not in expanded


# =============================================================================
# Tests for service construction + module entry points
# =============================================================================


class TestServiceConstruction:
    """Test AlexaNotificationService.__init__."""

    def test_init_sets_hass_and_last_called(self):
        """__init__ stores hass and defaults last_called to True."""
        hass = MagicMock()

        service = AlexaNotificationService(hass)

        assert service.hass is hass
        assert service.last_called is True


class TestAsyncGetService:
    """Test async_get_service.

    The function is wrapped by ``retry_async`` (which sleeps between retries on
    a falsy result). Tests call ``async_get_service.__wrapped__`` to exercise
    the undecorated body directly, avoiding the retry delays entirely.
    """

    @pytest.mark.asyncio
    async def test_returns_service_when_all_media_players_loaded(self):
        """A service is created and stored when every device has an entity."""
        hass = MagicMock()
        hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {
                    "test@example.com": {
                        "devices": {"media_player": {"serial1": MagicMock()}},
                        "entities": {"media_player": {"serial1": MagicMock()}},
                    }
                }
            }
        }

        result = await async_get_service.__wrapped__(hass, {})

        assert isinstance(result, AlexaNotificationService)
        assert hass.data[DATA_ALEXAMEDIA]["notify_service"] is result

    @pytest.mark.asyncio
    async def test_returns_false_when_media_player_not_loaded(self):
        """False is returned (delaying load) when a device has no entity yet."""
        hass = MagicMock()
        hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {
                    "test@example.com": {
                        "devices": {
                            "media_player": {
                                "serial1": MagicMock(),
                                "serial2": MagicMock(),
                            }
                        },
                        # serial2 is intentionally absent from entities.
                        "entities": {"media_player": {"serial1": MagicMock()}},
                    }
                }
            }
        }

        result = await async_get_service.__wrapped__(hass, {})

        assert result is False
        assert "notify_service" not in hass.data[DATA_ALEXAMEDIA]


class TestAsyncUnloadEntry:
    """Test async_unload_entry service-removal logic."""

    def _make_entry(self, email="test@example.com"):
        entry = MagicMock()
        entry.data = {CONF_EMAIL: email}
        return entry

    @pytest.mark.asyncio
    async def test_removes_services_and_pops_notify_service(self):
        """Single-account unload removes per-device + domain services and state."""
        device = MagicMock()
        device.entity_id = "media_player.echo1"
        hass = MagicMock()
        hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {
                    "test@example.com": {
                        "entities": {"media_player": {"serial1": device}},
                    }
                },
                "notify_service": MagicMock(),
            }
        }

        result = await async_unload_entry(hass, self._make_entry())

        assert result is True
        hass.services.async_remove.assert_any_call(SERVICE_NOTIFY, f"{DOMAIN}_echo1")
        hass.services.async_remove.assert_any_call(SERVICE_NOTIFY, DOMAIN)
        assert "notify_service" not in hass.data[DATA_ALEXAMEDIA]

    @pytest.mark.asyncio
    async def test_skips_account_without_entities(self):
        """A target account without an 'entities' key is skipped (continue)."""
        hass = MagicMock()
        hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {"test@example.com": {}},
            }
        }

        result = await async_unload_entry(hass, self._make_entry())

        assert result is True
        # Only the domain-wide service is removed; no per-device removal.
        hass.services.async_remove.assert_called_once_with(SERVICE_NOTIFY, DOMAIN)

    @pytest.mark.asyncio
    async def test_skips_device_without_entity_id(self):
        """A device whose entity_id is falsy is not removed."""
        device = MagicMock()
        device.entity_id = None
        hass = MagicMock()
        hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {
                    "test@example.com": {
                        "entities": {"media_player": {"serial1": device}},
                    }
                },
            }
        }

        result = await async_unload_entry(hass, self._make_entry())

        assert result is True
        # No per-device removal happened; only the domain-wide removal.
        hass.services.async_remove.assert_called_once_with(SERVICE_NOTIFY, DOMAIN)

    @pytest.mark.asyncio
    async def test_keeps_domain_service_when_other_accounts_present(self):
        """When another account remains, the domain service is preserved."""
        device = MagicMock()
        device.entity_id = "media_player.echo1"
        notify_service = MagicMock()
        hass = MagicMock()
        hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {
                    "test@example.com": {
                        "entities": {"media_player": {"serial1": device}},
                    },
                    "other@example.com": {
                        "entities": {"media_player": {}},
                    },
                },
                "notify_service": notify_service,
            }
        }

        result = await async_unload_entry(hass, self._make_entry())

        assert result is True
        # Only the per-device service for the target account is removed.
        hass.services.async_remove.assert_called_once_with(
            SERVICE_NOTIFY, f"{DOMAIN}_echo1"
        )
        # The shared notify_service is left in place for the remaining account.
        assert hass.data[DATA_ALEXAMEDIA]["notify_service"] is notify_service


# =============================================================================
# Tests for AlexaNotificationService.convert
# =============================================================================


class TestConvert:
    """Test convert() name / serial / entity_id resolution and type mapping."""

    def _alexa(self, name, serial, entity_id):
        alexa = MagicMock()
        alexa.name = name
        alexa.unique_id = serial
        alexa.device_serial_number = serial
        alexa.entity_id = entity_id
        return alexa

    def _service(self, devices):
        hass = MagicMock()
        hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {
                    "test@example.com": {
                        "entities": {
                            "media_player": {d.device_serial_number: d for d in devices}
                        },
                        "options": {},
                    }
                }
            }
        }
        service = object.__new__(AlexaNotificationService)
        service.hass = hass
        service.last_called = True
        return service

    def test_convert_string_is_wrapped_in_list(self):
        """A bare string (not a list) is treated as a single name."""
        alexa = self._alexa("Echo 1", "serial1", "media_player.echo1")
        service = self._service([alexa])

        assert service.convert("Echo 1") == [alexa]

    def test_convert_matches_by_name(self):
        """A device is matched by its accountName."""
        alexa = self._alexa("Echo 1", "serial1", "media_player.echo1")
        service = self._service([alexa])

        assert service.convert(["Echo 1"]) == [alexa]

    def test_convert_matches_by_serial(self):
        """A device is matched by its serial number / unique_id."""
        alexa = self._alexa("Echo 1", "serial1", "media_player.echo1")
        service = self._service([alexa])

        assert service.convert(["serial1"]) == [alexa]

    def test_convert_matches_by_entity_id(self):
        """A device is matched by its Home Assistant entity_id."""
        alexa = self._alexa("Echo 1", "serial1", "media_player.echo1")
        service = self._service([alexa])

        assert service.convert(["media_player.echo1"]) == [alexa]

    def test_convert_matches_by_object(self):
        """A device is matched when the alexa object itself is passed."""
        alexa = self._alexa("Echo 1", "serial1", "media_player.echo1")
        service = self._service([alexa])

        assert service.convert([alexa]) == [alexa]

    def test_convert_type_serialnumbers(self):
        """type_='serialnumbers' returns the device serial."""
        alexa = self._alexa("Echo 1", "serial1", "media_player.echo1")
        service = self._service([alexa])

        assert service.convert(["Echo 1"], type_="serialnumbers") == ["serial1"]

    def test_convert_type_names(self):
        """type_='names' returns the device accountName."""
        alexa = self._alexa("Echo 1", "serial1", "media_player.echo1")
        service = self._service([alexa])

        assert service.convert(["serial1"], type_="names") == ["Echo 1"]

    def test_convert_type_entity_ids(self):
        """type_='entity_ids' returns the device entity_id."""
        alexa = self._alexa("Echo 1", "serial1", "media_player.echo1")
        service = self._service([alexa])

        assert service.convert(["Echo 1"], type_="entity_ids") == ["media_player.echo1"]

    def test_convert_passes_through_unmatched_when_not_filtering(self):
        """An unmatched name is returned verbatim when filter_matches is False."""
        alexa = self._alexa("Echo 1", "serial1", "media_player.echo1")
        service = self._service([alexa])

        assert service.convert(["unknown"]) == ["unknown"]

    def test_convert_drops_unmatched_when_filtering(self):
        """An unmatched name is dropped when filter_matches is True."""
        alexa = self._alexa("Echo 1", "serial1", "media_player.echo1")
        service = self._service([alexa])

        assert service.convert(["unknown"], filter_matches=True) == []


# =============================================================================
# Tests for AlexaNotificationService.targets property
# =============================================================================


class TestTargetsProperty:
    """Test the targets property, including last_called resolution."""

    def _service(self, accounts, last_called=True):
        hass = MagicMock()
        hass.data = {DATA_ALEXAMEDIA: {"accounts": accounts}}
        service = object.__new__(AlexaNotificationService)
        service.hass = hass
        service.last_called = last_called
        return service

    def _entity(self, entity_id, unique_id, last_called=None, ts=None):
        entity = MagicMock()
        entity.entity_id = entity_id
        entity.unique_id = unique_id
        attrs = {}
        if last_called is not None:
            attrs["last_called"] = last_called
        if ts is not None:
            attrs["last_called_timestamp"] = ts
        entity.extra_state_attributes = attrs
        return entity

    def test_basic_device_mapping(self):
        """Each entity maps its short name to its unique_id."""
        entity = self._entity("media_player.echo1", "u1")
        service = self._service(
            {"test@example.com": {"entities": {"media_player": {"s1": entity}}}}
        )

        assert service.targets == {"echo1": "u1"}

    def test_account_without_entities_is_skipped(self):
        """An account without an 'entities' key is skipped."""
        entity = self._entity("media_player.echo1", "u1")
        service = self._service(
            {
                "no_entities@example.com": {},
                "test@example.com": {"entities": {"media_player": {"s1": entity}}},
            }
        )

        assert service.targets == {"echo1": "u1"}

    def test_none_entities_are_skipped(self):
        """None entities and entities with no entity_id are skipped."""
        good = self._entity("media_player.echo1", "u1")
        no_id = self._entity(None, "u2")
        service = self._service(
            {
                "test@example.com": {
                    "entities": {"media_player": {"s0": None, "s1": no_id, "s2": good}}
                }
            }
        )

        assert service.targets == {"echo1": "u1"}

    def test_last_called_entity_with_numeric_name_suffix(self):
        """A last_called name ending in a digit gets an email-suffixed key."""
        entity = self._entity("media_player.echo1", "u1", last_called=True, ts="100")
        service = self._service(
            {"test@example.com": {"entities": {"media_player": {"s1": entity}}}}
        )

        result = service.targets

        assert result["echo1"] == "u1"
        assert result["last_called_test@example.com"] == "u1"

    def test_last_called_entity_with_non_numeric_name(self):
        """A last_called name not ending in a digit uses the bare key."""
        entity = self._entity("media_player.kitchen", "u1", last_called=True, ts="50")
        service = self._service(
            {"test@example.com": {"entities": {"media_player": {"s1": entity}}}}
        )

        result = service.targets

        assert result["last_called"] == "u1"

    def test_last_called_picks_highest_timestamp_with_invalid_values(self):
        """The most-recent last_called wins; invalid timestamps coerce to 0."""
        first = self._entity("media_player.echo1", "u1", last_called=True, ts="abc")
        second = self._entity("media_player.echo2", "u2", last_called=True, ts="100")
        service = self._service(
            {
                "test@example.com": {
                    "entities": {"media_player": {"s1": first, "s2": second}}
                }
            }
        )

        result = service.targets

        # "abc" coerces to 0, so the second entity (ts=100) is selected.
        assert result["last_called_test@example.com"] == "u2"

    def test_last_called_keeps_first_when_new_timestamp_not_greater(self):
        """A later entity with a lower timestamp does not replace the current one."""
        first = self._entity("media_player.echo1", "u1", last_called=True, ts="100")
        second = self._entity("media_player.echo2", "u2", last_called=True, ts="50")
        service = self._service(
            {
                "test@example.com": {
                    "entities": {"media_player": {"s1": first, "s2": second}}
                }
            }
        )

        result = service.targets

        assert result["last_called_test@example.com"] == "u1"

    def test_last_called_ignored_when_disabled(self):
        """No last_called key is added when service.last_called is False."""
        entity = self._entity("media_player.echo1", "u1", last_called=True, ts="100")
        service = self._service(
            {"test@example.com": {"entities": {"media_player": {"s1": entity}}}},
            last_called=False,
        )

        result = service.targets

        assert result == {"echo1": "u1"}
        assert "last_called_test@example.com" not in result


# =============================================================================
# Tests for async_send_message: ATTR_TARGET string (JSON) handling
# =============================================================================


class TestAsyncSendMessageStringTarget:
    """Test JSON-string handling of the ATTR_TARGET keyword."""

    def _service(self):
        hass = MagicMock()
        hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {
                    "test@example.com": {
                        "entities": {"media_player": {}},
                        "options": {},
                    }
                }
            }
        }
        hass.states.get.return_value = None
        service = object.__new__(AlexaNotificationService)
        service.hass = hass
        service.last_called = True
        service.convert = MagicMock(return_value=[])
        return service

    @pytest.mark.asyncio
    async def test_string_target_valid_json_is_parsed(self):
        """A JSON-list string target is decoded and processed."""
        service = self._service()

        await service.async_send_message("hello", target='["media_player.echo1"]')

        service.convert.assert_called_once()
        expanded = service.convert.call_args[0][0]
        assert "media_player.echo1" in expanded

    @pytest.mark.asyncio
    async def test_string_target_invalid_json_aborts(self):
        """A non-JSON string target logs an error and aborts before convert()."""
        service = self._service()

        await service.async_send_message("hello", target="living_room")

        service.convert.assert_not_called()


# =============================================================================
# Tests for async_send_message: per-notification-type dispatch
# =============================================================================


class TestAsyncSendMessageDispatch:
    """Test the tts / announce / push / dropin / unknown dispatch branches."""

    def _alexa(
        self,
        name="Echo 1",
        serial="serial1",
        entity_id="media_player.echo1",
        available=True,
    ):
        alexa = MagicMock()
        alexa.name = name
        alexa.unique_id = serial
        alexa.device_serial_number = serial
        alexa.entity_id = entity_id
        alexa.available = available
        alexa.async_send_tts = AsyncMock()
        alexa.async_send_announcement = AsyncMock()
        alexa.async_send_mobilepush = AsyncMock()
        alexa.async_send_dropin_notification = AsyncMock()
        return alexa

    def _service(self, alexas, options=None):
        hass = MagicMock()
        hass.data = {
            DATA_ALEXAMEDIA: {
                "accounts": {
                    "test@example.com": {
                        "entities": {
                            "media_player": {a.device_serial_number: a for a in alexas}
                        },
                        "options": options or {},
                    }
                }
            }
        }
        hass.states.get.return_value = None
        service = object.__new__(AlexaNotificationService)
        service.hass = hass
        service.last_called = True
        return service

    @pytest.mark.asyncio
    async def test_tts_sends_tts(self):
        """The default type (tts) routes to async_send_tts."""
        alexa = self._alexa()
        service = self._service([alexa])

        await service.async_send_message("hello", target=["media_player.echo1"])

        alexa.async_send_tts.assert_awaited_once()
        args, kwargs = alexa.async_send_tts.call_args
        assert args[0] == "hello"
        assert kwargs["queue_delay"] == DEFAULT_QUEUE_DELAY

    @pytest.mark.asyncio
    async def test_tts_uses_configured_queue_delay(self):
        """The configured CONF_QUEUE_DELAY option overrides the default."""
        alexa = self._alexa()
        service = self._service([alexa], options={CONF_QUEUE_DELAY: 5})

        await service.async_send_message("hello", target=["media_player.echo1"])

        _, kwargs = alexa.async_send_tts.call_args
        assert kwargs["queue_delay"] == 5

    @pytest.mark.asyncio
    async def test_tts_skips_unavailable_device(self):
        """An unavailable device receives no tts."""
        alexa = self._alexa(available=False)
        service = self._service([alexa])

        await service.async_send_message("hello", target=["media_player.echo1"])

        alexa.async_send_tts.assert_not_called()

    @pytest.mark.asyncio
    async def test_tts_skips_unmatched_target(self):
        """A target that matches no device produces no tts."""
        alexa = self._alexa()
        service = self._service([alexa])

        await service.async_send_message("hello", target=["media_player.unknown"])

        alexa.async_send_tts.assert_not_called()

    @pytest.mark.asyncio
    async def test_announce_default_method(self):
        """type=announce routes to async_send_announcement with method='all'."""
        alexa = self._alexa()
        service = self._service([alexa])

        await service.async_send_message(
            "hello",
            target=["media_player.echo1"],
            title="My Title",
            data={"type": "announce"},
        )

        alexa.async_send_announcement.assert_awaited_once()
        _, kwargs = alexa.async_send_announcement.call_args
        assert kwargs["targets"] == ["serial1"]
        assert kwargs["title"] == "My Title"
        assert kwargs["method"] == "all"
        assert kwargs["queue_delay"] == DEFAULT_QUEUE_DELAY

    @pytest.mark.asyncio
    async def test_announce_custom_method(self):
        """A custom data['method'] is forwarded to async_send_announcement."""
        alexa = self._alexa()
        service = self._service([alexa])

        await service.async_send_message(
            "hello",
            target=["media_player.echo1"],
            data={"type": "announce", "method": "speak"},
        )

        _, kwargs = alexa.async_send_announcement.call_args
        assert kwargs["method"] == "speak"

    @pytest.mark.asyncio
    async def test_push_sends_mobilepush(self):
        """type=push routes to async_send_mobilepush."""
        alexa = self._alexa()
        service = self._service([alexa])

        await service.async_send_message(
            "hello",
            target=["media_player.echo1"],
            title="My Title",
            data={"type": "push"},
        )

        alexa.async_send_mobilepush.assert_awaited_once()
        _, kwargs = alexa.async_send_mobilepush.call_args
        assert kwargs["title"] == "My Title"
        assert kwargs["queue_delay"] == DEFAULT_QUEUE_DELAY

    @pytest.mark.asyncio
    async def test_dropin_sends_dropin_notification(self):
        """type=dropin_notification routes to async_send_dropin_notification."""
        alexa = self._alexa()
        service = self._service([alexa])

        await service.async_send_message(
            "hello",
            target=["media_player.echo1"],
            data={"type": "dropin_notification"},
        )

        alexa.async_send_dropin_notification.assert_awaited_once()
        _, kwargs = alexa.async_send_dropin_notification.call_args
        assert kwargs["queue_delay"] == DEFAULT_QUEUE_DELAY

    @pytest.mark.asyncio
    async def test_unknown_type_raises_invalid(self):
        """An unimplemented data['type'] raises voluptuous.Invalid."""
        alexa = self._alexa()
        service = self._service([alexa])

        with pytest.raises(vol.Invalid):
            await service.async_send_message(
                "hello",
                target=["media_player.echo1"],
                data={"type": "bogus"},
            )
