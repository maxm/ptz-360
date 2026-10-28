"""Websocket commands for the card, by the camera entity: ptz_360/info, status, goto, stop, home, set_home,
calibrate (admin), and subscribe_progress (a calibration's progress as it goes)."""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, entity_registry as er
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN
from .device import PtzError

if TYPE_CHECKING:
    from . import PtzEntry


def _entry(hass: HomeAssistant, entity_id: str) -> PtzEntry:
    reg = er.async_get(hass).async_get(entity_id)
    entry = hass.config_entries.async_get_entry(reg.config_entry_id) if reg and reg.config_entry_id else None
    if entry is None or entry.domain != DOMAIN or not hasattr(entry, "runtime_data"):
        raise PtzError(f"{entity_id} isn't a PTZ 360 camera")
    return entry.runtime_data


async def _run(connection: websocket_api.ActiveConnection, msg: dict[str, Any],
               fn: Callable[[PtzEntry], Awaitable[Any]]) -> None:
    try:
        result = await fn(_entry(connection.hass, msg["entity_id"]))
    except PtzError as err:
        connection.send_error(msg["id"], "ptz_error", str(err))
        return
    connection.send_result(msg["id"], result)


ENTITY = {vol.Required("entity_id"): cv.entity_id}


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/info", **ENTITY})
@websocket_api.async_response
async def ws_info(hass, connection, msg):
    async def fn(e: PtzEntry):
        return {**e.info(), "status": await e.device.status()}
    await _run(connection, msg, fn)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/status", **ENTITY})
@websocket_api.async_response
async def ws_status(hass, connection, msg):
    await _run(connection, msg, lambda e: e.device.status())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/goto", **ENTITY, vol.Required("pan"): vol.Coerce(float),
                                  vol.Required("tilt"): vol.Coerce(float), vol.Required("zoom"): vol.Coerce(float)})
@websocket_api.async_response
async def ws_goto(hass, connection, msg):
    async def fn(e: PtzEntry):
        await e.device.goto(msg["pan"], msg["tilt"], msg["zoom"])
        return {}
    await _run(connection, msg, fn)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/stop", **ENTITY})
@websocket_api.async_response
async def ws_stop(hass, connection, msg):
    async def fn(e: PtzEntry):
        await e.device.stop()
        return {}
    await _run(connection, msg, fn)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/home", **ENTITY})
@websocket_api.async_response
async def ws_home(hass, connection, msg):
    await _run(connection, msg, lambda e: e.home())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/set_home", **ENTITY})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_set_home(hass, connection, msg):
    await _run(connection, msg, lambda e: e.set_home())


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/calibrate", **ENTITY})
@websocket_api.require_admin
@websocket_api.async_response
async def ws_calibrate(hass, connection, msg):
    async def fn(e: PtzEntry):
        await e.calibrate()
        return {}
    await _run(connection, msg, fn)


@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/subscribe_progress", **ENTITY})
@callback
def ws_subscribe_progress(hass, connection, msg):
    from . import SIGNAL_PROGRESS  # noqa: PLC0415

    try:
        e = _entry(hass, msg["entity_id"])
    except PtzError as err:
        connection.send_error(msg["id"], "ptz_error", str(err))
        return

    @callback
    def forward(progress: dict[str, Any]) -> None:
        connection.send_event(msg["id"], progress)

    connection.subscriptions[msg["id"]] = async_dispatcher_connect(hass, SIGNAL_PROGRESS.format(e.entry.entry_id), forward)
    connection.send_result(msg["id"])
    if e.progress:
        connection.send_event(msg["id"], e.progress)


@callback
def async_register(hass: HomeAssistant) -> None:
    for handler in (ws_info, ws_status, ws_goto, ws_stop, ws_home, ws_set_home, ws_calibrate, ws_subscribe_progress):
        websocket_api.async_register_command(hass, handler)
