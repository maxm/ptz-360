"""HTTP for the card: a camera's panorama (GET /api/ptz_360/<entry id>/panorama/<id>.jpg, signed in), and the card's
script (/ptz_360/ptz-360-card.js), added to every dashboard."""
from __future__ import annotations

import os
import re

from aiohttp import web

from homeassistant.components.frontend import add_extra_js_url
from homeassistant.components.http import KEY_HASS, HomeAssistantView, StaticPathConfig
from homeassistant.core import HomeAssistant

from .const import CARD_URL, DOMAIN


class PanoramaView(HomeAssistantView):
    url = "/api/ptz_360/{entry_id}/panorama/{pid}.jpg"
    name = "api:ptz_360:panorama"
    requires_auth = True

    async def get(self, request: web.Request, entry_id: str, pid: str) -> web.Response:
        hass = request.app[KEY_HASS]
        if not re.fullmatch(r"[0-9A-Za-z]+", entry_id) or not re.fullmatch(r"[0-9a-f]+", pid):
            return web.Response(status=404)
        path = hass.config.path(".storage", DOMAIN, entry_id, f"panorama-{pid}.jpg")

        def read() -> bytes | None:
            try:
                with open(path, "rb") as f:
                    return f.read()
            except OSError:
                return None
        data = await hass.async_add_executor_job(read)
        if data is None:
            return web.Response(status=404)
        # A new panorama gets a new id: this one never changes.
        return web.Response(body=data, content_type="image/jpeg", headers={"Cache-Control": "private, max-age=31536000, immutable"})


async def async_register(hass: HomeAssistant) -> None:
    hass.http.register_view(PanoramaView())
    card = os.path.join(os.path.dirname(__file__), "frontend", "ptz-360-card.js")
    await hass.http.async_register_static_paths([StaticPathConfig(CARD_URL, card, cache_headers=False)])
    add_extra_js_url(hass, CARD_URL)
