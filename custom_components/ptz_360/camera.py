"""The camera's picture as a camera entity: its RTSP stream (HA's WebRTC through go2rtc, HLS otherwise) and snapshots."""
from __future__ import annotations

from haffmpeg.camera import CameraMjpeg

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.components.ffmpeg import get_ffmpeg_manager
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_aiohttp_proxy_stream
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

    async def handle_async_mjpeg_stream(self, request):
        """MJPEG straight from the RTSP stream (ffmpeg), at the camera's frame rate: for browsers without WebRTC to
        the camera (the card follows moves frame by frame; snapshots would be a second apart)."""
        manager = get_ffmpeg_manager(self.hass)
        stream = CameraMjpeg(manager.binary)
        # RTSP over TCP: over UDP the camera's packets don't make it back through NAT (Docker, some routers).
        await stream.open(cmd=["-rtsp_transport", "tcp", "-i", await self._runtime.device.stream_uri(), "-an", "-c:v", "mjpeg",
                               "-q:v", "5", "-vf", "scale=960:-2"], input_source=None, output="-f mpjpeg -")
        try:
            return await async_aiohttp_proxy_stream(self.hass, request, await stream.get_reader(), manager.ffmpeg_stream_content_type)
        finally:
            await stream.close()

    async def async_camera_image(self, width: int | None = None, height: int | None = None) -> bytes | None:
        try:
            return await self._runtime.device.snapshot()
        except PtzError:
            return None
