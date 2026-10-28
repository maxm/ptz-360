"""Calibrate: the calibration pass (calibrate.py) from the device page or an automation."""
from __future__ import annotations

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PtzConfigEntry
from .device import PtzError
from .entity import PtzEntity


async def async_setup_entry(hass: HomeAssistant, entry: PtzConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    add([CalibrateButton(entry)])


class CalibrateButton(PtzEntity, ButtonEntity):
    _attr_translation_key = "calibrate"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_icon = "mdi:camera-marker-outline"

    def __init__(self, entry: PtzConfigEntry) -> None:
        super().__init__(entry, "calibrate")

    @property
    def available(self) -> bool:
        # Not while a calibration is running.
        return self._runtime.calibration is None

    async def async_press(self) -> None:
        try:
            await self._runtime.calibrate()
        except PtzError as err:
            raise HomeAssistantError(str(err)) from err
