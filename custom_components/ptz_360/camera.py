"""The camera's picture as a camera entity: its RTSP stream (HA's WebRTC through go2rtc, HLS otherwise) and snapshots."""
from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PtzConfigEntry
from .const import DOMAIN
from .device import PtzError


async def async_setup_entry(hass: HomeAssistant, entry: PtzConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    add([PtzCamera(entry)])


class PtzCamera(Camera):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry: PtzConfigEntry) -> None:
        super().__init__()
        self._runtime = entry.runtime_data
        info = self._runtime.device.info
        self._attr_unique_id = entry.unique_id or entry.entry_id
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, self._attr_unique_id)}, name=entry.title,
                                            manufacturer=info.get("manufacturer") or None, model=info.get("model") or None,
                                            serial_number=info.get("serial") or None, sw_version=info.get("firmware") or None)
        # The card finds the integration's commands by this.
        self._attr_extra_state_attributes = {"ptz_360": True}

    async def stream_source(self) -> str | None:
        try:
            return await self._runtime.device.stream_uri()
        except PtzError:
            return None

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        try:
            return await self._runtime.device.snapshot()
        except PtzError:
            return None
