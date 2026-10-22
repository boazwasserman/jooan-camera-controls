"""Home Assistant services for bounded local JOOAN PTZ control."""

from __future__ import annotations

import asyncio
import functools
import logging
import math
import threading
from dataclasses import dataclass
from typing import Any, Callable

import voluptuous as vol
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.components import persistent_notification
from homeassistant.const import ATTR_ENTITY_ID, EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HomeAssistant, ServiceCall, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_state_change_event

from . import DOMAIN
from .ptz import PtzCancelled, PtzClient, PtzDeadlineExceeded, PtzStopUnconfirmed

_LOGGER = logging.getLogger(__name__)
DATA_KEY = f"{DOMAIN}_ptz"
SERVICE_NUDGE = "nudge"
SERVICE_STOP = "stop"
CONF_PTZ_CAMERAS = "ptz_cameras"
CONF_HOST = "host"
CONF_USERNAME = "username"
CONF_PASSWORD = "password"
CONF_PTZ_PATH = "ptz_path"
CONF_PROFILE_TOKEN = "profile_token"
CONF_PULSE_SECONDS_ENTITY = "pulse_seconds_entity"
CONF_INTERLOCKS = "interlocks"

_DIRECTIONS = vol.In({direction: direction for direction in ("left", "right", "up", "down")})
_NUDGE_SCHEMA = vol.Schema(
    {vol.Required(ATTR_ENTITY_ID): cv.entity_id, vol.Required("direction"): _DIRECTIONS}
)
_STOP_SCHEMA = vol.Schema({vol.Required(ATTR_ENTITY_ID): cv.entity_id})
_CAMERA_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(CONF_HOST): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(CONF_USERNAME): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(CONF_PASSWORD): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(CONF_PTZ_PATH): vol.All(cv.string, vol.Length(min=1)),
        vol.Required(CONF_PROFILE_TOKEN): vol.All(cv.string, vol.Length(min=1, max=256)),
        vol.Required(CONF_PULSE_SECONDS_ENTITY): cv.entity_id,
        vol.Optional(CONF_INTERLOCKS, default=[]): vol.All(cv.ensure_list, [cv.entity_id]),
    }
)


