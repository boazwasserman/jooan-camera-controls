"""JOOAN Camera Controls integration."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv

DOMAIN = "jooan_night_vision"

from .ptz_services import (  # noqa: E402, I001
    CONF_PTZ_CAMERAS,
    async_setup as async_setup_ptz,
    validate_ptz_config,
)


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Set up optional local PTZ services without touching legacy YAML select."""
    cameras = config.get(DOMAIN, {}).get(CONF_PTZ_CAMERAS, [])
    if cameras:
        await async_setup_ptz(hass, validate_ptz_config(cameras))
    return True


CONFIG_SCHEMA = vol.Schema(
    {
        vol.Optional(DOMAIN, default={}): vol.Schema(
            {
                vol.Optional("ptz_cameras", default=[]): vol.All(
                    cv.ensure_list, validate_ptz_config
                )
            },
            extra=vol.PREVENT_EXTRA,
        )
    },
    extra=vol.ALLOW_EXTRA,
)
