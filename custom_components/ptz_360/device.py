"""One ONVIF PTZ camera: absolute moves, its position, its stream and its pictures."""
from __future__ import annotations

import asyncio
import importlib
import logging
import os
import time
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

from homeassistant.core import HomeAssistant
from homeassistant.helpers.httpx_client import get_async_client

_LOGGER = logging.getLogger(__name__)


class PtzError(Exception):
    pass


class PtzDevice:
    """The camera's ONVIF services, connected on first use (and again after a dropped connection)."""

    def __init__(self, hass: HomeAssistant, host: str, port: int, username: str, password: str) -> None:
        self.hass = hass
        self.host, self.port, self.username, self.password = host, port, username, password
        self._lock = asyncio.Lock()
        self._cam = None
        self._ptz = None
        self._media = None
        self._token: str | None = None
        self.ranges: dict[str, list[float]] = {"pan": [-1.0, 1.0], "tilt": [-1.0, 1.0], "zoom": [0.0, 1.0]}
        self.info: dict[str, str] = {}
        self._stream_uri: str | None = None
        self._snapshot_uri: str | None = None

    async def connect(self) -> None:
        """Connects (or reuses the connection); raises PtzError when the camera can't be reached or can't pan."""
        await self._services()

    async def _services(self):
        async with self._lock:
            if self._ptz is not None:
                return self._ptz, self._token
            try:
                # HA's onvif integration ships the library; importing it reads files, so not in the event loop.
                onvif = await self.hass.async_add_executor_job(importlib.import_module, "onvif")
                wsdl = os.path.join(os.path.dirname(onvif.__file__), "wsdl")
                cam = onvif.ONVIFCamera(self.host, self.port, self.username, self.password, wsdl)
                await cam.update_xaddrs()
                devicemgmt = await cam.create_devicemgmt_service()
                info = await devicemgmt.GetDeviceInformation()
                self.info = {"manufacturer": str(info.Manufacturer or ""), "model": str(info.Model or ""),
                             "serial": str(info.SerialNumber or ""), "firmware": str(info.FirmwareVersion or "")}
                media = await cam.create_media_service()
                ptz = await cam.create_ptz_service()
                profiles = await media.GetProfiles()
                # The first profile that can pan (usually the main stream's).
                profile = next((p for p in profiles if getattr(p, "PTZConfiguration", None)), profiles[0])
                token = profile.token
                try:
                    for node in await ptz.GetNodes():
                        spaces = node.SupportedPTZSpaces
                        pt = spaces.AbsolutePanTiltPositionSpace[0]
                        z = spaces.AbsoluteZoomPositionSpace[0]
                        self.ranges = {"pan": [pt.XRange.Min, pt.XRange.Max], "tilt": [pt.YRange.Min, pt.YRange.Max],
                                       "zoom": [z.XRange.Min, z.XRange.Max]}
                        break
                except Exception:  # noqa: BLE001  (the generic spaces are the default)
                    pass
                self._cam, self._ptz, self._media, self._token = cam, ptz, media, token
                return ptz, token
            except Exception as err:  # noqa: BLE001
                raise PtzError(str(err) or type(err).__name__) from err

    async def _call(self, fn):
        try:
            ptz, token = await self._services()
            return await fn(ptz, token)
        except PtzError:
            raise
        except Exception as err:  # noqa: BLE001
            # A dropped connection: reconnect next time.
            self._ptz = None
            raise PtzError(str(err) or type(err).__name__) from err

    async def close(self) -> None:
        if self._cam is not None:
            try:
                await self._cam.close()
            except Exception:  # noqa: BLE001
                pass
        self._cam = self._ptz = self._media = None

    # ── moving ──

    async def status(self) -> dict[str, Any]:
        async def fn(ptz, token):
            s = await ptz.GetStatus({"ProfileToken": token})
            pos, move = s.Position, s.MoveStatus
            moving = any(str(getattr(move, k, "IDLE") or "IDLE").upper() not in ("IDLE", "UNKNOWN")
                         for k in ("PanTilt", "Zoom")) if move else False
            return {"pan": pos.PanTilt.x, "tilt": pos.PanTilt.y, "zoom": pos.Zoom.x, "moving": moving, "at": time.time()}
        return await self._call(fn)

    async def goto(self, pan: float, tilt: float, zoom: float) -> None:
        def clamp(v, r):
            return min(max(v, r[0]), r[1])

        async def fn(ptz, token):
            await ptz.AbsoluteMove({"ProfileToken": token, "Position": {
                "PanTilt": {"x": clamp(pan, self.ranges["pan"]), "y": clamp(tilt, self.ranges["tilt"])},
                "Zoom": {"x": clamp(zoom, self.ranges["zoom"])}}})
        await self._call(fn)

    async def stop(self) -> None:
        async def fn(ptz, token):
            await ptz.Stop({"ProfileToken": token, "PanTilt": True, "Zoom": True})
        await self._call(fn)

    async def goto_home(self) -> None:
        """The camera's own home position (ONVIF GotoHomePosition)."""
        async def fn(ptz, token):
            await ptz.GotoHomePosition({"ProfileToken": token})
        await self._call(fn)

    async def settle(self, timeout: float = 15) -> dict[str, Any]:
        """Waits until the camera stops moving, then a little more for the picture to catch up."""
        end = time.monotonic() + timeout
        s = await self.status()
        last = None
        # Some cameras keep saying "moving" once still: an unchanged position counts as stopped.
        while time.monotonic() < end and s["moving"] and (last is None or (s["pan"], s["tilt"], s["zoom"]) != last):
            last = (s["pan"], s["tilt"], s["zoom"])
            await asyncio.sleep(0.3)
            s = await self.status()
        await asyncio.sleep(2.0)
        return await self.status()

    # ── video and pictures ──

    def _with_login(self, uri: str) -> str:
        """`uri` with the camera's login in it (RTSP clients take it from the URL)."""
        parts = urlsplit(uri)
        if parts.username:
            return uri
        login = f"{quote(self.username, safe='')}:{quote(self.password, safe='')}@"
        return urlunsplit((parts.scheme, login + parts.netloc, parts.path, parts.query, parts.fragment))

    async def stream_uri(self) -> str:
        """The profile's RTSP address, with the login."""
        if self._stream_uri is None:
            await self._services()
            r = await self._media.GetStreamUri({"StreamSetup": {"Stream": "RTP-Unicast", "Transport": {"Protocol": "RTSP"}},
                                                "ProfileToken": self._token})
            self._stream_uri = self._with_login(r.Uri)
        return self._stream_uri

    async def snapshot(self) -> bytes:
        """The camera's current picture (JPEG): its ONVIF snapshot address, else a frame from its stream."""
        await self._services()
        if self._snapshot_uri is None:
            try:
                self._snapshot_uri = (await self._media.GetSnapshotUri({"ProfileToken": self._token})).Uri or ""
            except Exception:  # noqa: BLE001
                self._snapshot_uri = ""
        if self._snapshot_uri:
            client = get_async_client(self.hass, verify_ssl=False)
            for auth in (httpx.DigestAuth(self.username, self.password), httpx.BasicAuth(self.username, self.password)):
                try:
                    r = await client.get(self._snapshot_uri, auth=auth, timeout=10)
                    if r.status_code == 200 and r.content[:2] == b"\xff\xd8":
                        return r.content
                except httpx.HTTPError:
                    pass
            _LOGGER.debug("%s: snapshot address didn't answer with a JPEG; using the stream", self.host)
            self._snapshot_uri = ""
        from homeassistant.components.ffmpeg import async_get_image  # noqa: PLC0415

        image = await async_get_image(self.hass, await self.stream_uri())
        if not image:
            raise PtzError("no picture from the camera")
        return image
