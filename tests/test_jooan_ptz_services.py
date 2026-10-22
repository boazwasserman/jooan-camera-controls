from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
import voluptuous as vol
from homeassistant.core import Context, ServiceCall
from homeassistant.exceptions import HomeAssistantError

import custom_components.jooan_night_vision.ptz_services as ptz_services
from custom_components.jooan_night_vision import DOMAIN
from custom_components.jooan_night_vision.ptz import PtzCancelled, PtzStopUnconfirmed
from custom_components.jooan_night_vision.ptz_services import (
    CONF_HOST,
    CONF_INTERLOCKS,
    CONF_PROFILE_TOKEN,
    CONF_PTZ_PATH,
    CONF_PULSE_SECONDS_ENTITY,
    CONF_USERNAME,
    validate_ptz_config,
)


def camera(entity_id="camera.example", host="192.168.50.10"):
    return {
        "entity_id": entity_id,
        CONF_HOST: host,
        CONF_USERNAME: "ptz-user",
        "password": "ptz-password",
        CONF_PTZ_PATH: "/onvif/ptz_service",
        CONF_PROFILE_TOKEN: "profile-explicit",
        CONF_PULSE_SECONDS_ENTITY: "input_number.example_ptz_pulse",
        CONF_INTERLOCKS: [],
    }


def test_config_requires_unique_camera_entities_and_hosts():
    with pytest.raises(vol.Invalid):
        validate_ptz_config([camera(), camera()])
    with pytest.raises(vol.Invalid):
        validate_ptz_config([camera(), camera("camera.example_two", "192.168.50.10")])


@pytest.mark.parametrize(
    "changes",
    [
        {"entity_id": "light.example"},
        {CONF_HOST: "8.8.8.8"},
        {CONF_PTZ_PATH: "/onvif/../cgi"},
        {CONF_PROFILE_TOKEN: ""},
        {CONF_PROFILE_TOKEN: "x" * 257},
        {CONF_PULSE_SECONDS_ENTITY: "sensor.pulse"},
    ],
)
def test_config_rejects_unqualified_mapping(changes):
    config = camera()
    config.update(changes)
    with pytest.raises(vol.Invalid):
        validate_ptz_config([config])


def test_config_accepts_two_distinct_local_mappings():
    result = validate_ptz_config(
        [camera(), camera("camera.example_two", "192.168.50.12")]
    )
    assert [entry["entity_id"] for entry in result] == [
        "camera.example",
        "camera.example_two",
    ]


@pytest.mark.asyncio
async def test_queued_worker_is_canceled_when_pulse_helper_changes(hass, monkeypatch):
    config = camera()
    config[CONF_PULSE_SECONDS_ENTITY] = "input_number.example_ptz_pulse_seconds"
    hass.states.async_set("camera.example", "idle")
    hass.states.async_set(config[CONF_PULSE_SECONDS_ENTITY], "0.1")

    queued = asyncio.Event()
    calls = []

    class FakeClient:
        def __init__(self, *args):
            pass

        def nudge(self, *args, **kwargs):
            calls.append(("nudge", kwargs["cancellation"].is_set()))
            if kwargs["cancellation"].is_set():
                from custom_components.jooan_night_vision.ptz import PtzCancelled

                raise PtzCancelled("guard changed")

        def stop(self):
            calls.append(("stop", False))

    monkeypatch.setattr(ptz_services, "PtzClient", FakeClient)

    async def queued_executor(func, *args, **kwargs):
        await queued.wait()
        return func(*args, **kwargs)

    monkeypatch.setattr(hass, "async_add_executor_job", queued_executor)
    controller = ptz_services.PtzController(hass, [config])
    call = ServiceCall(
        hass,
        DOMAIN,
        "nudge",
        {"entity_id": "camera.example", "direction": "left"},
        Context(),
    )
    task = hass.async_create_task(controller.async_nudge(call))
    await asyncio.sleep(0)
    hass.states.async_set(config[CONF_PULSE_SECONDS_ENTITY], "0.15")
    queued.set()
    with pytest.raises(HomeAssistantError, match="expired or canceled"):
        await task
    assert calls == [("nudge", True)]