def _validate_camera(camera: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(camera, dict):
        raise vol.Invalid("PTZ camera must be a mapping")
    camera = _CAMERA_SCHEMA(camera)
    if not camera[ATTR_ENTITY_ID].startswith("camera."):
        raise vol.Invalid("entity_id must be a camera entity")
    if not camera[CONF_PULSE_SECONDS_ENTITY].startswith("input_number."):
        raise vol.Invalid("pulse_seconds_entity must be an input_number entity")
    try:
        PtzClient._validate_endpoint(camera[CONF_HOST], 8899, camera[CONF_PTZ_PATH])
    except (TypeError, ValueError) as err:
        raise vol.Invalid(str(err)) from None
    if any(ord(char) < 32 or ord(char) == 127 for char in camera[CONF_PROFILE_TOKEN]):
        raise vol.Invalid("profile_token contains control characters")
    return camera


def validate_ptz_config(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, (list, tuple)):
        raise vol.Invalid("ptz_cameras must be a list")
    cameras = [_validate_camera(camera) for camera in value]
    entity_ids = [camera[ATTR_ENTITY_ID] for camera in cameras]
    hosts = [camera[CONF_HOST] for camera in cameras]
    if len(set(entity_ids)) != len(entity_ids):
        raise vol.Invalid("PTZ camera entity IDs must be unique")
    if len(set(hosts)) != len(hosts):
        raise vol.Invalid("PTZ camera hosts must be unique")
    return cameras


@dataclass
class _Camera:
    config: dict[str, Any]
    client: PtzClient
    operation: "_Operation | None" = None
    stop_task: asyncio.Task | None = None
    stopping: bool = False
    baseline: bool = False
    safety_latched: bool = False


@dataclass
class _Operation:
    cancel: threading.Event
    worker: asyncio.Future | None = None
    listener_remove: Callable[[], None] | None = None


class PtzController:
    """Own configured camera operations until their device work is complete."""

    def __init__(self, hass: HomeAssistant, cameras: list[dict[str, Any]]) -> None:
        self.hass = hass
        self._shutting_down = False
        self._cameras = {
            config[ATTR_ENTITY_ID]: _Camera(
                config,
                PtzClient(
                    config[CONF_HOST],
                    config[CONF_USERNAME],
                    config[CONF_PASSWORD],
                    config[CONF_PTZ_PATH],
                    config[CONF_PROFILE_TOKEN],
                ),
            )
            for config in cameras
        }
        hass.services.async_register(DOMAIN, SERVICE_NUDGE, self.async_nudge, schema=_NUDGE_SCHEMA)
        hass.services.async_register(DOMAIN, SERVICE_STOP, self.async_stop, schema=_STOP_SCHEMA)
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.async_shutdown)

    async def _authorize(self, call: ServiceCall, entity_id: str) -> None:
        user_id = call.context.user_id
        if user_id is None:
            return
        user = await self.hass.auth.async_get_user(user_id)
        if user is None or not user.is_active or not user.permissions.check_entity(
            entity_id, POLICY_CONTROL
        ):
            raise HomeAssistantError("PTZ control is not permitted")

    def _camera(self, entity_id: str) -> _Camera:
        camera = self._cameras.get(entity_id)
        if camera is None:
            raise HomeAssistantError("PTZ camera is not configured")
        return camera

    def _camera_available(self, entity_id: str) -> bool:
        state = self.hass.states.get(entity_id)
        return state is not None and state.state not in {"unknown", "unavailable"}

    def _pulse(self, camera: _Camera) -> tuple[float, str]:
        state = self.hass.states.get(camera.config[CONF_PULSE_SECONDS_ENTITY])
        if state is None or state.state in {"unknown", "unavailable"}:
            raise HomeAssistantError("PTZ pulse duration is unavailable")
        try:
            value = float(state.state)
        except (TypeError, ValueError):
            raise HomeAssistantError("PTZ pulse duration is invalid") from None
        if not math.isfinite(value) or not 0.1 <= value <= 0.25:
            raise HomeAssistantError("PTZ pulse duration is invalid")
        return value, state.state

    def _interlocks_off(self, camera: _Camera) -> bool:
        return all(
            (state := self.hass.states.get(entity_id)) is not None and state.state == "off"
            for entity_id in camera.config.get(CONF_INTERLOCKS, [])
        )

    async def _drain(self, future: asyncio.Future | None) -> BaseException | None:
        if future is None:
            return None
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                if future.done():
                    try:
                        return future.exception()
                    except asyncio.CancelledError as err:
                        return err
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                continue
            except Exception as err:
                return err
        try:
            return future.exception()
        except asyncio.CancelledError as err:
            return err

    async def _drain_nudge(
        self, future: asyncio.Future | None, cancel: threading.Event
    ) -> tuple[BaseException | None, bool]:
        if future is None:
            return None, False
        caller_cancelled = False
        while not future.done():
            try:
                await asyncio.shield(future)
            except asyncio.CancelledError:
                caller_cancelled = True
                cancel.set()
                current = asyncio.current_task()
                if current is not None:
                    current.uncancel()
                continue
            except Exception as err:
                return err, caller_cancelled
        try:
            return future.exception(), caller_cancelled
        except asyncio.CancelledError as err:
            return err, caller_cancelled

    async def _executor(self, target, *args):
        return await self.hass.async_add_executor_job(target, *args)

    async def _explicit_stop(self, camera: _Camera) -> None:
        try:
            await self._executor(camera.client.stop)
        except Exception:
            camera.safety_latched = True
            self._notify_stop_failure(camera.config[ATTR_ENTITY_ID])
            raise PtzStopUnconfirmed("PTZ stop was not confirmed") from None
        camera.safety_latched = False
        persistent_notification.async_dismiss(
            self.hass, f"{DOMAIN}_ptz_stop_failed_{camera.config[ATTR_ENTITY_ID].replace('.', '_')}"
        )

    def _notify_stop_failure(self, entity_id: str) -> None:
        _LOGGER.error("JOOAN PTZ safety stop was not confirmed for %s", entity_id)
        persistent_notification.async_create(
            self.hass,
            "A JOOAN PTZ safety stop could not be confirmed. Use Stop before retrying.",
            title=f"JOOAN PTZ safety stop failed ({entity_id})",
            notification_id=f"{DOMAIN}_ptz_stop_failed_{entity_id.replace('.', '_')}",
        )

    def _watch(
        self, camera: _Camera, operation: _Operation, entity_id: str, helper_state: str
    ) -> None:
        helper_entity = camera.config[CONF_PULSE_SECONDS_ENTITY]
        watched = [entity_id, helper_entity, *camera.config.get(CONF_INTERLOCKS, [])]

        @callback
        def changed(event) -> None:
            state = event.data.get("new_state")
            value = state.state if state is not None else None
            changed_entity = event.data["entity_id"]
            if changed_entity == entity_id:
                invalid = value in {None, "unknown", "unavailable"}
            elif changed_entity == helper_entity:
                invalid = value != helper_state
            else:
                invalid = value != "off"
            if invalid:
                operation.cancel.set()

        operation.listener_remove = async_track_state_change_event(self.hass, watched, changed)

    async def async_nudge(self, call: ServiceCall) -> None:
        entity_id = call.data[ATTR_ENTITY_ID]
        await self._authorize(call, entity_id)
        camera = self._camera(entity_id)
        if self._shutting_down or camera.operation is not None or camera.stopping:
            raise HomeAssistantError("PTZ camera is already owned by another operation")
        if camera.safety_latched:
            raise HomeAssistantError("PTZ safety stop is required before moving")
        if not self._camera_available(entity_id):
            raise HomeAssistantError("PTZ camera is unavailable")
        if not self._interlocks_off(camera):
            raise HomeAssistantError("PTZ interlock is active")
        duration, helper_state = self._pulse(camera)
        deadline = self.hass.loop.time() + 2.0
        operation = _Operation(threading.Event())
        camera.operation = operation
        self._watch(camera, operation, entity_id, helper_state)
        try:
            if (
                self._shutting_down
                or not self._camera_available(entity_id)
                or self.hass.states.get(camera.config[CONF_PULSE_SECONDS_ENTITY]) is None
                or self.hass.states.get(camera.config[CONF_PULSE_SECONDS_ENTITY]).state != helper_state
                or not self._interlocks_off(camera)
            ):
                operation.cancel.set()
                raise HomeAssistantError("PTZ authorization changed")
            partial = functools.partial(
                camera.client.nudge,
                call.data["direction"],
                duration,
                deadline_monotonic=deadline,
                cancellation=operation.cancel,
                initial_stop=not camera.baseline,
            )
            operation.worker = asyncio.ensure_future(self.hass.async_add_executor_job(partial))
            error, caller_cancelled = await self._drain_nudge(
                operation.worker, operation.cancel
            )
            if error is not None:
                owns_operation = camera.operation is operation
                if isinstance(error, PtzStopUnconfirmed) and owns_operation:
                    camera.safety_latched = True
                    self._notify_stop_failure(entity_id)
                if caller_cancelled:
                    raise asyncio.CancelledError
                if isinstance(error, (PtzCancelled, PtzDeadlineExceeded)):
                    raise HomeAssistantError("PTZ operation expired or canceled") from None
                raise HomeAssistantError("PTZ movement failed") from None
            if camera.operation is operation:
                camera.baseline = True
            if caller_cancelled:
                raise asyncio.CancelledError
        finally:
            if operation.listener_remove:
                operation.listener_remove()
                operation.listener_remove = None
            if camera.operation is operation:
                camera.operation = None

    async def _run_stop(self, camera: _Camera) -> None:
        camera.stopping = True
        operation = camera.operation
        if operation is not None:
            operation.cancel.set()
        error = await self._drain(operation.worker if operation is not None else None)
        if error is not None and not isinstance(error, (PtzCancelled, PtzDeadlineExceeded)):
            camera.safety_latched = True
        try:
            await self._explicit_stop(camera)
            camera.baseline = True
        finally:
            if camera.operation is operation and operation is not None:
                if operation.listener_remove:
                    operation.listener_remove()
                    operation.listener_remove = None
                camera.operation = None
            if camera.stop_task is asyncio.current_task():
                camera.stop_task = None
                camera.stopping = False

    async def async_stop(self, call: ServiceCall) -> None:
        entity_id = call.data[ATTR_ENTITY_ID]
        await self._authorize(call, entity_id)
        camera = self._camera(entity_id)
        camera.stopping = True
        if camera.stop_task is None or camera.stop_task.done():
            task = asyncio.create_task(self._run_stop(camera))
            camera.stop_task = task
        else:
            task = camera.stop_task
        cancelled = False
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                cancelled = True
                continue
            except Exception:
                break
        error = task.exception()
        if isinstance(error, PtzStopUnconfirmed):
            raise HomeAssistantError("PTZ stop was not confirmed") from None
        if cancelled:
            raise asyncio.CancelledError
        if error is not None:
            raise error

    async def async_shutdown(self, _event: Any) -> None:
        self._shutting_down = True
        tasks = []
        for camera in self._cameras.values():
            if camera.stop_task is None or camera.stop_task.done():
                if camera.operation is None:
                    continue
                camera.stop_task = asyncio.create_task(self._run_stop(camera))
            tasks.append(camera.stop_task)
        for task in tasks:
            await self._drain(task)


async def async_setup(hass: HomeAssistant, cameras: list[dict[str, Any]]) -> PtzController:
    controller = PtzController(hass, cameras)
    hass.data[DATA_KEY] = controller
    return controller
