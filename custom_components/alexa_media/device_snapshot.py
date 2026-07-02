"""Persistent device snapshot enabling optimistic boot.

The last successful device inventory (``account["devices"]``) is persisted to
Home Assistant storage after each coordinator refresh. On the next startup the
entities are recreated immediately from that snapshot — before any Amazon
round-trip — and the real first refresh runs in the background, flipping the
entities to live data a second or two later.

The snapshot lives in ``.storage/alexa_media.<entry_id>.device_snapshot``
(private file, like the alexapy cookie jar) and is removed with the entry.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.helpers.storage import Store

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
# Debounce writes: the inventory is enriched on every refresh cycle but only
# meaningfully changes when devices come or go.
SNAPSHOT_SAVE_DELAY = 10.0


class DeviceSnapshotStore:
    """Store wrapper for one config entry's device snapshot."""

    def __init__(self, hass: HomeAssistant, entry_id: str) -> None:
        """Initialize the underlying HA Store."""
        self._store: Store[dict[str, Any]] = Store(
            hass,
            STORAGE_VERSION,
            f"{DOMAIN}.{entry_id}.device_snapshot",
            private=True,
        )

    async def async_load(self) -> dict[str, Any] | None:
        """Load the persisted device inventory, or None."""
        try:
            return await self._store.async_load()
        except Exception:  # pylint: disable=broad-except
            # A corrupt snapshot must never block the classic boot path.
            _LOGGER.warning("Discarding unreadable device snapshot", exc_info=True)
            return None

    def async_delay_save(self, devices: dict[str, Any]) -> None:
        """Schedule a debounced save of the device inventory.

        ``devices`` is the live ``account["devices"]`` mapping; it is
        serialized at write time, so the freshest state wins.
        """
        self._store.async_delay_save(lambda: devices, SNAPSHOT_SAVE_DELAY)

    async def async_remove(self) -> None:
        """Delete the persisted snapshot."""
        await self._store.async_remove()