@pytest.mark.asyncio
@pytest.mark.parametrize("active,allowed,expected", [(True, True, True), (False, True, False), (True, False, False)])
async def test_actual_service_dispatch_awaits_auth_and_binds_executor(
    hass, monkeypatch, active, allowed, expected
):
    config = camera()
    hass.states.async_set("camera.example", "idle")
    hass.states.async_set(config[CONF_PULSE_SECONDS_ENTITY], "0.1")
    calls = []

    class FakeClient:
        def __init__(self, *args):
            pass

        def nudge(self, *args, **kwargs):
            calls.append("nudge")
            calls.append("stop")

        def stop(self):
            calls.append("stop")

    monkeypatch.setattr(ptz_services, "PtzClient", FakeClient)
    user = SimpleNamespace(
        is_active=active,
        permissions=SimpleNamespace(check_entity=lambda *_args: allowed),
    )

    async def get_user(_user_id):
        return user

    monkeypatch.setattr(hass.auth, "async_get_user", get_user)
    ptz_services.PtzController(hass, [config])
    context = Context(user_id="user-id")
    if expected:
        await hass.services.async_call(
            DOMAIN,
            "nudge",
            {"entity_id": "camera.example", "direction": "left"},
            context=context,
            blocking=True,
        )
        assert calls == ["nudge", "stop"]
    else:
        with pytest.raises(HomeAssistantError):
            await hass.services.async_call(
                DOMAIN,
                "nudge",
                {"entity_id": "camera.example", "direction": "left"},
                context=context,
                blocking=True,
            )
        assert calls == []


@pytest.mark.asyncio
async def test_unauthorized_stop_and_controller_startup_send_no_device_request(hass, monkeypatch):
    config = camera()
    _ready_states(hass, config)
    calls = []

    class Client:
        def __init__(self, *args):
            pass

        def stop(self):
            calls.append("stop")

    monkeypatch.setattr(ptz_services, "PtzClient", Client)
    user = SimpleNamespace(
        is_active=True,
        permissions=SimpleNamespace(check_entity=lambda *_args: False),
    )

    async def get_user(_user_id):
        return user

    monkeypatch.setattr(hass.auth, "async_get_user", get_user)
    ptz_services.PtzController(hass, [config])
    assert calls == []
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "stop",
            {"entity_id": "camera.example"},
            context=Context(user_id="user-id"),
            blocking=True,
        )
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"entity_id": ["camera.example"], "direction": "left"},
        {"entity_id": "camera.example", "direction": "diagonal"},
        {"entity_id": "camera.example", "direction": "left", "host": "192.168.50.19"},
        {"entity_id": "camera.example", "direction": "left", "profile_token": "x"},
        {"entity_id": "camera.example", "direction": "left", "url": "http://127.0.0.1"},
        {"entity_id": "camera.example", "direction": "left"},
    ],
)
async def test_registered_nudge_service_rejects_malformed_or_unconfigured_payloads(
    hass, monkeypatch, payload
):
    config = camera()
    _ready_states(hass, config)
    calls = []

    class Client:
        def __init__(self, *args):
            pass

        def nudge(self, *args, **kwargs):
            calls.append("nudge")

    monkeypatch.setattr(ptz_services, "PtzClient", Client)
    ptz_services.PtzController(hass, [config])
    if payload["entity_id"] == "camera.example" and len(payload) == 2:
        payload = {**payload, "entity_id": "camera.not_configured"}
    with pytest.raises((HomeAssistantError, vol.Invalid)):
        await hass.services.async_call(
            DOMAIN, "nudge", payload, blocking=True
        )
    assert calls == []


@pytest.mark.asyncio
async def test_missing_authenticated_user_is_denied(hass, monkeypatch):
    config = camera()
    _ready_states(hass, config)
    calls = []

    class Client:
        def __init__(self, *args):
            pass

        def nudge(self, *args, **kwargs):
            calls.append("nudge")

    async def get_user(_user_id):
        return None

    monkeypatch.setattr(ptz_services, "PtzClient", Client)
    monkeypatch.setattr(hass.auth, "async_get_user", get_user)
    ptz_services.PtzController(hass, [config])
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            DOMAIN,
            "nudge",
            {"entity_id": "camera.example", "direction": "left"},
            context=Context(user_id="missing"),
            blocking=True,
        )
    assert calls == []


