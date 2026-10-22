"""Home Assistant select entities for JOOAN night vision."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone

import voluptuous as vol
from homeassistant.components.select import PLATFORM_SCHEMA as SELECT_PLATFORM_SCHEMA
from homeassistant.components.select import SelectEntity
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import DOMAIN
from .client import JooanClient, JooanClientError, validate_uid

CONF_APPLICATION_ID = "application_id"
CONF_CAMERAS = "cameras"
CONF_KEY = "key"
CONF_DEVICE_ID = "device_id"
OPTIONS = ("Normal night vision", "Scheduled full color", "Smart full color")
MODE_BY_OPTION = dict(zip(OPTIONS, range(3), strict=True))

_CAMERA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_KEY): cv.string,
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_DEVICE_ID): cv.string,
    }
)
_BASE_SCHEMA = SELECT_PLATFORM_SCHEMA.extend(
    {
        vol.Required(CONF_EMAIL): cv.string,
        vol.Required(CONF_PASSWORD): cv.string,
        vol.Required(CONF_APPLICATION_ID): cv.string,
        vol.Required(CONF_CAMERAS): vol.All(
            cv.ensure_list, vol.Length(min=1), [_CAMERA_SCHEMA]
        ),
    }
)


def _validate_config(config):
    email = config[CONF_EMAIL]
    password = config[CONF_PASSWORD]
    application_id = config[CONF_APPLICATION_ID]
    if not email or not password:
        raise vol.Invalid("JOOAN email and password are required")
    if len(application_id) != 20 or any(
        ord(char) < 0x20 or ord(char) > 0x7E or char.isspace()
        for char in application_id
    ):
        raise vol.Invalid("JOOAN application_id must be 20 printable ASCII characters")
    cameras = config[CONF_CAMERAS]
    keys = [camera[CONF_KEY] for camera in cameras]
    device_ids = [camera[CONF_DEVICE_ID] for camera in cameras]
    if any(not re.fullmatch(r"[A-Za-z0-9_-]+", key) for key in keys):
        raise vol.Invalid("JOOAN camera keys must be safe stable identifiers")
    if len(set(keys)) != len(keys):
        raise vol.Invalid("JOOAN camera keys must be unique")
    if len(set(device_ids)) != len(device_ids):
        raise vol.Invalid("JOOAN device_ids must be unique")
    try:
        for device_id in device_ids:
            validate_uid(device_id)
    except ValueError as err:
        raise vol.Invalid(str(err)) from err
    return config


PLATFORM_SCHEMA = vol.All(_BASE_SCHEMA, _validate_config)


def _account_lock(hass: HomeAssistant, email: str) -> asyncio.Lock:
    accounts = hass.data.setdefault(DOMAIN, {})
    account_key = email.strip().casefold()
    account = accounts.setdefault(account_key, {})
    return account.setdefault("lock", asyncio.Lock())


async def async_setup_platform(
    hass: HomeAssistant,
    config: dict,
    async_add_entities: AddEntitiesCallback,
    discovery_info=None,
) -> None:
    lock = _account_lock(hass, config[CONF_EMAIL])
    async_add_entities(
        [
            JooanNightVisionSelect(
                hass,
                lock,
                email=config[CONF_EMAIL],
                password=config[CONF_PASSWORD],
                application_id=config[CONF_APPLICATION_ID],
                key=camera[CONF_KEY],
                name=camera[CONF_NAME],
                device_id=camera[CONF_DEVICE_ID],
            )
            for camera in config[CONF_CAMERAS]
        ]
    )


class JooanNightVisionSelect(SelectEntity):
    """A command select whose state is last acknowledged, not read back."""

    _attr_assumed_state = True
    _attr_options = list(OPTIONS)
    _attr_should_poll = False

    def __init__(
        self,
        hass,
        lock,
        *,
        email,
        password,
        application_id,
        key,
        name,
        device_id,
    ):
        self.hass = hass
        self._lock = lock
        self._attr_name = name
        self._attr_unique_id = f"{key}_night_vision"
        self._attr_current_option = None
        self._client = JooanClient(
            email=email,
            password=password,
            application_id=application_id,
            device_id=device_id,
        )
        self._last_acknowledged_at: str | None = None

    @property
    def extra_state_attributes(self):
        return {
            "state_source": (
                "last_device_acknowledged_command"
                if self._last_acknowledged_at
                else "unknown"
            ),
            "last_acknowledged_at": self._last_acknowledged_at,
            "readback_supported": False,
        }

    def _clear_state(self) -> None:
        self._attr_current_option = None
        self._last_acknowledged_at = None
        self.async_write_ha_state()

    async def async_select_option(self, option: str) -> None:
        if option not in MODE_BY_OPTION:
            raise HomeAssistantError("Invalid JOOAN night-vision option")
        if self._lock.locked():
            raise HomeAssistantError(
                "Another JOOAN night-vision command is already in progress"
            )
        async with self._lock:
            cancelled = False
            succeeded = False
            try:
                try:
                    worker = asyncio.ensure_future(
                        self.hass.async_add_executor_job(
                            self._client.set_mode, MODE_BY_OPTION[option]
                        )
                    )
                except RuntimeError:
                    raise HomeAssistantError(
                        "JOOAN night-vision command could not be scheduled"
                    ) from None
                completed = asyncio.Event()
                worker.add_done_callback(lambda _: completed.set())
                while not worker.done():
                    try:
                        await completed.wait()
                    except asyncio.CancelledError:
                        cancelled = True
                if cancelled:
                    if not worker.cancelled():
                        worker.exception()
                    raise asyncio.CancelledError
                try:
                    worker.result()
                except JooanClientError:
                    raise HomeAssistantError(
                        "JOOAN night-vision command failed"
                    ) from None
                self._attr_current_option = option
                self._last_acknowledged_at = datetime.now(timezone.utc).isoformat()
                self.async_write_ha_state()
                succeeded = True
            finally:
                if not succeeded:
                    self._clear_state()
