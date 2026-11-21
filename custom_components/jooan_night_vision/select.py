"""Home Assistant select entities for JOOAN night vision."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone

import voluptuous as vol
from homeassistant.components.select import PLATFORM_SCHEMA as SELECT_PLATFORM_SCHEMA
from homeassistant.components.select import SelectEntity
from homeassistant.const import CONF_EMAIL, CONF_NAME, CONF_PASSWORD
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_platform
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from . import DOMAIN
from .client import JooanClient, JooanClientError, JooanState, validate_uid

CONF_APPLICATION_ID = "application_id"
CONF_CAMERAS = "cameras"
CONF_KEY = "key"
CONF_DEVICE_ID = "device_id"
OPTIONS = ("Normal night vision", "Scheduled full color", "Smart full color")
MODE_BY_OPTION = dict(zip(OPTIONS, range(3), strict=True))
POLL_INTERVAL = timedelta(minutes=15)
CONFIRM_DELAY = 3
SERVICE_SET_SCHEDULE = "set_schedule"
_HOUR_TIME = vol.All(cv.string, vol.Match(r"^([01]\d|2[0-3]):00(:00)?\Z"))

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
    platform = entity_platform.async_get_current_platform()
    platform.async_register_entity_service(
        SERVICE_SET_SCHEDULE,
        {vol.Required("start"): _HOUR_TIME, vol.Required("end"): _HOUR_TIME},
        "async_set_schedule",
    )
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
    """Night-vision select. HA keeps the desired state and the cloud read-back
    confirms it; ``confirmed`` is False when the camera/cloud does not match."""

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
        self._desired_option: str | None = None
        self._desired_window: tuple[int, int] | None = None
        self._cloud: JooanState | None = None
        self._read_at: str | None = None
        self._read_error: str | None = None

    @property
    def _cloud_option(self) -> str | None:
        if self._cloud is None or self._cloud.mode is None:
            return None
        return OPTIONS[self._cloud.mode]

    @property
    def _cloud_window(self) -> tuple[int, int] | None:
        if self._cloud is None or self._cloud.start is None:
            return None
        return (self._cloud.start, self._cloud.end)

    @property
    def _confirmed(self) -> bool | None:
        """True when the cloud read-back matches the desired state."""
        if self._cloud is None:
            return None
        if self._desired_option is not None and self._desired_option != self._cloud_option:
            return False
        if self._desired_window is not None and self._desired_window != self._cloud_window:
            return False
        if self._desired_option is None and self._desired_window is None:
            return True if self._cloud_option is not None else None
        return True

    @property
    def extra_state_attributes(self):
        window = self._cloud_window
        return {
            "state_source": (
                "cloud_readback"
                if self._cloud is not None
                else "last_device_acknowledged_command"
                if self._last_acknowledged_at
                else "unknown"
            ),
            "last_acknowledged_at": self._last_acknowledged_at,
            "readback_supported": self._cloud is not None,
            "confirmed": self._confirmed,
            "cloud_option": self._cloud_option,
            "schedule_start": f"{window[0]:02d}:00" if window else None,
            "schedule_end": f"{window[1]:02d}:00" if window else None,
            "last_read_at": self._read_at,
            "read_error": self._read_error,
        }

    def _publish(self) -> None:
        self._attr_current_option = self._desired_option or self._cloud_option
        self.async_write_ha_state()

    def _clear_state(self) -> None:
        self._attr_current_option = None
        self._last_acknowledged_at = None
        self._desired_option = None
        self._desired_window = None
        self.async_write_ha_state()

    async def async_added_to_hass(self) -> None:
        self.async_on_remove(
            async_track_time_interval(self.hass, self._async_poll, POLL_INTERVAL)
        )
        self.hass.async_create_task(self.async_refresh())

    async def _async_poll(self, _now) -> None:
        await self.async_refresh()

    async def async_refresh(self) -> None:
        """Read the cloud state, waiting for the shared account lock.

        Both cameras of one account share the lock; waiting (rather than
        skipping) keeps every camera's startup and periodic read from being
        starved by a sibling's read or a command.
        """
        async with self._lock:
            await self._async_read()
        self._publish()

    async def _async_read(self) -> None:
        try:
            result = await self.hass.async_add_executor_job(self._client.read_state)
        except JooanClientError:
            self._read_error = "camera state could not be read"
            return
        if isinstance(result, JooanState):
            self._cloud = result
            self._read_error = None
            self._read_at = datetime.now(timezone.utc).isoformat()

    async def _run_command(self, func, *args) -> None:
        """Run one blocking command under the lock, then read back to confirm."""
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
                        self.hass.async_add_executor_job(func, *args)
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
                self._last_acknowledged_at = datetime.now(timezone.utc).isoformat()
                succeeded = True
            finally:
                if not succeeded:
                    self._clear_state()
            await asyncio.sleep(CONFIRM_DELAY)
            await self._async_read()
            self._publish()

    async def async_select_option(self, option: str) -> None:
        if option not in MODE_BY_OPTION:
            raise HomeAssistantError("Invalid JOOAN night-vision option")
        await self._run_command(self._client.set_mode, MODE_BY_OPTION[option])
        self._desired_option = option
        self._publish()
        self._raise_if_unconfirmed()

    async def async_set_schedule(self, start: str, end: str) -> None:
        start_hour, end_hour = int(start[:2]), int(end[:2])
        if start_hour == end_hour:
            raise HomeAssistantError("Night-vision start and end must differ")
        await self._run_command(self._client.set_schedule, start_hour, end_hour)
        self._desired_window = (start_hour, end_hour)
        self._publish()
        self._raise_if_unconfirmed()

    def _raise_if_unconfirmed(self) -> None:
        if self._confirmed is False:
            raise HomeAssistantError(
                "The camera accepted the command but its reported state "
                "did not confirm it yet"
            )
