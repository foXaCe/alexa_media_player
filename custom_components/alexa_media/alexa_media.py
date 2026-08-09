"""
Alexa Devices Base Class.

SPDX-License-Identifier: Apache-2.0

For more details about this platform, please refer to the documentation at
https://community.home-assistant.io/t/echo-devices-alexa-as-media-player-testers-needed/58639
"""

from __future__ import annotations

import logging

from alexapy import AlexaAPI, AlexaLogin, hide_email

from .const import DATA_ALEXAMEDIA

_LOGGER = logging.getLogger(__name__)


class AlexaMedia:
    """Implementation of Alexa Media Base object."""

    def __init__(self, device, login) -> None:
        """Initialize the Alexa device."""

        # Class info
        self._login = login
        # The object AlexaAPI expects as its `device` argument (the AlexaClient
        # instance on media_player/switch, None on alarm_control_panel). Stored
        # under a name subclasses do not override (`self._device` is repurposed
        # by AlexaClient to hold the raw device dict).
        self._api_device = device
        self.email = login.email
        self.account = hide_email(login.email)
        self._alexa_api: AlexaAPI | None = None

    @property
    def alexa_api(self) -> AlexaAPI:
        """Lazily build the API handle.

        Constructing ``AlexaAPI`` logs a warning when the session has no csrf
        cookie yet. During the fully-optimistic boot entities are created
        before the login probe completes, so deferring the construction until
        first use avoids spurious "missing csrf" warnings (API calls are also
        gated on login success in ``refresh()``).
        """
        if self._alexa_api is None:
            self._alexa_api = AlexaAPI(self._api_device, self._login)
        return self._alexa_api

    @alexa_api.setter
    def alexa_api(self, value: AlexaAPI) -> None:
        """Allow replacing the API handle (tests, relogin)."""
        self._alexa_api = value
        # Keep the wrapper's base login state in sync when the handle is
        # swapped for a real API object bound to a (different) login. Mocks
        # (tests) and None are left untouched.
        login = getattr(value, "_login", None)
        if isinstance(login, AlexaLogin):
            self._login = login
            self.email = login.email
            self.account = hide_email(login.email)

    def check_login_changes(self):
        """Update Login object if it has changed."""
        # _LOGGER.debug("Checking if Login object has changed")
        try:
            login = self.hass.data[DATA_ALEXAMEDIA]["accounts"][self.email]["login_obj"]
        except AttributeError, KeyError:
            return
        # _LOGGER.debug("Login object %s closed status: %s", login, login.session.closed)
        # _LOGGER.debug(
        #     "Alexaapi %s closed status: %s",
        #     self.alexa_api,
        #     self.alexa_api._session.closed,
        # )
        if self.alexa_api.update_login(login):
            _LOGGER.debug("Login object has changed; updating")
            self._login = login
            self.email = login.email
            self.account = hide_email(login.email)
