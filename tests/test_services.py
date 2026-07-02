"""Tests for services.py - registration, handlers and translated error paths."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from alexapy import AlexapyLoginError
from alexapy.errors import AlexapyConnectionError
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


# --------------------------------------------------------------------------- #
# last_call_handler - the scheduled _run_update_last_called coroutine body
# --------------------------------------------------------------------------- #


def _patch_create_task(hass):
    """Make hass.async_create_task spawn a real asyncio task we can await."""
    created: list = []

    def _create(coro, name=None):
        task = asyncio.ensure_future(coro)
        created.append(task)
        return task

    hass.async_create_task.side_effect = _create
    return created


async def test_last_call_handler_runs_injected_update():
    injected = AsyncMock()
    account = {"login_obj": MagicMock()}
    svc, hass = _make_services(
        {"a@example.com": account}, functions={"update_last_called": injected}
    )
    created = _patch_create_task(hass)
    await svc.last_call_handler(_make_call({"email": []}))
    await asyncio.gather(*created)
    injected.assert_awaited_once()
    # finally block cleans up the stored task handle
    assert "service_update_last_called_task" not in account


@patch(
    "custom_components.alexa_media.setup.last_called._async_update_last_called_global",
    new_callable=AsyncMock,
)
async def test_last_call_handler_runs_global_fallback(mock_global):
    account = {"login_obj": MagicMock()}
    svc, hass = _make_services({"a@example.com": account})  # no injected closure
    created = _patch_create_task(hass)
    await svc.last_call_handler(_make_call({"email": []}))
    await asyncio.gather(*created)
    mock_global.assert_awaited_once()
    assert "service_update_last_called_task" not in account


@patch("custom_components.alexa_media.services.report_relogin_required")
async def test_last_call_handler_login_error_reports_relogin(mock_report):
    injected = AsyncMock(side_effect=AlexapyLoginError("nope"))
    account = {"login_obj": MagicMock()}
    svc, hass = _make_services(
        {"a@example.com": account}, functions={"update_last_called": injected}
    )
    created = _patch_create_task(hass)
    await svc.last_call_handler(_make_call({"email": []}))
    await asyncio.gather(*created)
    mock_report.assert_called_once()


async def test_last_call_handler_connection_error_is_caught():
    injected = AsyncMock(side_effect=AlexapyConnectionError())
    account = {"login_obj": MagicMock()}
    svc, hass = _make_services(
        {"a@example.com": account}, functions={"update_last_called": injected}
    )
    created = _patch_create_task(hass)
    await svc.last_call_handler(_make_call({"email": []}))
    await asyncio.gather(*created)  # error is logged, not raised
    injected.assert_awaited_once()
    assert "service_update_last_called_task" not in account


async def test_last_call_handler_cancelled_propagates():
    injected = AsyncMock(side_effect=asyncio.CancelledError())
    account = {"login_obj": MagicMock()}
    svc, hass = _make_services(
        {"a@example.com": account}, functions={"update_last_called": injected}
    )
    created = _patch_create_task(hass)
    await svc.last_call_handler(_make_call({"email": []}))
    with pytest.raises(asyncio.CancelledError):
        await created[0]


async def test_last_call_handler_cancels_existing_running_task():
    existing = MagicMock()
    existing.done.return_value = False
    account = {
        "login_obj": MagicMock(),
        "service_update_last_called_task": existing,
    }
    svc, hass = _make_services({"a@example.com": account})

    def _create_task(coro, name=None):
        coro.close()  # don't run the replacement task
        return MagicMock(done=lambda: True)

    hass.async_create_task.side_effect = _create_task
    await svc.last_call_handler(_make_call({"email": []}))
    existing.cancel.assert_called_once()


# --------------------------------------------------------------------------- #
# get_history_records - empty results, per-account error handling, no state
# --------------------------------------------------------------------------- #


def _history_entry(platform=DOMAIN, unique_id="SERIAL123"):
    entry = MagicMock()
    entry.platform = platform
    entry.unique_id = unique_id
    return entry


@patch("custom_components.alexa_media.services.AlexaAPI")
@patch("custom_components.alexa_media.services.er")
async def test_get_history_empty_records_returns_early(mock_er, mock_api):
    mock_er.async_get.return_value.async_get.return_value = _history_entry()
    mock_api.get_customer_history_records = AsyncMock(return_value=[])
    svc, hass = _make_services({"a@example.com": {"login_obj": MagicMock()}})
    state = MagicMock()
    state.state = "idle"
    state.attributes = {}
    hass.states.get.return_value = state
    result = await svc.get_history_records(
        _make_call({"entity_id": "media_player.x", "entries": 5})
    )
    assert result is True
    new_attributes = hass.states.async_set.call_args.args[2]
    assert new_attributes["history_records"] == []


@patch("custom_components.alexa_media.services.AlexaAPI")
@patch("custom_components.alexa_media.services.er")
async def test_get_history_connection_error_is_logged(mock_er, mock_api):
    mock_er.async_get.return_value.async_get.return_value = _history_entry()
    mock_api.get_customer_history_records = AsyncMock(
        side_effect=AlexapyConnectionError()
    )
    svc, hass = _make_services({"a@example.com": {"login_obj": MagicMock()}})
    state = MagicMock()
    state.state = "idle"
    state.attributes = {}
    hass.states.get.return_value = state
    result = await svc.get_history_records(
        _make_call({"entity_id": "media_player.x", "entries": 5})
    )
    assert result is True


@patch("custom_components.alexa_media.services.report_relogin_required")
@patch("custom_components.alexa_media.services.AlexaAPI")
@patch("custom_components.alexa_media.services.er")
async def test_get_history_login_error_reports_relogin(mock_er, mock_api, mock_report):
    mock_er.async_get.return_value.async_get.return_value = _history_entry()
    mock_api.get_customer_history_records = AsyncMock(side_effect=AlexapyLoginError())
    svc, hass = _make_services({"a@example.com": {"login_obj": MagicMock()}})
    state = MagicMock()
    state.state = "idle"
    state.attributes = {}
    hass.states.get.return_value = state
    result = await svc.get_history_records(
        _make_call({"entity_id": "media_player.x", "entries": 5})
    )
    assert result is True
    mock_report.assert_called_once()


@patch("custom_components.alexa_media.services.AlexaAPI")
@patch("custom_components.alexa_media.services.er")
async def test_get_history_cancelled_propagates(mock_er, mock_api):
    mock_er.async_get.return_value.async_get.return_value = _history_entry()
    mock_api.get_customer_history_records = AsyncMock(
        side_effect=asyncio.CancelledError()
    )
    svc, _ = _make_services({"a@example.com": {"login_obj": MagicMock()}})
    with pytest.raises(asyncio.CancelledError):
        await svc.get_history_records(
            _make_call({"entity_id": "media_player.x", "entries": 5})
        )


@patch("custom_components.alexa_media.services.AlexaAPI")
@patch("custom_components.alexa_media.services.er")
async def test_get_history_unexpected_error_is_logged(mock_er, mock_api):
    mock_er.async_get.return_value.async_get.return_value = _history_entry()
    mock_api.get_customer_history_records = AsyncMock(side_effect=ValueError("boom"))
    svc, hass = _make_services({"a@example.com": {"login_obj": MagicMock()}})
    state = MagicMock()
    state.state = "idle"
    state.attributes = {}
    hass.states.get.return_value = state
    result = await svc.get_history_records(
        _make_call({"entity_id": "media_player.x", "entries": 5})
    )
    assert result is True


@patch("custom_components.alexa_media.services.er")
async def test_get_history_no_state_raises(mock_er):
    mock_er.async_get.return_value.async_get.return_value = _history_entry()
    svc, hass = _make_services({})  # no accounts -> history loop is skipped
    hass.states.get.return_value = None
    with pytest.raises(HomeAssistantError):
        await svc.get_history_records(
            _make_call({"entity_id": "media_player.x", "entries": 5})
        )