@pytest.mark.asyncio
async def test_stop_dispatches_when_movement_guards_are_unavailable(hass, monkeypatch):
    config = camera()
    config[CONF_INTERLOCKS] = ["input_boolean.ptz_interlock"]
    hass.states.async_set("camera.example", "unavailable")
    hass.states.async_set(config[CONF_PULSE_SECONDS_ENTITY], "unknown")
    hass.states.async_set("input_boolean.ptz_interlock", "unknown")
    calls = []

    class Client:
        def __init__(self, *args):
            pass

        def stop(self):
            calls.append("stop")

    monkeypatch.setattr(ptz_services, "PtzClient", Client)
    ptz_services.PtzController(hass, [config])
    await hass.services.async_call(
        DOMAIN,
        "stop",
        {"entity_id": "camera.example"},
        blocking=True,
    )
    assert calls == ["stop"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "camera_state,helper_state,interlock_state",
    [
        ("unavailable", "0.1", "off"),
        ("idle", "unknown", "off"),
        ("idle", "0.1", "on"),
    ],
)
async def test_nudge_fails_closed_for_camera_helper_and_interlock(
    hass, monkeypatch, camera_state, helper_state, interlock_state
):
    config = camera()
    config[CONF_INTERLOCKS] = ["input_boolean.ptz_interlock"]
    hass.states.async_set("camera.example", camera_state)
    hass.states.async_set(config[CONF_PULSE_SECONDS_ENTITY], helper_state)
    hass.states.async_set("input_boolean.ptz_interlock", interlock_state)
    calls = []

    class Client:
        def __init__(self, *args):
            pass

        def nudge(self, *args, **kwargs):
            calls.append("nudge")

    monkeypatch.setattr(ptz_services, "PtzClient", Client)
    controller = ptz_services.PtzController(hass, [config])
    with pytest.raises(HomeAssistantError):
        await controller.async_nudge(_service_call(hass, "nudge"))
    assert calls == []


class _ControlledClient:
    def __init__(
        self,
        *args,
        nudge_started=None,
        nudge_release=None,
        stop_started=None,
        stop_release=None,
        raise_on_cancel=False,
    ):
        self.nudge_started = nudge_started if nudge_started is not None else threading.Event()
        self.nudge_release = nudge_release if nudge_release is not None else threading.Event()
        self.stop_started = stop_started if stop_started is not None else threading.Event()
        self.stop_release = stop_release if stop_release is not None else threading.Event()
        self.raise_on_cancel = raise_on_cancel
        self.calls = []

    def nudge(self, direction, duration, *, cancellation, **_kwargs):
        self.calls.append(("nudge", direction))
        self.nudge_started.set()
        deadline = time.monotonic() + 5
        while not self.nudge_release.wait(0.01):
            if time.monotonic() >= deadline:
                raise RuntimeError("nudge barrier was not released")
        self.calls.append(("nudge_stop", cancellation.is_set()))
        if cancellation.is_set():
            if self.raise_on_cancel:
                raise PtzCancelled("canceled")
            return

    def stop(self):
        self.calls.append(("stop",))
        self.stop_started.set()
        deadline = time.monotonic() + 5
        while not self.stop_release.wait(0.01):
            if time.monotonic() >= deadline:
                raise RuntimeError("stop barrier was not released")


class _WorkerCleanup:
    def __init__(self, *clients):
        self.clients = clients
        self.tasks = []

    def track(self, *tasks):
        self.tasks.extend(tasks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        for client in self.clients:
            client.nudge_release.set()
            client.stop_release.set()
        if self.tasks:
            await asyncio.wait_for(
                asyncio.gather(*self.tasks, return_exceptions=True), 3
            )
        return False


async def _wait_started(event):
    assert await asyncio.wait_for(asyncio.to_thread(event.wait, 2), 3)


def _service_call(hass, service, entity_id="camera.example", direction="left"):
    return ServiceCall(
        hass,
        DOMAIN,
        service,
        {"entity_id": entity_id, **({"direction": direction} if service == "nudge" else {})},
        Context(),
    )


def _ready_states(hass, config):
    hass.states.async_set(config[ptz_services.ATTR_ENTITY_ID], "idle")
    hass.states.async_set(config[CONF_PULSE_SECONDS_ENTITY], "0.1")


@pytest.mark.asyncio
async def test_canceled_active_nudge_keeps_ownership_until_worker_and_safety_stop_finish(
    hass, monkeypatch
):
    config = camera()
    _ready_states(hass, config)
    client = _ControlledClient()
    monkeypatch.setattr(ptz_services, "PtzClient", lambda *args: client)
    controller = ptz_services.PtzController(hass, [config])
    async with _WorkerCleanup(client) as cleanup:
        task = hass.async_create_task(controller.async_nudge(_service_call(hass, "nudge")))
        cleanup.track(task)
        await _wait_started(client.nudge_started)
        task.cancel()
        await asyncio.sleep(0)
        assert controller._cameras["camera.example"].operation is not None
        client.nudge_release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 2)
    assert controller._cameras["camera.example"].operation is None
    assert client.calls == [("nudge", "left"), ("nudge_stop", True)]


