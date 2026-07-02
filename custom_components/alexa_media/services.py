"""
Alexa Services.

SPDX-License-Identifier: Apache-2.0

For more details about this platform, please refer to the documentation at
https://community.home-assistant.io/t/echo-devices-alexa-as-media-player-testers-needed/58639
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import logging
from typing import Any

from alexapy import AlexaAPI, AlexapyLoginError, hide_email
from alexapy.errors import AlexapyConnectionError
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv, entity_registry as er
import voluptuous as vol

from .const import (
    ATTR_EMAIL,
    ATTR_ENTITY_ID,
    ATTR_NUM_ENTRIES,
    DATA_ALEXAMEDIA,
    DOMAIN,
    SERVICE_ENABLE_NETWORK_DISCOVERY,
    SERVICE_FORCE_LOGOUT,
    SERVICE_GET_HISTORY_RECORDS,
    SERVICE_RESTORE_VOLUME,
    SERVICE_UPDATE_LAST_CALLED,
)
from .helpers import _catch_login_errors, report_relogin_required, safe_get

_LOGGER = logging.getLogger(__name__)


FORCE_LOGOUT_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_EMAIL, default=[]): vol.All(cv.ensure_list, [cv.string])}
)
LAST_CALL_UPDATE_SCHEMA = vol.Schema(
    {vol.Optional(ATTR_EMAIL, default=[]): vol.All(cv.ensure_list, [cv.string])}
)
RESTORE_VOLUME_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id})

GET_HISTORY_RECORDS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Optional(ATTR_NUM_ENTRIES, default=5): cv.positive_int,
    }
)

ENABLE_NETWORK_DISCOVERY_SCHEMA = vol.Schema(
    {
        vol.Optional(ATTR_EMAIL, default=[]): vol.All(
            cv.ensure_list,
            [cv.string],
        ),
    }
)


@dataclass(frozen=True)
class AlexaServiceDef:
    """Definition for an Alexa Media custom service."""

    name: str  # service name as exposed in HA: alexa_media.<name>
    schema: vol.Schema  # voluptuous schema
    handler: str  # method name on AlexaMediaServices


SERVICE_DEFS: tuple[AlexaServiceDef, ...] = (
    AlexaServiceDef(
        name=SERVICE_FORCE_LOGOUT,
        schema=FORCE_LOGOUT_SCHEMA,
        handler="force_logout",
    ),
    AlexaServiceDef(
        name=SERVICE_UPDATE_LAST_CALLED,
        schema=LAST_CALL_UPDATE_SCHEMA,
        handler="last_call_handler",
    ),
    AlexaServiceDef(
        name=SERVICE_RESTORE_VOLUME,
        schema=RESTORE_VOLUME_SCHEMA,
        handler="restore_volume",
    ),
    AlexaServiceDef(
        name=SERVICE_GET_HISTORY_RECORDS,
        schema=GET_HISTORY_RECORDS_SCHEMA,
        handler="get_history_records",
    ),
    AlexaServiceDef(
        name=SERVICE_ENABLE_NETWORK_DISCOVERY,
        schema=ENABLE_NETWORK_DISCOVERY_SCHEMA,
        handler="enable_network_discovery",
    ),
)


class AlexaMediaServices:
    def __init__(
        self,
        hass: HomeAssistant,
        functions: dict[str, Callable[..., Any]] | None = None,
    ):
        self.hass = hass
        self._functions = functions or {}

    async def register(self) -> None:
        """Register Alexa Media custom services."""
        for service_def in SERVICE_DEFS:
            handler = getattr(self, service_def.handler)
            self.hass.services.async_register(
                DOMAIN,
                service_def.name,
                handler,
                schema=service_def.schema,
            )

    async def force_logout(self, call: ServiceCall) -> bool:
        """Handle force logout service request.

        Arguments
            call.ATTR_EMAIL {List[str] | None}: List of case-sensitive Alexa emails.
                                                If None, all accounts are logged out.

        Returns
            bool -- True if at least one account was marked for relogin.
        """
        requested_emails = call.data.get(ATTR_EMAIL)
        _LOGGER.debug("Service force_logout called for: %s", requested_emails)

        accounts = self.hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {})
        success = False

        for email, account_dict in accounts.items():
            if requested_emails and email not in requested_emails:
                continue

            login_obj = account_dict["login_obj"]

            # This is the effective “force logout” for this account: mark it as
            # requiring reauthentication and notify the user/UI.
            report_relogin_required(self.hass, login_obj, email)
            success = True
            _LOGGER.debug(
                "Marked Alexa Media account %s for relogin via force_logout service",
                hide_email(email),
            )

        if requested_emails and not success:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_matching_accounts",
                translation_placeholders={"emails": ", ".join(requested_emails)},
            )

        return success

    @_catch_login_errors
    async def last_call_handler(self, call: ServiceCall) -> None:
        """Handle last call service request.

        Arguments
            call.ATTR_EMAIL: {List[str: None]}: List of case-sensitive Alexa emails.
                                                If None, all accounts are updated.
        """
        requested_emails = call.data.get(ATTR_EMAIL)
        accounts = self.hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {})

        if not accounts:
            _LOGGER.debug("update_last_called called but no accounts are loaded")
            return

        _LOGGER.debug("Service update_last_called called for: %s", requested_emails)

        # Prefer an injected closure (legacy per-entry wiring) but fall back to the
        # module-level implementation so the action works when it is registered from
        # async_setup, independently of any config entry.
        injected = self._functions.get("update_last_called")

        for email, account_dict in accounts.items():
            if requested_emails and email not in requested_emails:
                continue

            login_obj = account_dict["login_obj"]

            async def _run_update_last_called(email: str, login_obj) -> None:
                try:
                    if callable(injected):
                        await injected(login_obj)
                    else:
                        from .setup.last_called import _async_update_last_called_global

                        await _async_update_last_called_global(
                            self.hass, login_obj, email
                        )
                except asyncio.CancelledError:
                    raise
                except AlexapyLoginError:
                    report_relogin_required(self.hass, login_obj, email)
                except AlexapyConnectionError:
                    _LOGGER.error(
                        "Unable to connect to Alexa for %s;"
                        " check your network connection and try again",
                        hide_email(email),
                    )
                except Exception:  # pragma: no cover
                    _LOGGER.exception(
                        "Unexpected error updating last_called for %s",
                        hide_email(email),
                    )
                finally:
                    # Clean up task reference when done
                    if email in accounts:
                        accounts[email].pop("service_update_last_called_task", None)

            # Cancel any existing task for this account before creating a new one
            existing_task = account_dict.get("service_update_last_called_task")
            if existing_task and not existing_task.done():
                existing_task.cancel()

            # Store task handle for proper cleanup on unload
            task = self.hass.async_create_task(
                _run_update_last_called(email, login_obj),
                name=f"alexa_media.update_last_called.{hide_email(email)}",
            )
            account_dict["service_update_last_called_task"] = task

    async def restore_volume(self, call: ServiceCall) -> bool:
        """Handle restore volume service request.

        Arguments:
            call.ATTR_ENTITY_ID {str: None} -- Alexa Media Player entity.

        """
        entity_id = call.data.get(ATTR_ENTITY_ID)
        _LOGGER.debug("Service restore_volume called for: %s", entity_id)

        # Retrieve the entity registry and entity entry
        entity_registry = er.async_get(self.hass)
        entity_entry = entity_registry.async_get(entity_id)

        if not entity_entry:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entity_not_found",
                translation_placeholders={"entity_id": entity_id},
            )

        # Retrieve the state and attributes
        state = self.hass.states.get(entity_id)
        if not state:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="entity_no_state",
                translation_placeholders={"entity_id": entity_id},
            )

        previous_volume = state.attributes.get("previous_volume")
        current_volume = state.attributes.get("volume_level")

        if previous_volume is None:
            _LOGGER.warning(
                "Previous volume not found for %s; attempting to use current volume level: %s",
                entity_id,
                current_volume,
            )
            previous_volume = current_volume

        if previous_volume is None:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="no_previous_volume",
                translation_placeholders={"entity_id": entity_id},
            )

        # Call the volume_set service with the retrieved volume
        await self.hass.services.async_call(
            domain="media_player",
            service="volume_set",
            service_data={
                "volume_level": previous_volume,
            },
            target={"entity_id": entity_id},
            blocking=True,
        )

        _LOGGER.debug("Volume restored to %s for entity %s", previous_volume, entity_id)
        return True

    async def get_history_records(self, call: ServiceCall) -> bool:
        """Handle request to get history records and store them on the entity."""
        entity_id = call.data.get(ATTR_ENTITY_ID)
        number_of_entries = call.data.get(ATTR_NUM_ENTRIES)

        # Validate number_of_entries
        try:
            number_of_entries_int = int(number_of_entries)
        except (TypeError, ValueError) as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_entries",
                translation_placeholders={"entries": str(number_of_entries)},
            ) from err

        if number_of_entries_int <= 0:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="invalid_entries",
                translation_placeholders={"entries": str(number_of_entries_int)},
            )

        _LOGGER.debug(
            "Service get_history_records for: %s with %s entries",
            entity_id,
            number_of_entries_int,
        )

        # Validate the target entity
        entity_registry = er.async_get(self.hass)
        entity_entry = entity_registry.async_get(entity_id)
        if not entity_entry or entity_entry.platform != DOMAIN:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="entity_not_alexa_media",
                translation_placeholders={"entity_id": entity_id},
            )
        target_serial_number = entity_entry.unique_id

        history_data_total: list[dict[str, Any]] = []

        async def _collect_history_for_account(login_obj) -> None:
            """Collect history entries for a single account matching the target device."""
            # Get the history records. Input: time_from, time_to (both None here).
            history_data = await AlexaAPI.get_customer_history_records(
                login_obj, None, None
            )
            if not history_data:
                return

            for item in history_data:
                summary = safe_get(item, ["description", "summary"], "")
                device_serial_number = item.get("deviceSerialNumber")
                timestamp = item.get("creationTimestamp")

                if (
                    not summary
                    or summary == ","
                    or device_serial_number != target_serial_number
                    or timestamp is None
                ):
                    continue

                entry = {
                    "timestamp": timestamp,
                    "summary": summary,
                    "response": item.get("alexaResponse", ""),
                }
                history_data_total.append(entry)

        # Iterate accounts and collect history
        accounts = self.hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {})
        for email, account_dict in accounts.items():
            login_obj = account_dict["login_obj"]
            try:
                await _collect_history_for_account(login_obj)
            except AlexapyConnectionError:
                _LOGGER.exception(
                    "Error retrieving history for %s",
                    hide_email(email),
                )
            except AlexapyLoginError:
                _LOGGER.exception(
                    "Login error retrieving history for %s",
                    hide_email(email),
                )
                report_relogin_required(self.hass, login_obj, email)

            except asyncio.CancelledError:
                # Let HA cancellation propagate
                raise
            except Exception:
                # Fallback for truly unexpected errors
                _LOGGER.exception(
                    "Unexpected error retrieving history for %s",
                    hide_email(email),
                )

        # Sort and limit entries
        history_data_total.sort(key=lambda x: x["timestamp"], reverse=True)
        history_data_total = history_data_total[:number_of_entries_int]

        # Update the entity's attributes
        state = self.hass.states.get(entity_id)
        if state is not None:
            new_attributes = dict(state.attributes)
            new_attributes["history_records"] = history_data_total
            self.hass.states.async_set(entity_id, state.state, new_attributes)
            return True

        raise HomeAssistantError(
            translation_domain=DOMAIN,
            translation_key="entity_no_state",
            translation_placeholders={"entity_id": entity_id},
        )

    async def enable_network_discovery(self, call: ServiceCall) -> None:
        """Re-enable network discovery for one or more Alexa accounts."""
        data = call.data or {}
        target_emails: list[str] = data.get(ATTR_EMAIL, [])

        accounts = self.hass.data.get(DATA_ALEXAMEDIA, {}).get("accounts", {})
        any_matched = False

        for email, account_dict in accounts.items():
            if target_emails and email not in target_emails:
                continue

            any_matched = True

            if "should_get_network" not in account_dict:
                _LOGGER.debug(
                    "Account %s has no 'should_get_network' flag; skipping",
                    hide_email(email),
                )
                continue

            account_dict["should_get_network"] = True
            _LOGGER.debug(
                "Re-enabled network discovery for Alexa Media account %s",
                hide_email(email),
            )

        if target_emails and not any_matched:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_matching_accounts",
                translation_placeholders={"emails": ", ".join(target_emails)},
            )
