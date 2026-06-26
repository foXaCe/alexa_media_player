"""Bluetooth state synchronisation for Alexa Media Player.

Extracted from the monolithic ``setup_alexa`` function. Behaviour is unchanged;
``login_obj`` stays the first positional argument because ``@_catch_login_errors``
inspects ``args[0]`` to detect the :class:`~alexapy.AlexaLogin` instance.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from alexapy import AlexaAPI, hide_serial

from ..const import DATA_ALEXAMEDIA
from ..helpers import _catch_login_errors, hide_email

if TYPE_CHECKING:
    from alexapy import AlexaLogin

    from .context import SetupContext

_LOGGER = logging.getLogger(__name__)


@_catch_login_errors
async def update_bluetooth_state(
    login_obj: AlexaLogin, ctx: SetupContext, device_serial: str
):
    """Update the bluetooth state on ws bluetooth event."""
    hass = ctx.hass
    email = ctx.email
    bluetooth = await AlexaAPI.get_bluetooth(login_obj)
    device = hass.data[DATA_ALEXAMEDIA]["accounts"][email]["devices"]["media_player"][
        device_serial
    ]

    if bluetooth is not None and "bluetoothStates" in bluetooth:
        for b_state in bluetooth["bluetoothStates"]:
            if device_serial == b_state["deviceSerialNumber"]:
                _LOGGER.debug(
                    "%s: setting value for: %s to %s",
                    hide_email(email),
                    hide_serial(device_serial),
                    hide_serial(b_state),
                )
                device["bluetooth_state"] = b_state
                return device["bluetooth_state"]
    _LOGGER.debug(
        "%s: get_bluetooth for: %s failed with %s",
        hide_email(email),
        hide_serial(device_serial),
        hide_serial(bluetooth),
    )
    return None