@pytest.mark.asyncio
async def test_stop_preempts_and_coalesces_and_blocks_new_nudge_until_finished(hass, monkeypatch):
    config = camera()
    _ready_states(hass, config)
    client = _ControlledClient(raise_on_cancel=True)
    monkeypatch.setattr(ptz_services, "PtzClient", lambda *args: client)
    controller = ptz_services.PtzController(hass, [config])
    async with _WorkerCleanup(client) as cleanup:
        nudge = hass.async_create_task(controller.async_nudge(_service_call(hass, "nudge")))
        cleanup.track(nudge)
        await _wait_started(client.nudge_started)
        stop_one = hass.async_create_task(controller.async_stop(_service_call(hass, "stop")))
        stop_two = hass.async_create_task(controller.async_stop(_service_call(hass, "stop")))
        cleanup.track(stop_one, stop_two)
        await asyncio.sleep(0)
        with pytest.raises(HomeAssistantError, match="already owned"):
            await controller.async_nudge(_service_call(hass, "nudge"))
        assert not client.stop_started.is_set()
        client.nudge_release.set()
        await _wait_started(client.stop_started)
        assert client.calls.count(("stop",)) == 1
        client.stop_release.set()
        with pytest.raises(HomeAssistantError, match="expired or canceled"):
            await nudge
        await asyncio.gather(stop_one, stop_two)
    assert controller._cameras["camera.example"].stopping is False


@pytest.mark.asyncio
async def test_canceled_stop_caller_does_not_abandon_real_stop(hass, monkeypatch):
    config = camera()
    _ready_states(hass, config)
    client = _ControlledClient()
    client.nudge_release.set()
    monkeypatch.setattr(ptz_services, "PtzClient", lambda *args: client)
    controller = ptz_services.PtzController(hass, [config])
    async with _WorkerCleanup(client) as cleanup:
        nudge = hass.async_create_task(controller.async_nudge(_service_call(hass, "nudge")))
        cleanup.track(nudge)
        await _wait_started(client.nudge_started)
        client.nudge_release.set()
        await nudge
        stop = hass.async_create_task(controller.async_stop(_service_call(hass, "stop")))
        cleanup.track(stop)
        await _wait_started(client.stop_started)
        stop.cancel()
        await asyncio.sleep(0)
        assert client.stop_started.is_set()
        assert not stop.done()
        with pytest.raises(HomeAssistantError, match="already owned"):
            await controller.async_nudge(_service_call(hass, "nudge"))
        client.stop_release.set()
        with pytest.raises(asyncio.CancelledError):
            await stop
    assert client.calls.count(("stop",)) == 1


@pytest.mark.asyncio
async def test_failed_stop_latches_and_successful_explicit_stop_clears(hass, monkeypatch):
    config = camera()
    _ready_states(hass, config)
    calls = []
    failures = [True, False]
    notifications = []
    dismissals = []
    monkeypatch.setattr(
        ptz_services.persistent_notification,
        "async_create",
        lambda *_args, **kwargs: notifications.append(kwargs),
    )
    monkeypatch.setattr(
        ptz_services.persistent_notification,
        "async_dismiss",
        lambda _hass, notification_id: dismissals.append(notification_id),
    )

    class Client:
        def __init__(self, *args):
            pass

        def nudge(self, *args, **kwargs):
            calls.append("nudge")

        def stop(self):
            calls.append("stop")
            if failures.pop(0):
                raise RuntimeError("device failure")

    monkeypatch.setattr(ptz_services, "PtzClient", Client)
    controller = ptz_services.PtzController(hass, [config])
    with pytest.raises(HomeAssistantError, match="stop was not confirmed"):
        await controller.async_stop(_service_call(hass, "stop"))
    assert controller._cameras["camera.example"].safety_latched
    assert len(notifications) == 1
    assert dismissals == []
    with pytest.raises(HomeAssistantError, match="required"):
        await controller.async_nudge(_service_call(hass, "nudge"))
    await controller.async_stop(_service_call(hass, "stop"))
    assert not controller._cameras["camera.example"].safety_latched
    assert dismissals == [notifications[0]["notification_id"]]
    assert calls == ["stop", "stop"]


