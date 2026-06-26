"""Shared per-ConfigEntry context for the Alexa Media Player setup helpers.

This dataclass carries the mutable state that used to be captured as closures
inside ``setup_alexa`` (DND throttling locks/timers, the account email, the
metrics handle, ...). Extracted ``setup`` submodules receive it explicitly so
they share exactly the same state without relying on nested scope.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Mapping

    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant

    from ..metrics import AlexaMetrics


@dataclass
class SetupContext:
    """Mutable state shared across the per-entry setup helpers.

    One instance is created per ``setup_alexa`` invocation, matching the
    previous behaviour where the throttling state was re-initialised on every
    (re)login.
    """

    hass: HomeAssistant
    config_entry: ConfigEntry
    email: str
    debug: bool = False
    metrics: AlexaMetrics | None = None

    # DND throttling state (one set per setup_alexa invocation).
    dnd_update_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_dnd_update_times: dict[str, datetime] = field(default_factory=dict)
    pending_dnd_updates: dict[str, bool] = field(default_factory=dict)
    scheduled_dnd_tasks: dict[str, asyncio.Task] = field(default_factory=dict)

    @property
    def config(self) -> Mapping[str, Any]:
        """Return the config entry data mapping."""
        return self.config_entry.data
