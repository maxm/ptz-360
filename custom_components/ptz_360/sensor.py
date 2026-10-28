"""Calibration: where a calibration is (idle, measuring, model frames, fitting, sweeping, stitching, done, error),
with its step, its error, when the camera was last calibrated and how well its model fits."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PtzConfigEntry
from .entity import PtzEntity

STAGES = ["idle", "starting", "measuring", "model_frames", "fitting", "sweeping", "stitching", "done", "error"]


async def async_setup_entry(hass: HomeAssistant, entry: PtzConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    add([CalibrationSensor(entry)])


class CalibrationSensor(PtzEntity, SensorEntity):
    _attr_translation_key = "calibration"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = STAGES
    _attr_icon = "mdi:progress-wrench"

    def __init__(self, entry: PtzConfigEntry) -> None:
        super().__init__(entry, "calibration")

    @property
    def native_value(self) -> str:
        p = self._runtime.progress
        if not p:
            return "idle"
        stage = str(p.get("stage", "idle")).replace(" ", "_")
        return stage if stage in STAGES else "idle"

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        p = self._runtime.progress or {}
        calibration = self._runtime.data.get("calibration") or {}
        at, rms = calibration.get("at"), (calibration.get("model") or {}).get("rms")
        return {"step": p.get("done"), "steps": p.get("total"), "error": p.get("error"),
                "calibrated": datetime.fromtimestamp(at, timezone.utc).isoformat() if at else None,
                # How well the camera model fits (pixels; well under 1 is good).
                "fit_rms": round(rms, 2) if rms is not None else None}