@pytest.mark.asyncio
async def test_cameras_have_independent_operation_ownership(hass, monkeypatch):
    configs = [camera(), camera("camera.example_two", "192.168.50.12")]
    for config in configs:
        _ready_states(hass, config)
    clients = {}

    class Client(_ControlledClient):
        def __init__(self, host, *args):
            super().__init__(raise_on_cancel=True)
            clients[host] = self

    monkeypatch.setattr(ptz_services, "PtzClient", Client)
    controller = ptz_services.PtzController(hass, configs)
    async with _WorkerCleanup(*clients.values()) as cleanup:
        first = hass.async_create_task(
            controller.async_nudge(_service_call(hass, "nudge"))
        )
        second = hass.async_create_task(
            controller.async_nudge(_service_call(hass, "nudge", "camera.example_two"))
        )
        cleanup.track(first, second)
        await _wait_started(clients["192.168.50.10"].nudge_started)
        await _wait_started(clients["192.168.50.12"].nudge_started)
        assert all(client.calls[0][0] == "nudge" for client in clients.values())
        clients["192.168.50.10"].nudge_release.set()
        clients["192.168.50.12"].nudge_release.set()
        await asyncio.gather(first, second)


@pytest.mark.asyncio
async def test_shutdown_stops_active_cameras_but_does_not_write_idle_cameras(hass, monkeypatch):
    configs = [camera(), camera("camera.example_two", "192.168.50.12")]
    _ready_states(hass, configs[0])
    hass.states.async_set(configs[1][ptz_services.ATTR_ENTITY_ID], "idle")
    hass.states.async_set(configs[1][CONF_PULSE_SECONDS_ENTITY], "0.1")
    clients = {}

    class Client(_ControlledClient):
        def __init__(self, host, *args):
            super().__init__(raise_on_cancel=True)
            clients[host] = self

    monkeypatch.setattr(ptz_services, "PtzClient", Client)
    controller = ptz_services.PtzController(hass, configs)
    async with _WorkerCleanup(*clients.values()) as cleanup:
        nudge = hass.async_create_task(controller.async_nudge(_service_call(hass, "nudge")))
        cleanup.track(nudge)
        await _wait_started(clients["192.168.50.10"].nudge_started)
        shutdown = hass.async_create_task(controller.async_shutdown({}))
        cleanup.track(shutdown)
        await asyncio.sleep(0)
        clients["192.168.50.10"].nudge_release.set()
        await _wait_started(clients["192.168.50.10"].stop_started)
        assert not shutdown.done()
        clients["192.168.50.10"].stop_release.set()
        with pytest.raises(HomeAssistantError, match="expired or canceled"):
            await nudge
        await shutdown
        assert clients["192.168.50.12"].calls == []


@pytest.mark.asyncio
async def test_shutdown_drains_existing_idle_stop_without_duplicate_stop(hass, monkeypatch):
    config = camera()
    _ready_states(hass, config)
    client = _ControlledClient()
    client.nudge_release.set()
    monkeypatch.setattr(ptz_services, "PtzClient", lambda *args: client)
    controller = ptz_services.PtzController(hass, [config])
    async with _WorkerCleanup(client) as cleanup:
        stop = hass.async_create_task(controller.async_stop(_service_call(hass, "stop")))
        cleanup.track(stop)
        await _wait_started(client.stop_started)
        shutdown = hass.async_create_task(controller.async_shutdown({}))
        cleanup.track(shutdown)
        await asyncio.sleep(0)
        assert not shutdown.done()
        assert client.calls.count(("stop",)) == 1
        client.stop_release.set()
        await asyncio.gather(stop, shutdown)
        assert client.calls.count(("stop",)) == 1


