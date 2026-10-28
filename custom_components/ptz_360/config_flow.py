"""Setting up a camera: its ONVIF address and login (checked: it must answer and be able to pan)."""
from __future__ import annotations

import asyncio
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_PORT, CONF_USERNAME
from homeassistant.helpers.selector import TextSelector, TextSelectorConfig, TextSelectorType

from .const import DOMAIN
from .device import PtzDevice, PtzError

SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Required(CONF_PORT, default=80): int,
    vol.Required(CONF_USERNAME): str,
    vol.Required(CONF_PASSWORD): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
})


class PtzConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            device = PtzDevice(self.hass, user_input[CONF_HOST], user_input[CONF_PORT], user_input[CONF_USERNAME],
                               user_input[CONF_PASSWORD])
            try:
                await asyncio.wait_for(device.connect(), 20)
            except (PtzError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                info = device.info
                await self.async_set_unique_id(info.get("serial") or user_input[CONF_HOST])
                self._abort_if_unique_id_configured(updates=user_input)
                title = " ".join(v for v in (info.get("manufacturer"), info.get("model")) if v) or user_input[CONF_HOST]
                return self.async_create_entry(title=title, data=user_input)
            finally:
                await device.close()
        return self.async_show_form(step_id="user", data_schema=self.add_suggested_values_to_schema(SCHEMA, user_input),
                                    errors=errors)
