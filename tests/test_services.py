"""Tests for services.py - registration, handlers and translated error paths."""

from unittest.mock import AsyncMock, MagicMock, patch

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
import pytest

from custom_components.alexa_media.const import DATA_ALEXAMEDIA, DOMAIN
from custom_components.alexa_media.services import SERVICE_DEFS, AlexaMediaServices


def _make_hass(accounts=None):
    """Build a lightweight mock hass with the bits services.py touches."""
    hass = MagicMock()
    hass.data = {DATA_ALEXAMEDIA: {"accounts": accounts or {}}}
    hass.services.async_register = MagicMock()
    hass.services.async_remove = MagicMock()
    hass.services.async_call = AsyncMock()
    return hass


def _make_call(data):
    call = MagicMock()
    call.data = data
    return call


def _make_services(accounts=None, functions=None):
    hass = _make_hass(accounts)
    return AlexaMediaServices(hass, functions=functions or {}), hass


# --------------------------------------------------------------------------- #
# register / unregister
# --------------------------------------------------------------------------- #


async def test_register_registers_every_service():
    svc, hass = _make_services()
    await svc.register()
    assert hass.services.async_register.call_count == len(SERVICE_DEFS)
    registered = {call.args[1] for call in hass.services.async_register.call_args_list}
    assert registered == {definition.name for definition in SERVICE_DEFS}


async def test_unregister_removes_every_service():
    svc, hass = _make_services()
    await svc.unregister()
    assert hass.services.async_remove.call_count == len(SERVICE_DEFS)


# --------------------------------------------------------------------------- #
# force_logout
# --------------------------------------------------------------------------- #


@patch("custom_components.alexa_media.services.report_relogin_required")
async def test_force_logout_all_accounts(mock_report):
    svc, _ = _make_services({"a@example.com": {"login_obj": MagicMock()}})
    result = await svc.force_logout(_make_call({"email": []}))
    assert result is True
    mock_report.assert_called_once()


@patch("custom_components.alexa_media.services.report_relogin_required")
async def test_force_logout_specific_match(mock_report):
    svc, _ = _make_services({"a@example.com": {"login_obj": MagicMock()}})
    result = await svc.force_logout(_make_call({"email": ["a@example.com"]}))
    assert result is True
    mock_report.assert_called_once()


@patch("custom_components.alexa_media.services.report_relogin_required")
async def test_force_logout_no_match_raises(mock_report):
    svc, _ = _make_services({"a@example.com": {"login_obj": MagicMock()}})
    with pytest.raises(ServiceValidationError):
        await svc.force_logout(_make_call({"email": ["other@example.com"]}))
    mock_report.assert_not_called()


# --------------------------------------------------------------------------- #
# restore_volume
# --------------------------------------------------------------------------- #


@patch("custom_components.alexa_media.services.er")
async def test_restore_volume_entity_not_found_raises(mock_er):
    mock_er.async_get.return_value.async_get.return_value = None
    svc, _ = _make_services()
    with pytest.raises(ServiceValidationError):
        await svc.restore_volume(_make_call({"entity_id": "media_player.x"}))


@patch("custom_components.alexa_media.services.er")
async def test_restore_volume_no_state_raises(mock_er):
    mock_er.async_get.return_value.async_get.return_value = MagicMock()
    svc, hass = _make_services()
    hass.states.get.return_value = None
    with pytest.raises(HomeAssistantError):
        await svc.restore_volume(_make_call({"entity_id": "media_player.x"}))


@patch("custom_components.alexa_media.services.er")
async def test_restore_volume_uses_previous_volume(mock_er):
    mock_er.async_get.return_value.async_get.return_value = MagicMock()
    svc, hass = _make_services()
    state = MagicMock()
    state.attributes = {"previous_volume": 0.4, "volume_level": 0.9}
    hass.states.get.return_value = state
    result = await svc.restore_volume(_make_call({"entity_id": "media_player.x"}))
    assert result is True
    hass.services.async_call.assert_awaited_once()
    assert hass.services.async_call.await_args.kwargs["service_data"][
        "volume_level"
    ] == pytest.approx(0.4)


@patch("custom_components.alexa_media.services.er")
async def test_restore_volume_falls_back_to_current_volume(mock_er):
    mock_er.async_get.return_value.async_get.return_value = MagicMock()
    svc, hass = _make_services()
    state = MagicMock()
    state.attributes = {"previous_volume": None, "volume_level": 0.7}
    hass.states.get.return_value = state
    result = await svc.restore_volume(_make_call({"entity_id": "media_player.x"}))
    assert result is True
    assert hass.services.async_call.await_args.kwargs["service_data"][
        "volume_level"
    ] == pytest.approx(0.7)


@patch("custom_components.alexa_media.services.er")
async def test_restore_volume_no_volume_raises(mock_er):
    mock_er.async_get.return_value.async_get.return_value = MagicMock()
    svc, hass = _make_services()
    state = MagicMock()
    state.attributes = {"previous_volume": None, "volume_level": None}
    hass.states.get.return_value = state
    with pytest.raises(HomeAssistantError):
        await svc.restore_volume(_make_call({"entity_id": "media_player.x"}))