@pytest.mark.asyncio
async def test_delayed_old_nudge_finalizer_cannot_clear_new_operation(hass, monkeypatch):
    config = camera()
    _ready_states(hass, config)
    client = _ControlledClient()
    client.nudge_release.set()
    client.stop_release.set()
    monkeypatch.setattr(ptz_services, "PtzClient", lambda *args: client)
    controller = ptz_services.PtzController(hass, [config])
    original_drain = controller._drain_nudge
    old_drain_released = asyncio.Event()
    old_drain_entered = asyncio.Event()
    drain_calls = 0

    async def delayed_drain(future, cancel):
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 1:
            old_drain_entered.set()
            await old_drain_released.wait()
            return PtzCancelled("old operation"), False
        return await original_drain(future, cancel)

    monkeypatch.setattr(controller, "_drain_nudge", delayed_drain)
    old_nudge = hass.async_create_task(
        controller.async_nudge(_service_call(hass, "nudge"))
    )
    async with _WorkerCleanup(client) as cleanup:
        cleanup.track(old_nudge)
        await asyncio.wait_for(old_drain_entered.wait(), 2)
        await controller.async_stop(_service_call(hass, "stop"))
        client.nudge_release.clear()
        client.nudge_started.clear()
        new_nudge = hass.async_create_task(
            controller.async_nudge(_service_call(hass, "nudge"))
        )
        cleanup.track(new_nudge)
        await _wait_started(client.nudge_started)
        new_operation = controller._cameras["camera.example"].operation
        assert new_operation is not None
        with pytest.raises(HomeAssistantError, match="already owned"):
            await controller.async_nudge(_service_call(hass, "nudge"))
        old_drain_released.set()
        with pytest.raises(HomeAssistantError, match="expired or canceled"):
            await old_nudge
        assert controller._cameras["camera.example"].operation is new_operation
        client.nudge_release.set()
        await new_nudge
    assert controller._cameras["camera.example"].operation is None


@pytest.mark.asyncio
async def test_delayed_old_stop_failure_cannot_latch_after_new_stop_and_nudge(
    hass, monkeypatch
):
    config = camera()
    _ready_states(hass, config)
    client = _ControlledClient()
    client.nudge_release.set()
    client.stop_release.set()
    notifications = []
    monkeypatch.setattr(
        ptz_services.persistent_notification,
        "async_create",
        lambda *_args, **kwargs: notifications.append(kwargs),
    )
    monkeypatch.setattr(ptz_services, "PtzClient", lambda *args: client)
    controller = ptz_services.PtzController(hass, [config])
    original_drain = controller._drain_nudge
    old_drain_released = asyncio.Event()
    old_drain_entered = asyncio.Event()
    drain_calls = 0

    async def delayed_drain(future, cancel):
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 1:
            old_drain_entered.set()
            await old_drain_released.wait()
            return PtzStopUnconfirmed("old safety stop failed"), False
        return await original_drain(future, cancel)

    monkeypatch.setattr(controller, "_drain_nudge", delayed_drain)
    old_nudge = hass.async_create_task(
        controller.async_nudge(_service_call(hass, "nudge"))
    )
    async with _WorkerCleanup(client) as cleanup:
        cleanup.track(old_nudge)
        await asyncio.wait_for(old_drain_entered.wait(), 2)
        await controller.async_stop(_service_call(hass, "stop"))
        client.nudge_release.clear()
        client.nudge_started.clear()
        new_nudge = hass.async_create_task(
            controller.async_nudge(_service_call(hass, "nudge"))
        )
        cleanup.track(new_nudge)
        await _wait_started(client.nudge_started)
        new_operation = controller._cameras["camera.example"].operation
        assert new_operation is not None
        with pytest.raises(HomeAssistantError, match="already owned"):
            await controller.async_nudge(_service_call(hass, "nudge"))
        old_drain_released.set()
        with pytest.raises(HomeAssistantError, match="movement failed"):
            await old_nudge
        assert controller._cameras["camera.example"].operation is new_operation
        assert not controller._cameras["camera.example"].safety_latched
        assert notifications == []
        client.nudge_release.set()
        await new_nudge
