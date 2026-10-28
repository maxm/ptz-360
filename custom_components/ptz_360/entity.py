"""What the device's entities share: the device, and updates as a calibration goes."""
from __future__ import annotations

from typing import Any

from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity

from .const import DOMAIN


class PtzEntity(Entity):
    _attr_has_entity_name = True

    def __init__(self, entry, key: str | None) -> None:
        self._runtime = entry.runtime_data
        self._entry = entry
        self.device_id = entry.unique_id or entry.entry_id
        self._attr_unique_id = f"{self.device_id}_{key}" if key else self.device_id
        info = self._runtime.device.info
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, self.device_id)}, name=entry.title,
                                            manufacturer=info.get("manufacturer") or None, model=info.get("model") or None,
                                            serial_number=info.get("serial") or None, sw_version=info.get("firmware") or None)

    async def async_added_to_hass(self) -> None:
        from . import SIGNAL_PROGRESS  # noqa: PLC0415

        self.async_on_remove(async_dispatcher_connect(self.hass, SIGNAL_PROGRESS.format(self._entry.entry_id), self._progress))

    @callback
    def _progress(self, progress: dict[str, Any]) -> None:
        self.async_write_ha_state()