# --------------------------------------------------------------------------- #
# get_history_records
# --------------------------------------------------------------------------- #


async def test_get_history_invalid_entries_raises():
    svc, _ = _make_services()
    with pytest.raises(ServiceValidationError):
        await svc.get_history_records(
            _make_call({"entity_id": "media_player.x", "entries": "not-a-number"})
        )


async def test_get_history_non_positive_entries_raises():
    svc, _ = _make_services()
    with pytest.raises(ServiceValidationError):
        await svc.get_history_records(
            _make_call({"entity_id": "media_player.x", "entries": 0})
        )


@patch("custom_components.alexa_media.services.er")
async def test_get_history_wrong_platform_raises(mock_er):
    entry = MagicMock()
    entry.platform = "some_other_domain"
    mock_er.async_get.return_value.async_get.return_value = entry
    svc, _ = _make_services()
    with pytest.raises(ServiceValidationError):
        await svc.get_history_records(
            _make_call({"entity_id": "media_player.x", "entries": 5})
        )


@patch("custom_components.alexa_media.services.AlexaAPI")
@patch("custom_components.alexa_media.services.er")
async def test_get_history_success(mock_er, mock_api):
    entry = MagicMock()
    entry.platform = DOMAIN
    entry.unique_id = "SERIAL123"
    mock_er.async_get.return_value.async_get.return_value = entry
    mock_api.get_customer_history_records = AsyncMock(
        return_value=[
            {
                "description": {"summary": "play some music"},
                "deviceSerialNumber": "SERIAL123",
                "creationTimestamp": 1000,
                "alexaResponse": "Playing",
            },
            {
                "description": {"summary": "ignored other device"},
                "deviceSerialNumber": "OTHER",
                "creationTimestamp": 1001,
                "alexaResponse": "",
            },
        ]
    )
    svc, hass = _make_services({"a@example.com": {"login_obj": MagicMock()}})
    state = MagicMock()
    state.state = "playing"
    state.attributes = {}
    hass.states.get.return_value = state
    result = await svc.get_history_records(
        _make_call({"entity_id": "media_player.x", "entries": 5})
    )
    assert result is True
    hass.states.async_set.assert_called_once()
    new_attributes = hass.states.async_set.call_args.args[2]
    assert len(new_attributes["history_records"]) == 1
    assert new_attributes["history_records"][0]["summary"] == "play some music"


# --------------------------------------------------------------------------- #
# enable_network_discovery
# --------------------------------------------------------------------------- #


async def test_enable_network_discovery_sets_flag():
    account = {"login_obj": MagicMock(), "should_get_network": False}
    svc, _ = _make_services({"a@example.com": account})
    await svc.enable_network_discovery(_make_call({"email": []}))
    assert account["should_get_network"] is True


async def test_enable_network_discovery_skips_account_without_flag():
    account = {"login_obj": MagicMock()}  # no should_get_network key
    svc, _ = _make_services({"a@example.com": account})
    await svc.enable_network_discovery(_make_call({"email": []}))
    assert "should_get_network" not in account


async def test_enable_network_discovery_no_match_raises():
    account = {"login_obj": MagicMock(), "should_get_network": False}
    svc, _ = _make_services({"a@example.com": account})
    with pytest.raises(ServiceValidationError):
        await svc.enable_network_discovery(
            _make_call({"email": ["nobody@example.com"]})
        )


# --------------------------------------------------------------------------- #
# last_call_handler (decorated)
# --------------------------------------------------------------------------- #


async def test_last_call_handler_no_accounts_is_noop():
    svc, hass = _make_services({})  # no accounts loaded
    await svc.last_call_handler(_make_call({"email": []}))
    hass.async_create_task.assert_not_called()


async def test_last_call_handler_schedules_task_per_account():
    scheduled = []

    def _create_task(coro, name=None):
        coro.close()  # avoid "coroutine was never awaited" warnings
        scheduled.append(name)
        return MagicMock(done=lambda: True)

    svc, hass = _make_services({"a@example.com": {"login_obj": MagicMock()}})
    hass.async_create_task.side_effect = _create_task
    await svc.last_call_handler(_make_call({"email": []}))
    assert len(scheduled) == 1


async def test_last_call_handler_filters_by_requested_email():
    scheduled = []

    def _create_task(coro, name=None):
        coro.close()
        scheduled.append(name)
        return MagicMock(done=lambda: True)

    accounts = {
        "a@example.com": {"login_obj": MagicMock()},
        "b@example.com": {"login_obj": MagicMock()},
    }
    svc, hass = _make_services(accounts)
    hass.async_create_task.side_effect = _create_task
    await svc.last_call_handler(_make_call({"email": ["a@example.com"]}))
    assert len(scheduled) == 1
