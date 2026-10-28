"""PTZ 360: an ONVIF pan / tilt / zoom camera as a 360° view in the dashboard.

The integration talks ONVIF to the camera (moves by position, reads where it is), gives it a camera entity (its RTSP
stream: HA's WebRTC through go2rtc), measures it once (`calibrate`: the camera model and a stitched panorama of its
whole range) and serves a dashboard card (`custom:ptz-360-card`) that draws the panorama with the live picture on it
and moves the camera as the view is dragged.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME, Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.storage import Store

from . import http, websocket
from .calibrate import Calibration
from .const import CONF_STREAM_URL, CONF_VIDEO_ENTITY, DOMAIN, STORAGE_VERSION
from .device import PtzDevice, PtzError

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BUTTON, Platform.CAMERA, Platform.SENSOR]
SIGNAL_PROGRESS = f"{DOMAIN}_progress_{{}}"


class PtzEntry:
    """A configured camera: its device, what was measured of it (stored), and a calibration under way."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry, device: PtzDevice) -> None:
        self.hass, self.entry, self.device = hass, entry, device
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, f"{DOMAIN}.{entry.entry_id}")
        self.data: dict[str, Any] = {}
        self.calibration: Calibration | None = None
        self.progress: dict[str, Any] | None = None
        self.folder = hass.config.path(".storage", DOMAIN, entry.entry_id)

    async def load(self) -> None:
        self.data = (await self._store.async_load()) or {}

    async def save(self) -> None:
        await self._store.async_save(self.data)

    def info(self) -> dict[str, Any]:
        """What the card needs: ranges, the model, the panorama, home, and a calibration's progress."""
        return {"entry_id": self.entry.entry_id, "video_entity": self.entry.options.get(CONF_VIDEO_ENTITY),
                "ranges": self.device.ranges, "device": self.device.info,
                "calibration": self.data.get("calibration"), "panorama": self.data.get("panorama"),
                "home": self.data.get("home"), "progress": self.progress}

    async def calibrate(self) -> None:
        """Starts the calibration pass in the background (its progress in `info`, and as events)."""
        if self.calibration is not None:
            raise PtzError("already calibrating")
        await self.hass.async_add_executor_job(os.makedirs, self.folder, 0o755, True)

        @callback
        def report(progress: dict[str, Any]) -> None:
            self.progress = progress
            async_dispatcher_send(self.hass, SIGNAL_PROGRESS.format(self.entry.entry_id), progress)

        self.calibration = Calibration(self.hass, self.device, self.folder, report)

        async def run() -> None:
            try:
                result = await self.calibration.run()
                self.data["calibration"] = result["calibration"]
                self.data["panorama"] = result["panorama"]
                await self.save()
                report({"stage": "done"})
            except Exception:  # noqa: BLE001  (reported as the progress's error)
                _LOGGER.exception("calibration of %s failed", self.entry.title)
            finally:
                self.calibration = None

        self.entry.async_create_background_task(self.hass, run(), f"{DOMAIN} calibration {self.entry.entry_id}")

    async def home(self) -> dict[str, Any]:
        """Sends the camera home and says where that is: the pose noted with `set_home`, else the camera's own home
        position (noted the first time)."""
        pose = self.data.get("home")
        if pose:
            await self.device.goto(pose["pan"], pose["tilt"], pose["zoom"])
            return pose
        await self.device.goto_home()
        s = await self.device.settle()
        pose = {"pan": s["pan"], "tilt": s["tilt"], "zoom": s["zoom"]}
        self.data["home"] = pose
        await self.save()
        return pose

    async def set_home(self) -> dict[str, Any]:
        s = await self.device.status()
        self.data["home"] = {"pan": s["pan"], "tilt": s["tilt"], "zoom": s["zoom"]}
        await self.save()
        return self.data["home"]


type PtzConfigEntry = ConfigEntry[PtzEntry]


# Set up from the UI only (no YAML).
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    websocket.async_register(hass)
    await http.async_register(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: PtzConfigEntry) -> bool:
    d = entry.data
    device = PtzDevice(hass, d[CONF_HOST], d[CONF_PORT], d[CONF_USERNAME], d[CONF_PASSWORD],
                       stream_url=entry.options.get(CONF_STREAM_URL))
    try:
        await asyncio.wait_for(device.connect(), 20)
    except (PtzError, TimeoutError) as err:
        raise ConfigEntryNotReady(f"{d[CONF_HOST]}: {err}") from err
    runtime = PtzEntry(hass, entry, device)
    await runtime.load()
    entry.runtime_data = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    # New options (where the picture comes from): set up again.
    entry.async_on_unload(entry.add_update_listener(lambda hass, entry: hass.config_entries.async_reload(entry.entry_id)))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: PtzConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        await entry.runtime_data.device.close()
    return ok
