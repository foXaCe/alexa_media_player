"""Tests for switch.py - specifically the hue_emulated_enabled bugfix.

This tests the fix for the undefined variable bug where hue_emulated_enabled
was used but never defined, causing a NameError when users had Smart Plug
devices with CONF_EXTENDED_ENTITY_DISCOVERY enabled.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from homeassistant.exceptions import ConfigEntryNotReady, NoEntitySpecifiedError
from homeassistant.helpers.entity import EntityCategory
import pytest

from custom_components.alexa_media.const import (
    CONF_EXTENDED_ENTITY_DISCOVERY,
    DATA_ALEXAMEDIA,
)
from custom_components.alexa_media.switch import (
    AlexaMediaSwitch,
    DNDSwitch,
    RepeatSwitch,
    ShuffleSwitch,
    async_setup_entry,
    async_setup_platform,
)

_EMAIL = "test@example.com"  # noqa: S105


@pytest.fixture
def mock_hass():
    """Create a mock hass object."""
    hass = MagicMock()
    hass.config.as_dict.return_value = {"components": set()}
    return hass


@pytest.fixture
def mock_hass_with_emulated_hue():
    """Create a mock hass object with emulated_hue enabled."""
    hass = MagicMock()
    hass.config.as_dict.return_value = {"components": {"emulated_hue"}}
    return hass


@pytest.fixture
def mock_account_dict():
    """Create a mock account dictionary with smart switch entities."""
    coordinator = MagicMock()
    login_obj = MagicMock()
    return {
        "devices": {
            "media_player": {},
            "smart_switch": [
                {"id": "switch1", "name": "Smart Plug 1", "is_hue_v1": False},
                {"id": "switch2", "name": "Smart Plug 2", "is_hue_v1": True},
            ],
        },
        "entities": {
            "switch": {},
            "media_player": {},
            "smart_switch": [],
        },
        "options": {CONF_EXTENDED_ENTITY_DISCOVERY: True},
        "coordinator": coordinator,
        "login_obj": login_obj,
    }


class TestHueEmulatedEnabledVariable:
    """Tests for the hue_emulated_enabled variable definition fix."""

    @pytest.mark.asyncio
    async def test_no_name_error_with_smart_switches(
        self, mock_hass, mock_account_dict
    ):
        """Verify that hue_emulated_enabled is defined before use.

        Before the fix, accessing smart_switch devices with
        CONF_EXTENDED_ENTITY_DISCOVERY would raise a NameError because
        hue_emulated_enabled was used but never defined.
        """
        email = "test@example.com"  # noqa: S105
        mock_hass.data = {DATA_ALEXAMEDIA: {"accounts": {email: mock_account_dict}}}

        with patch(
            "custom_components.alexa_media.switch.add_devices",
            new_callable=AsyncMock,
        ) as mock_add_devices:
            mock_add_devices.return_value = True

            from custom_components.alexa_media.switch import async_setup_platform

            # This should not raise NameError: name 'hue_emulated_enabled' is not defined
            config = {"email": email}
            result = await async_setup_platform(
                mock_hass, config, MagicMock(), discovery_info=None
            )

            assert result is True
            mock_add_devices.assert_called_once()


class TestSmartSwitchCreation:
    """Tests for Smart Switch entity creation with emulated_hue filtering."""

    @pytest.mark.asyncio
    async def test_non_hue_v1_switch_always_created(
        self, mock_hass_with_emulated_hue, mock_account_dict
    ):
        """Non-Hue-v1 switches are created regardless of emulated_hue setting.

        Even when emulated_hue is enabled, switches that are not marked as
        is_hue_v1=True should be created normally.
        """
        email = "test@example.com"  # noqa: S105
        # Only keep the non-hue-v1 switch
        mock_account_dict["devices"]["smart_switch"] = [
            {"id": "switch1", "name": "Smart Plug 1", "is_hue_v1": False}
        ]
        mock_hass_with_emulated_hue.data = {
            DATA_ALEXAMEDIA: {"accounts": {email: mock_account_dict}}
        }

        with patch(
            "custom_components.alexa_media.switch.add_devices",
            new_callable=AsyncMock,
        ) as mock_add_devices:
            mock_add_devices.return_value = True

            from custom_components.alexa_media.switch import async_setup_platform

            config = {"email": email}
            await async_setup_platform(
                mock_hass_with_emulated_hue, config, MagicMock(), discovery_info=None
            )

            # Verify the SmartSwitch was added to account_dict entities
            assert len(mock_account_dict["entities"]["smart_switch"]) == 1

    @pytest.mark.asyncio
    async def test_hue_v1_switch_skipped_when_emulated_hue_enabled(
        self, mock_hass_with_emulated_hue, mock_account_dict
    ):
        """Hue-v1 switches are skipped when emulated_hue is active.

        When emulated_hue is in the components list and a switch has
        is_hue_v1=True, it should be skipped to avoid duplicates with
        the emulated_hue integration.
        """
        email = "test@example.com"  # noqa: S105
        # Only keep the hue-v1 switch
        mock_account_dict["devices"]["smart_switch"] = [
            {"id": "switch2", "name": "Hue Plug", "is_hue_v1": True}
        ]
        mock_hass_with_emulated_hue.data = {
            DATA_ALEXAMEDIA: {"accounts": {email: mock_account_dict}}
        }

        with patch(
            "custom_components.alexa_media.switch.add_devices",
            new_callable=AsyncMock,
        ) as mock_add_devices:
            mock_add_devices.return_value = True

            from custom_components.alexa_media.switch import async_setup_platform

            config = {"email": email}
            await async_setup_platform(
                mock_hass_with_emulated_hue, config, MagicMock(), discovery_info=None
            )

            # Verify the hue-v1 switch was NOT added to smart_switch entities
            assert len(mock_account_dict["entities"]["smart_switch"]) == 0

    @pytest.mark.asyncio
    async def test_hue_v1_switch_created_when_emulated_hue_not_enabled(
        self, mock_hass, mock_account_dict
    ):
        """Hue-v1 switches are created when emulated_hue is not active.

        When emulated_hue is NOT in the components list, even switches with
        is_hue_v1=True should be created since there's no conflict.
        """
        email = "test@example.com"  # noqa: S105
        # Only keep the hue-v1 switch
        mock_account_dict["devices"]["smart_switch"] = [
            {"id": "switch2", "name": "Hue Plug", "is_hue_v1": True}
        ]
        mock_hass.data = {DATA_ALEXAMEDIA: {"accounts": {email: mock_account_dict}}}

        with patch(
            "custom_components.alexa_media.switch.add_devices",
            new_callable=AsyncMock,
        ) as mock_add_devices:
            mock_add_devices.return_value = True

            from custom_components.alexa_media.switch import async_setup_platform

            config = {"email": email}
            await async_setup_platform(
                mock_hass, config, MagicMock(), discovery_info=None
            )

            # Verify the hue-v1 switch WAS added when emulated_hue is not enabled
            assert len(mock_account_dict["entities"]["smart_switch"]) == 1

    @pytest.mark.asyncio
    async def test_no_smart_switches_when_extended_discovery_disabled(
        self, mock_hass, mock_account_dict
    ):
        """No smart switches created when CONF_EXTENDED_ENTITY_DISCOVERY is False.

        Smart switch creation should be skipped entirely when the extended
        entity discovery option is disabled.
        """
        email = "test@example.com"  # noqa: S105
        mock_account_dict["options"][CONF_EXTENDED_ENTITY_DISCOVERY] = False
        mock_hass.data = {DATA_ALEXAMEDIA: {"accounts": {email: mock_account_dict}}}

        with patch(
            "custom_components.alexa_media.switch.add_devices",
            new_callable=AsyncMock,
        ) as mock_add_devices:
            mock_add_devices.return_value = True

            from custom_components.alexa_media.switch import async_setup_platform

            config = {"email": email}
            await async_setup_platform(
                mock_hass, config, MagicMock(), discovery_info=None
            )

            # No smart switches should be added
            assert len(mock_account_dict["entities"]["smart_switch"]) == 0


class TestHueEmulatedDetection:
    """Tests for emulated_hue detection logic."""

    def test_emulated_hue_detected_in_components(self):
        """Verify emulated_hue is correctly detected in hass components.

        The fix checks for 'emulated_hue' in hass.config.as_dict().get('components').
        This tests the detection logic directly.
        """
        # Simulate hass.config.as_dict() return value
        config_with_hue = {"components": {"emulated_hue", "other_component"}}
        config_without_hue = {"components": {"other_component"}}
        config_empty = {"components": set()}
        config_missing = {}

        # With emulated_hue
        hue_enabled = "emulated_hue" in config_with_hue.get("components", set())
        assert hue_enabled is True

        # Without emulated_hue
        hue_enabled = "emulated_hue" in config_without_hue.get("components", set())
        assert hue_enabled is False

        # Empty components
        hue_enabled = "emulated_hue" in config_empty.get("components", set())
        assert hue_enabled is False

        # Missing components key
        hue_enabled = "emulated_hue" in config_missing.get("components", set())
        assert hue_enabled is False


def _client(*, value=True, unique_id="SN1"):
    """Build a media-player-like client for the switch entity constructors."""
    client = MagicMock()
    client._login = MagicMock()
    client._login.email = _EMAIL
    client.unique_id = unique_id
    client.device_serial_number = unique_id
    client.available = True
    client.assumed_state = False
    client.dnd_state = value
    client.shuffle = value
    client.repeat_state = value
    return client


def _account_with_media_players(
    mp_devices, mp_entities, *, switch_entities=None, switch_devices=None
):
    """Build a minimal account_dict for async_setup_platform."""
    return {
        "devices": {
            "media_player": mp_devices,
            "switch": switch_devices or {},
            "smart_switch": [],
        },
        "entities": {
            "media_player": mp_entities,
            "switch": {} if switch_entities is None else switch_entities,
            "smart_switch": [],
        },
        "options": {CONF_EXTENDED_ENTITY_DISCOVERY: False},
        "coordinator": MagicMock(),
        "login_obj": MagicMock(),
    }


class TestSetupPlatformEntities:
    """Tests for the media-player loop and entry points of async_setup_platform."""

    @pytest.mark.asyncio
    async def test_setup_creates_dnd_shuffle_repeat(self, mock_hass):
        """A fully-capable media player yields DND, shuffle and repeat switches."""
        account = _account_with_media_players(
            mp_devices={"SN1": {"capabilities": {"MUSIC_SKILL": {}}}},
            mp_entities={"SN1": _client(unique_id="SN1")},
            switch_devices={"SN1": {"dnd": True}},
        )
        # First-time setup: the per-account "switch" bucket does not exist yet.
        del account["entities"]["switch"]
        mock_hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: account}}}

        with patch(
            "custom_components.alexa_media.switch.add_devices",
            new_callable=AsyncMock,
        ) as mock_add_devices:
            mock_add_devices.return_value = True
            result = await async_setup_platform(
                mock_hass, {"email": _EMAIL}, MagicMock(), discovery_info=None
            )

        assert result is True
        assert sorted(account["entities"]["switch"]["SN1"]) == [
            "dnd",
            "repeat",
            "shuffle",
        ]
        # add_devices receives the three freshly-built entities.
        assert len(mock_add_devices.call_args.args[1]) == 3

    @pytest.mark.asyncio
    async def test_setup_skips_unsupported_and_reuses_existing(self, mock_hass):
        """Unsupported devices create no switches; already-loaded ones are reused."""
        existing = MagicMock()
        account = _account_with_media_players(
            mp_devices={
                # No DND and no MUSIC_SKILL -> every switch type is skipped.
                "SN2": {"capabilities": {}},
                # Already present in entities -> reuse branch.
                "SN3": {"capabilities": {"MUSIC_SKILL": {}}},
            },
            mp_entities={
                "SN2": _client(unique_id="SN2"),
                "SN3": _client(unique_id="SN3"),
            },
            switch_entities={"SN3": {"dnd": existing}},
        )
        mock_hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: account}}}

        with patch(
            "custom_components.alexa_media.switch.add_devices",
            new_callable=AsyncMock,
        ) as mock_add_devices:
            mock_add_devices.return_value = True
            result = await async_setup_platform(
                mock_hass, {"email": _EMAIL}, MagicMock(), discovery_info=None
            )

        assert result is True
        assert account["entities"]["switch"]["SN2"] == {}
        assert account["entities"]["switch"]["SN3"] == {"dnd": existing}
        assert len(mock_add_devices.call_args.args[1]) == 0

    @pytest.mark.asyncio
    async def test_setup_raises_when_media_player_entity_missing(self, mock_hass):
        """A device without its media_player entity yet delays the load."""
        account = _account_with_media_players(
            mp_devices={"SN1": {"capabilities": {}}},
            mp_entities={},  # SN1 not loaded yet
        )
        mock_hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: account}}}

        with (
            patch(
                "custom_components.alexa_media.switch.add_devices",
                new_callable=AsyncMock,
            ),
            pytest.raises(ConfigEntryNotReady),
        ):
            await async_setup_platform(
                mock_hass, {"email": _EMAIL}, MagicMock(), discovery_info=None
            )

    @pytest.mark.asyncio
    async def test_setup_reads_account_from_discovery_info(self, mock_hass):
        """When config has no email, the account comes from discovery_info."""
        account = _account_with_media_players({}, {})
        mock_hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: account}}}

        with patch(
            "custom_components.alexa_media.switch.add_devices",
            new_callable=AsyncMock,
        ) as mock_add_devices:
            mock_add_devices.return_value = True
            result = await async_setup_platform(
                mock_hass,
                {},
                MagicMock(),
                discovery_info={"config": {"email": _EMAIL}},
            )

        assert result is True

    @pytest.mark.asyncio
    async def test_setup_raises_without_account(self, mock_hass):
        """No email anywhere -> ConfigEntryNotReady."""
        mock_hass.data = {DATA_ALEXAMEDIA: {"accounts": {}}}

        with pytest.raises(ConfigEntryNotReady):
            await async_setup_platform(mock_hass, {}, MagicMock(), discovery_info=None)

    @pytest.mark.asyncio
    async def test_async_setup_entry_delegates_to_platform(self, mock_hass):
        """async_setup_entry forwards config_entry.data to async_setup_platform."""
        account = _account_with_media_players({}, {})
        mock_hass.data = {DATA_ALEXAMEDIA: {"accounts": {_EMAIL: account}}}
        entry = MagicMock()
        entry.data = {"email": _EMAIL}

        with patch(
            "custom_components.alexa_media.switch.add_devices",
            new_callable=AsyncMock,
        ) as mock_add_devices:
            mock_add_devices.return_value = True
            result = await async_setup_entry(mock_hass, entry, MagicMock())

        assert result is True


class TestAlexaMediaSwitchLifecycle:
    """Tests for entity lifecycle, event handling and base properties."""

    @pytest.mark.asyncio
    async def test_added_to_hass_connects_and_removal_disconnects(self):
        """async_added_to_hass registers a dispatcher listener that removal calls."""
        switch = ShuffleSwitch(_client())
        switch.hass = MagicMock()
        listener = MagicMock()

        with patch(
            "custom_components.alexa_media.switch.async_dispatcher_connect",
            return_value=listener,
        ) as mock_connect:
            await switch.async_added_to_hass()

        assert switch._listener is listener
        mock_connect.assert_called_once()

        await switch.async_will_remove_from_hass()
        listener.assert_called_once()

    def test_base_handle_event_queue_state(self):
        """The base _handle_event refreshes only on a matching device serial."""
        switch = ShuffleSwitch(_client(unique_id="SN1"))
        switch.schedule_update_ha_state = MagicMock()

        switch._handle_event(
            {"queue_state": {"dopplerId": {"deviceSerialNumber": "SN1"}}}
        )
        switch.schedule_update_ha_state.assert_called_once()

        switch.schedule_update_ha_state.reset_mock()
        switch._handle_event(
            {"queue_state": {"dopplerId": {"deviceSerialNumber": "OTHER"}}}
        )
        switch.schedule_update_ha_state.assert_not_called()

    @pytest.mark.asyncio
    async def test_async_update_schedules_and_ignores_no_entity(self):
        """async_update schedules a refresh and swallows the startup race error."""
        switch = DNDSwitch(_client())
        switch.schedule_update_ha_state = MagicMock()
        await switch.async_update()
        switch.schedule_update_ha_state.assert_called_once()

        racey = DNDSwitch(_client())
        racey.schedule_update_ha_state = MagicMock(side_effect=NoEntitySpecifiedError)
        # Must not raise despite the NoEntitySpecifiedError.
        await racey.async_update()

    def test_base_icon_and_repeat_entity_category(self):
        """The base icon resolves to None and repeat is a CONFIG entity."""
        base = AlexaMediaSwitch(_client(), "dnd_state", "set_dnd_state", "x")
        assert base.icon is None
        assert RepeatSwitch(_client()).entity_category == EntityCategory.CONFIG


class TestSwitchEnabledGuards:
    """Tests for the `self.enabled` guard branches shared across the entity."""

    @staticmethod
    def _disable(switch):
        switch.registry_entry = SimpleNamespace(disabled=True)
        return switch

    @pytest.mark.asyncio
    async def test_guards_short_circuit_when_disabled(self):
        """Disabled entities skip event handling, command and update work."""
        shuffle = self._disable(ShuffleSwitch(_client(unique_id="SN1")))
        shuffle.schedule_update_ha_state = MagicMock()
        shuffle._handle_event(
            {"queue_state": {"dopplerId": {"deviceSerialNumber": "SN1"}}}
        )
        shuffle.schedule_update_ha_state.assert_not_called()

        dnd = self._disable(DNDSwitch(_client()))
        dnd.alexa_api = MagicMock()
        dnd.alexa_api.set_dnd_state = AsyncMock()
        await dnd._set_switch(True)
        dnd.alexa_api.set_dnd_state.assert_not_awaited()

        updater = self._disable(DNDSwitch(_client()))
        updater.schedule_update_ha_state = MagicMock()
        await updater.async_update()
        updater.schedule_update_ha_state.assert_not_called()

        dnd_event = self._disable(DNDSwitch(_client(value=False)))
        dnd_event.schedule_update_ha_state = MagicMock()
        dnd_event._handle_event(
            {"dnd_update": [{"deviceSerialNumber": "SN1", "enabled": True}]}
        )
        dnd_event.schedule_update_ha_state.assert_not_called()

        added = self._disable(ShuffleSwitch(_client()))
        added.hass = MagicMock()
        with patch(
            "custom_components.alexa_media.switch.async_dispatcher_connect"
        ) as mock_connect:
            await added.async_added_to_hass()
        mock_connect.assert_not_called()

    @pytest.mark.asyncio
    async def test_guards_swallow_attribute_error(self):
        """If `.enabled` raises AttributeError, each guard swallows it and proceeds."""
        with (
            patch.object(
                AlexaMediaSwitch,
                "enabled",
                new_callable=PropertyMock,
                side_effect=AttributeError,
            ),
            patch.object(
                AlexaMediaSwitch,
                "name",
                new_callable=PropertyMock,
                return_value="Switch",
            ),
        ):
            shuffle = ShuffleSwitch(_client(unique_id="SN1"))
            shuffle.schedule_update_ha_state = MagicMock()
            shuffle._handle_event(
                {"queue_state": {"dopplerId": {"deviceSerialNumber": "SN1"}}}
            )
            shuffle.schedule_update_ha_state.assert_called_once()

            client = _client(value=False)
            dnd = DNDSwitch(client)
            dnd.alexa_api = MagicMock()
            dnd.alexa_api.set_dnd_state = AsyncMock(return_value=True)
            dnd.schedule_update_ha_state = MagicMock()
            await dnd._set_switch(True)
            assert client.dnd_state is True

            updater = DNDSwitch(_client())
            updater.schedule_update_ha_state = MagicMock()
            await updater.async_update()
            updater.schedule_update_ha_state.assert_called_once()

            dnd_client = _client(value=False)
            dnd_event = DNDSwitch(dnd_client)
            dnd_event.schedule_update_ha_state = MagicMock()
            dnd_event._handle_event(
                {"dnd_update": [{"deviceSerialNumber": "SN1", "enabled": True}]}
            )
            assert dnd_client.dnd_state is True

            added = ShuffleSwitch(_client())
            added.hass = MagicMock()
            listener = MagicMock()
            with patch(
                "custom_components.alexa_media.switch.async_dispatcher_connect",
                return_value=listener,
            ):
                await added.async_added_to_hass()
            assert added._listener is listener
