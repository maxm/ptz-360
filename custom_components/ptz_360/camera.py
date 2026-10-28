"""The camera's picture as a camera entity: its RTSP stream (HA's WebRTC through go2rtc, HLS otherwise) and snapshots."""
from __future__ import annotations

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from . import PtzConfigEntry
from .device import PtzError
from .entity import PtzEntity


async def async_setup_entry(hass: HomeAssistant, entry: PtzConfigEntry, add: AddConfigEntryEntitiesCallback) -> None:
    add([PtzCamera(entry)])


class PtzCamera(PtzEntity, Camera):
    _attr_name = None
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(self, entry: PtzConfigEntry) -> None:
        Camera.__init__(self)
        PtzEntity.__init__(self, entry, None)
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
