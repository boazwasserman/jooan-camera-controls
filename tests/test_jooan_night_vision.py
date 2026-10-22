"""Deterministic transport and entity tests for JOOAN night vision."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from paho.mqtt.packettypes import PacketTypes
from paho.mqtt.reasoncodes import ReasonCode

from custom_components.jooan_night_vision import client
from custom_components.jooan_night_vision.select import (
    OPTIONS,
    PLATFORM_SCHEMA,
    JooanNightVisionSelect,
    async_setup_platform,
)


class FakeResponse:
    def __init__(self, payload, status_code=200, close_error=None):
        self.payload = json.dumps(payload).encode()
        self.status_code = status_code
        self.close_error = close_error
        self.closed = False

    def iter_content(self, chunk_size=8192):
        yield self.payload

    def close(self):
        self.closed = True
        if self.close_error:
            raise self.close_error


class FakeSession:
    instances = []
    response_factory = None

    def __init__(self):
        self.trust_env = True
        self.requests = []
        self.closed = False
        self.responses = [
            {
                "error_code": "0",
                "body": {"user_id": "fresh-user", "token": "fresh-token"},
            },
            {
                "error_code": "0",
                "device_info_list": [
                    {
                        "jooan_info": {
                            "properties": {
                                "device_id": "camera-one-uid",
                                "device_pwd": "redacted",
                            }
                        }
                    },
                    {"jooan_info": {"properties": {"device_id": "other-camera-uid"}}},
                ],
            },
            {
                "error_code": "0",
                "device_mqtt_address": [
                    {
                        "device_id": "camera-one-uid",
                        "cluster_id": "17",
                        "url": "broker.jooaniot.com:443",
                    }
                ],
            },
            {"error_code": "0"},
        ]
        self.__class__.instances.append(self)

    def post(self, url, **kwargs):
        self.requests.append((url, kwargs))
        response_factory = type(self).response_factory
        response = (
            response_factory(self, url, kwargs)
            if response_factory
            else FakeResponse(self.responses.pop(0))
        )
        return response

    def close(self):
        self.closed = True


class FakeInfo:
    rc = client.mqtt.MQTT_ERR_SUCCESS
    mid = 44


class FakeMqtt:
    instance = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.disconnected = False
        self.stopped = False
        self.__class__.instance = self

    def username_pw_set(self, username, password):
        self.username = (username, password)

    def tls_set_context(self, context):
        self.tls_context = context

    def connect(self, host, port, keepalive):
        self.connect_args = (host, port, keepalive)
        return client.mqtt.MQTT_ERR_SUCCESS

    def loop_start(self):
        self.on_connect(
            self, None, {}, ReasonCode(PacketTypes.CONNACK, identifier=0), None
        )
        return client.mqtt.MQTT_ERR_SUCCESS

    def subscribe(self, topic, qos):
        self.subscribed = (topic, qos)
        self.on_subscribe(
            self, None, 8, [ReasonCode(PacketTypes.SUBACK, identifier=1)], None
        )
        return client.mqtt.MQTT_ERR_SUCCESS, 8

    def publish(self, topic, payload, qos, retain):
        self.published = (topic, json.loads(payload), qos, retain)
        self.on_publish(
            self, None, 44, ReasonCode(PacketTypes.PUBACK, identifier=0), None
        )
        self.on_message(
            self,
            None,
            SimpleNamespace(
                topic="qaiot/mqtt/user/camera-one-uid",
                retain=False,
                payload=json.dumps(
                    {"cmd_type": "response", "cmd": 66435, "value": 2}
                ).encode(),
            ),
        )
        return FakeInfo()

    def disconnect(self):
        self.disconnected = True
        return client.mqtt.MQTT_ERR_SUCCESS

    def loop_stop(self):
        self.stopped = True
        return client.mqtt.MQTT_ERR_SUCCESS


def _patch_transport(monkeypatch):
    FakeSession.instances.clear()
    FakeMqtt.instance = None
    monkeypatch.setattr(client.requests, "Session", FakeSession)
    monkeypatch.setattr(client.mqtt, "Client", FakeMqtt)
    monkeypatch.setattr(client.secrets, "token_hex", lambda _: "0123456789abcdef")


@pytest.mark.parametrize("mode", (0, 1, 2))
def test_mode_command_matches_actual_vendor_shapes_and_ack(monkeypatch, mode):
    _patch_transport(monkeypatch)

    def publish(self, topic, payload, qos, retain):
        self.published = (topic, json.loads(payload), qos, retain)
        self.on_publish(self, None, 44, SimpleNamespace(value=0), None)
        self.on_message(
            self,
            None,
            SimpleNamespace(
                topic="qaiot/mqtt/user/camera-one-uid",
                retain=False,
                payload=json.dumps(
                    {"cmd_type": "response", "cmd": 66435, "value": mode}
                ).encode(),
            ),
        )
        return FakeInfo()

    monkeypatch.setattr(FakeMqtt, "publish", publish)
    client.JooanClient(
        email="person@example.invalid",
        password="пароль",
        application_id="A" * 20,
        device_id="camera-one-uid",
    ).set_mode(mode)
    session = FakeSession.instances[0]
    assert session.trust_env is False
    assert session.closed is True
    assert session.requests[0][1]["verify"] is True
    assert session.requests[1][1]["json"]["header"]["client_version"] == "1.0.0.0"
    assert session.requests[2][1]["json"]["header"]["device_list"] == ["camera-one-uid"]
    assert FakeMqtt.instance.kwargs["reconnect_on_failure"] is False
    assert FakeMqtt.instance.subscribed == ("qaiot/mqtt/user/camera-one-uid", 1)
    assert FakeMqtt.instance.published == (
        "qaiot/mqtt/camera-one-uid",
        {"cmd_type": "request", "cmd": 66435, "value": mode},
        1,
        False,
    )
    assert FakeMqtt.instance.disconnected and FakeMqtt.instance.stopped


def test_reply_requires_request_phase_and_exact_user_topic():
    state = client._MqttState("uid", 2)
    message = SimpleNamespace(
        topic="qaiot/mqtt/uid",
        retain=False,
        payload=b'{"cmd_type":"response","cmd":66435,"value":2}',
    )
    state.on_message(None, None, message)
    assert state.reply_received is False
    state.request_phase = True
    state.on_message(None, None, message)
    assert state.reply_received is False
    message.topic = "qaiot/mqtt/user/uid"
    state.on_message(None, None, message)
    assert state.reply_received is True


def test_puback_wait_requires_matching_mid_and_reason_zero():
    state = client._MqttState("uid", 2)
    state.publish_mid = 7
    state.on_publish(None, None, 6, SimpleNamespace(value=0), None)
    assert state.wait_for_puback(0.001) is False
    state.on_publish(None, None, 7, SimpleNamespace(value=1), None)
    assert state.wait_for_puback(0.001) is False
    state.on_publish(None, None, 7, SimpleNamespace(value=0), None)
    assert state.wait_for_puback(0.001) is True


@pytest.mark.parametrize("grant", (0, True, "1"))
def test_suback_requires_exact_mid_and_qos1(grant):
    state = client._MqttState("uid", 2)
    state.subscription_mid = 9
    state.on_subscribe(None, None, 8, [SimpleNamespace(value=1)], None)
    assert state.wait_for_subscription(0.001) is False
    state.on_subscribe(None, None, 9, [SimpleNamespace(value=grant)], None)
    assert state.wait_for_subscription(0.001) is (
        type(grant) is int and grant == 1
    )


def test_connection_and_device_reply_timeouts_are_bounded():
    state = client._MqttState("uid", 2)
    assert state.connected.wait(0.001) is False
    state.publish_mid = 3
    assert state.wait_for_puback(0.001) is False
    assert state.wait_for_reply(0.001) is False


@pytest.mark.parametrize(
    "payload",
    [
        {"cmd_type": "response", "cmd": 66435, "value": 1},
        {"cmd_type": "response", "cmd": True, "value": 2},
        {"cmd_type": "response", "cmd": 66435, "value": True},
        {"cmd_type": "response", "cmd": 1, "value": 2},
        {"cmd_type": "response", "cmd": 66435, "value": 2, "deviceId": "wrong"},
    ],
)
def test_invalid_device_replies_are_rejected(payload):
    state = client._MqttState("uid", 2)
    state.request_phase = True
    state.on_message(
        None,
        None,
        SimpleNamespace(
            topic="qaiot/mqtt/user/uid", retain=False, payload=json.dumps(payload).encode()
        ),
    )
    assert state.reply_received is False


def test_schema_accepts_real_package_shape_and_unicode_credentials():
    config = PLATFORM_SCHEMA(
        {
            "platform": "jooan_night_vision",
            "email": "用户@example.invalid",
            "password": "пароль",
            "application_id": "X" * 20,
            "cameras": [
                {"key": "camera_one", "name": "Camera One", "device_id": "camera-one-uid"},
                {"key": "camera_two", "name": "Camera Two", "device_id": "camera-two-uid"},
            ],
        }
    )
    assert config["platform"] == "jooan_night_vision"


@pytest.mark.parametrize(
    "change",
    [
        {"application_id": "short"},
        {
            "cameras": [
                {"key": "one", "name": "One", "device_id": "same"},
                {"key": "two", "name": "Two", "device_id": "same"},
            ]
        },
    ],
)
def test_schema_rejects_invalid_appid_or_duplicate_device_ids(change):
    config = {
        "platform": "jooan_night_vision",
        "email": "a@example.invalid",
        "password": "secret",
        "application_id": "X" * 20,
        "cameras": [
            {"key": "one", "name": "One", "device_id": "one"},
            {"key": "two", "name": "Two", "device_id": "two"},
        ],
    }
    config.update(change)
    with pytest.raises(vol.Invalid):
        PLATFORM_SCHEMA(config)


@pytest.mark.parametrize(
    "camera",
    [
        {"key": "bad key", "name": "Bad", "device_id": "uid"},
        {"key": "good", "name": "Bad", "device_id": "uid/#"},
    ],
)
def test_schema_rejects_unsafe_key_or_uid(camera):
    config = {
        "platform": "jooan_night_vision",
        "email": "a@example.invalid",
        "password": "secret",
        "application_id": "X" * 20,
        "cameras": [camera],
    }
    with pytest.raises(vol.Invalid):
        PLATFORM_SCHEMA(config)


@pytest.mark.parametrize(
    "value",
    [
        "mqtts://broker.jooaniot.com:443",
        "broker.jooaniot.com/path:443",
        "broker.jooaniot.com:8444",
        "192.168.1.2:443",
        "broker.jooaniot.com:bad",
        "broker.jooaniot.com:443 user",
    ],
)
def test_broker_authority_is_strict(value):
    with pytest.raises(client.JooanClientError):
        client.validate_broker_url(value)


def test_http_status_and_response_close_are_fail_closed(monkeypatch):
    def response_factory(_session, _url, _kwargs):
        return FakeResponse({"error_code": "0"}, status_code=302)

    monkeypatch.setattr(FakeSession, "response_factory", response_factory)
    monkeypatch.setattr(client.requests, "Session", FakeSession)
    with pytest.raises(client.JooanClientError):
        client.JooanClient(
            email="a@example.invalid",
            password="secret",
            application_id="X" * 20,
            device_id="uid",
        ).set_mode(0)


def test_login_response_close_failure_still_logs_out(monkeypatch):
    class CloseLoginSession(FakeSession):
        def post(self, url, **kwargs):
            self.requests.append((url, kwargs))
            payload = self.responses.pop(0)
            return FakeResponse(
                payload,
                close_error=OSError("close") if url.endswith("pcLogin") else None,
            )

    monkeypatch.setattr(client.requests, "Session", CloseLoginSession)
    with pytest.raises(client.JooanClientError):
        client.JooanClient(
            email="a@example.invalid",
            password="secret",
            application_id="X" * 20,
            device_id="uid",
        ).set_mode(0)
    request_paths = [
        request[0].rsplit("/", 1)[-1] for request in FakeSession.instances[-1].requests
    ]
    assert request_paths == ["pcLogin", "pcLogout"]


class _FakeHass:
    def __init__(self, worker):
        self.worker = worker
        self.writes = 0

    def async_add_executor_job(self, _function, _mode):
        return asyncio.create_task(self.worker())


@pytest.mark.asyncio
async def test_entity_executor_scheduling_failure_clears_assumed_state():
    class BrokenHass:
        def async_add_executor_job(self, _function, _mode):
            raise RuntimeError("executor unavailable")

    entity = JooanNightVisionSelect(
        BrokenHass(),
        asyncio.Lock(),
        email="a@example.invalid",
        password="secret",
        application_id="X" * 20,
        key="front",
        name="Front",
        device_id="uid",
    )
    entity._attr_current_option = OPTIONS[0]
    entity._last_acknowledged_at = "previous"
    entity.async_write_ha_state = lambda: None
    with pytest.raises(HomeAssistantError):
        await entity.async_select_option(OPTIONS[1])
    assert entity.current_option is None
    assert entity._last_acknowledged_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("worker_error", (False, True))
async def test_entity_cancellation_holds_account_lock_until_worker_finishes(worker_error):
    started = asyncio.Event()
    release = asyncio.Event()

    async def worker():
        started.set()
        await release.wait()
        if worker_error:
            raise client.JooanClientError("worker failed")

    hass = _FakeHass(worker)
    entity = JooanNightVisionSelect(
        hass,
        asyncio.Lock(),
        email="a@example.invalid",
        password="secret",
        application_id="X" * 20,
        key="front",
        name="Front",
        device_id="uid",
    )
    entity.async_write_ha_state = lambda: None
    other = JooanNightVisionSelect(
        hass,
        entity._lock,
        email="a@example.invalid",
        password="secret",
        application_id="X" * 20,
        key="back",
        name="Back",
        device_id="back",
    )
    task = asyncio.create_task(entity.async_select_option(OPTIONS[0]))
    try:
        await started.wait()
        with pytest.raises(HomeAssistantError):
            await other.async_select_option(OPTIONS[1])
        task.cancel()
        await asyncio.sleep(0)
        assert entity._lock.locked() is True
        task.cancel()
        await asyncio.sleep(0)
        assert entity._lock.locked() is True
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert entity._lock.locked() is False
    assert entity.current_option is None


@pytest.mark.asyncio
async def test_platform_setup_reuses_account_lock_but_separates_accounts():
    class Hass:
        def __init__(self):
            self.data = {}

    hass = Hass()
    entities = []
    config = {
        "platform": "jooan_night_vision",
        "email": "A@example.invalid",
        "password": "secret",
        "application_id": "X" * 20,
        "cameras": [
            {"key": "front", "name": "Front", "device_id": "front"},
            {"key": "back", "name": "Back", "device_id": "back"},
        ],
    }
    await async_setup_platform(hass, config, entities.extend)
    second = dict(config, email="a@example.invalid")
    await async_setup_platform(hass, second, entities.extend)
    other = dict(config, email="other@example.invalid")
    await async_setup_platform(hass, other, entities.extend)
    assert entities[0]._lock is entities[2]._lock
    assert entities[0]._lock is not entities[4]._lock
    assert {entity.unique_id for entity in entities} == {
        "front_night_vision",
        "back_night_vision",
    }


def test_entity_starts_unknown_assumed_and_without_readback():
    entity = JooanNightVisionSelect(
        None,
        asyncio.Lock(),
        email="a@example.invalid",
        password="secret",
        application_id="X" * 20,
        key="camera_one",
        name="Front",
        device_id="camera-one-uid",
    )
    assert entity.current_option is None
    assert entity._attr_assumed_state is True
    assert entity.extra_state_attributes["readback_supported"] is False
    assert entity.unique_id == "camera_one_night_vision"


@pytest.mark.asyncio
@pytest.mark.parametrize("key", ("camera_one", "camera_two"))
async def test_entity_confirms_after_cleanup_reasserts_and_clears_failed_command(key):
    started = asyncio.Event()
    released = asyncio.Event()
    calls = []

    async def worker():
        calls.append(True)
        started.set()
        await released.wait()
        if len(calls) == 3:
            raise client.JooanClientError("command failed")

    entity = JooanNightVisionSelect(
        _FakeHass(worker), asyncio.Lock(),
        email="a@example.invalid", password="secret",
        application_id="X" * 20, key=key, name=key, device_id=key,
    )
    entity.async_write_ha_state = lambda: None
    pending = asyncio.create_task(entity.async_select_option(OPTIONS[0]))
    try:
        await started.wait()
        assert entity.current_option is None
        assert entity.extra_state_attributes["last_acknowledged_at"] is None
    finally:
        released.set()
        await pending
    assert entity.current_option == OPTIONS[0]
    assert entity.extra_state_attributes["last_acknowledged_at"] is not None
    await entity.async_select_option(OPTIONS[0])
    assert len(calls) == 2
    with pytest.raises(HomeAssistantError):
        await entity.async_select_option(OPTIONS[1])
    assert entity.current_option is None
    assert entity.extra_state_attributes["last_acknowledged_at"] is None
    assert not entity._lock.locked()


@pytest.mark.parametrize("phase", ("connect", "subscribe", "puback", "reply"))
def test_transaction_timeout_cleans_mqtt_and_cloud(monkeypatch, phase):
    _patch_transport(monkeypatch)
    for timeout in (
        "_CONNECT_TIMEOUT", "_SUBSCRIBE_TIMEOUT", "_PUBACK_TIMEOUT", "_REPLY_TIMEOUT"
    ):
        monkeypatch.setattr(client, timeout, 0)
    if phase == "connect":
        monkeypatch.setattr(
            FakeMqtt, "loop_start", lambda self: client.mqtt.MQTT_ERR_SUCCESS
        )
    elif phase == "subscribe":
        monkeypatch.setattr(
            FakeMqtt, "subscribe",
            lambda self, topic, qos: (client.mqtt.MQTT_ERR_SUCCESS, 8),
        )
    elif phase == "puback":
        monkeypatch.setattr(FakeMqtt, "publish", lambda *args, **kwargs: FakeInfo())
    else:
        def without_reply(self, *args, **kwargs):
            self.on_publish(self, None, 44, ReasonCode(PacketTypes.PUBACK, identifier=0))
            return FakeInfo()
        monkeypatch.setattr(FakeMqtt, "publish", without_reply)
    with pytest.raises(client.JooanClientError):
        client.JooanClient(
            email="a@example.invalid", password="secret",
            application_id="X" * 20, device_id="camera-one-uid",
        ).set_mode(2)
    assert FakeMqtt.instance.disconnected and FakeMqtt.instance.stopped
    assert FakeSession.instances[-1].requests[-1][0].endswith("pcLogout")
    assert FakeSession.instances[-1].closed


@pytest.mark.parametrize("failure", ("logout", "session_close", "oversize", "unowned", "duplicate"))
def test_cloud_failures_never_report_success_and_close_session(monkeypatch, failure):
    _patch_transport(monkeypatch)
    calls = []

    def response_factory(session, url, _kwargs):
        calls.append(url.rsplit("/", 1)[-1])
        if url.endswith("pcLogout"):
            return FakeResponse({"error_code": "1" if failure == "logout" else "0"})
        payload = session.responses.pop(0)
        if url.endswith("query_device_list"):
            if failure == "unowned":
                payload["device_info_list"] = []
            elif failure == "duplicate":
                payload["device_info_list"] *= 2
            elif failure == "oversize":
                payload["oversized"] = "x" * (client._MAX_JSON + 1)
        return FakeResponse(payload)

    monkeypatch.setattr(FakeSession, "response_factory", response_factory)
    if failure == "session_close":
        def close(session):
            session.closed = True
            raise OSError("private transport diagnostic")
        monkeypatch.setattr(FakeSession, "close", close)
    with pytest.raises(client.JooanClientError) as error:
        client.JooanClient(
            email="a@example.invalid", password="secret",
            application_id="X" * 20, device_id="camera-one-uid",
        ).set_mode(2)
    assert "private transport diagnostic" not in str(error.value)
    assert calls[0] == "pcLogin" and calls[-1] == "pcLogout"
    assert FakeSession.instances[-1].closed
    if failure in ("oversize", "unowned", "duplicate"):
        assert FakeMqtt.instance is None


@pytest.mark.parametrize("uid", ("bad\x80uid", "bad\ud800uid", "bad uid"))
def test_non_ascii_or_whitespace_uid_is_rejected(uid):
    with pytest.raises(ValueError):
        client.validate_uid(uid)


@pytest.mark.parametrize("method", ("loop_start", "disconnect", "loop_stop"))
@pytest.mark.parametrize("invalid", (None, False))
def test_mqtt_lifecycle_requires_real_return_codes(monkeypatch, method, invalid):
    _patch_transport(monkeypatch)
    monkeypatch.setattr(FakeMqtt, method, lambda self: invalid)
    with pytest.raises(client.JooanClientError):
        client.JooanClient(
            email="a@example.invalid", password="secret",
            application_id="X" * 20, device_id="camera-one-uid",
        ).set_mode(2)
    assert FakeSession.instances[-1].closed
    assert FakeSession.instances[-1].requests[-1][0].endswith("pcLogout")


def test_cleanup_accepts_already_disconnected_and_stopped_client(monkeypatch):
    _patch_transport(monkeypatch)
    monkeypatch.setattr(
        FakeMqtt, "disconnect", lambda self: client.mqtt.MQTT_ERR_NO_CONN
    )
    monkeypatch.setattr(
        FakeMqtt, "loop_stop", lambda self: client.mqtt.MQTT_ERR_INVAL
    )
    client.JooanClient(
        email="a@example.invalid", password="secret",
        application_id="X" * 20, device_id="camera-one-uid",
    ).set_mode(2)
    assert FakeSession.instances[-1].closed
