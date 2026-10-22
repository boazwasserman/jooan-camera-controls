from __future__ import annotations

import base64
import hashlib
import socket
import threading
import time
import types
from xml.etree import ElementTree

import pytest
import voluptuous as vol

from custom_components.jooan_night_vision import CONFIG_SCHEMA
from custom_components.jooan_night_vision import ptz as ptz_module
from custom_components.jooan_night_vision.ptz import (
    PASSWORD_DIGEST,
    PTZ,
    SOAP_ENV,
    TT,
    WSSE,
    WSU,
    PtzCancelled,
    PtzClient,
    PtzDeadlineExceeded,
    PtzError,
    PtzStopUnconfirmed,
    _OwnedResponse,
)


def response(operation: str) -> bytes:
    envelope = ElementTree.Element(f"{{{SOAP_ENV}}}Envelope")
    body = ElementTree.SubElement(envelope, f"{{{SOAP_ENV}}}Body")
    ElementTree.SubElement(body, f"{{{PTZ}}}{operation}Response")
    return ElementTree.tostring(envelope)


def client_with_capture():
    client = PtzClient(
        "192.168.50.10",
        "sentinel-user",
        "sentinel-password",
        "/onvif/ptz_service",
        "profile-1",
    )
    requests = []

    def post(payload, operation, *, before_dispatch=None):
        if before_dispatch:
            before_dispatch()
        requests.append((operation, payload))

    client._post = post
    return client, requests


@pytest.mark.parametrize(
    ("direction", "vector"),
    [
        ("left", {"x": "-0.1", "y": "0.0"}),
        ("right", {"x": "0.1", "y": "0.0"}),
        ("up", {"x": "0.0", "y": "0.1"}),
        ("down", {"x": "0.0", "y": "-0.1"}),
    ],
)
def test_continuous_move_xml_and_wsse(direction, vector):
    client, requests = client_with_capture()
    client.nudge(direction, 0.1, deadline_monotonic=time.monotonic() + 2)
    assert [operation for operation, _ in requests] == ["ContinuousMove", "Stop"]
    root = ElementTree.fromstring(requests[0][1])
    move = root.find(f".//{{{PTZ}}}ContinuousMove")
    assert move.find(f"{{{PTZ}}}Velocity/{{{TT}}}PanTilt").attrib == vector
    assert move.find(f"{{{PTZ}}}Timeout").text == "PT3.6S"
    token = root.find(f".//{{{WSSE}}}UsernameToken")
    nonce = base64.b64decode(token.find(f"{{{WSSE}}}Nonce").text)
    password = token.find(f"{{{WSSE}}}Password")
    created = token.find(f"{{{WSU}}}Created").text
    assert password.attrib["Type"] == PASSWORD_DIGEST
    assert base64.b64decode(password.text) == hashlib.sha1(
        nonce + created.encode() + b"sentinel-password"
    ).digest()


@pytest.mark.parametrize("duration", [0, 0.099, 0.251, float("nan"), float("inf"), True, "0.1"])
def test_invalid_duration_sends_no_request(duration):
    client, requests = client_with_capture()
    with pytest.raises(ValueError):
        client.nudge("left", duration, deadline_monotonic=time.monotonic() + 2)
    assert not requests


def test_preexpired_or_cancelled_sends_no_request():
    client, requests = client_with_capture()
    with pytest.raises(PtzDeadlineExceeded):
        client.nudge("left", 0.1, deadline_monotonic=time.monotonic() - 1)
    event = threading.Event()
    event.set()
    with pytest.raises(PtzCancelled):
        client.nudge("left", 0.1, deadline_monotonic=time.monotonic() + 2, cancellation=event)
    assert not requests


def test_initial_stop_late_authorization_sends_zero_requests(monkeypatch):
    client, requests = client_with_capture()
    event = threading.Event()
    original = client._stop_xml

    def prepare():
        payload = original()
        event.set()
        return payload

    monkeypatch.setattr(client, "_stop_xml", prepare)
    with pytest.raises(PtzCancelled):
        client.nudge(
            "left",
            0.1,
            deadline_monotonic=time.monotonic() + 2,
            cancellation=event,
            initial_stop=True,
        )
    assert not requests


def test_uncertain_move_always_attempts_stop_and_failed_stop_is_distinct():
    client, requests = client_with_capture()
    def fail_move(payload, operation, *, before_dispatch=None):
        if before_dispatch:
            before_dispatch()
        requests.append((operation, payload))
        if operation == "ContinuousMove":
            raise PtzError("uncertain")
        raise PtzError("stop")

    client._post = fail_move
    with pytest.raises(PtzStopUnconfirmed):
        client.nudge("left", 0.1, deadline_monotonic=time.monotonic() + 2)
    assert [operation for operation, _ in requests] == ["ContinuousMove", "Stop"]


def _schema_camera(**overrides):
    camera = {
        "entity_id": "camera.example",
        "host": "192.168.50.10",
        "username": "user",
        "password": "password",
        "ptz_path": "/onvif/ptz_service",
        "profile_token": "profile",
        "pulse_seconds_entity": "input_number.example_ptz_pulse_seconds",
    }
    camera.update(overrides)
    return {"jooan_night_vision": {"ptz_cameras": [camera]}}


def test_config_schema_rejects_unknown_camera_keys_but_allows_root_extras():
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(_schema_camera(unexpected="rejected"))
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA({"jooan_night_vision": {"unexpected": True}})
    validated = CONFIG_SCHEMA(
        {
            **_schema_camera(),
            "unrelated_top_level": "preserved",
        }
    )
    assert validated["unrelated_top_level"] == "preserved"


def test_config_schema_and_client_share_profile_token_length_bound():
    token = "x" * 257
    with pytest.raises(vol.Invalid):
        CONFIG_SCHEMA(_schema_camera(profile_token=token))
    with pytest.raises(ValueError):
        PtzClient(
            "192.168.50.10",
            "user",
            "password",
            "/onvif/ptz_service",
            token,
        )


class _TrackingSocket:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class _FakeResponse:
    def __init__(self, fail=False):
        self.closed = False
        self.fail = fail

    def close(self):
        self.closed = True
        if self.fail:
            raise OSError("close failed")


@pytest.mark.parametrize("fail", [False, True])
def test_owned_response_closes_response_and_socket_on_success_or_failure(fail):
    response_object = _FakeResponse(fail)
    socket_object = _TrackingSocket()
    owned = _OwnedResponse(response_object, socket_object)
    if fail:
        with pytest.raises(PtzError):
            owned.close()
    else:
        owned.close()
    assert response_object.closed
    assert socket_object.closed


@pytest.mark.enable_socket
@pytest.mark.parametrize(
    ("wire", "fails"),
    [
        (
            lambda: (
                b"HTTP/1.1 200 OK\r\nContent-Length: "
                + str(len(response("Stop"))).encode()
                + b"\r\nConnection: close\r\n\r\n"
                + response("Stop")
            ),
            False,
        ),
        (lambda: b"HTTP/1.1 500 Server Error\r\nContent-Length: 0\r\n\r\n", True),
        (lambda: b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nbad!!", True),
    ],
)
def test_stop_closes_retained_socket_for_success_bad_status_and_invalid_soap(
    socket_enabled, monkeypatch, wire, fails
):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(2)
    port = server.getsockname()[1]
    server_errors = []

    def serve():
        try:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(2)
                connection.recv(4096)
                connection.sendall(wire())
        except (OSError, socket.timeout) as err:
            server_errors.append(err)
        finally:
            server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    retained = []
    real_socket = socket.socket

    def factory(*args, **kwargs):
        connection = real_socket(*args, **kwargs)
        retained.append(connection)
        return connection

    isolated_socket = types.SimpleNamespace(
        AF_INET=socket.AF_INET,
        SOCK_STREAM=socket.SOCK_STREAM,
        timeout=socket.timeout,
        socket=factory,
    )
    monkeypatch.setattr(ptz_module, "socket", isolated_socket)
    client = PtzClient(
        "192.168.50.10",
        "user",
        "password",
        "/onvif/ptz_service",
        "profile",
    )
    client._host = "127.0.0.1"
    client._port = port
    try:
        if fails:
            with pytest.raises(PtzError):
                client.stop()
        else:
            client.stop()
    finally:
        thread.join(timeout=3)
        assert not thread.is_alive()
    assert not server_errors
    assert retained
    assert retained[0].fileno() == -1


class _Loopback:
    def __init__(self, first_response: bytes, drip: bool):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(2)
        self.server.settimeout(2)
        self.port = self.server.getsockname()[1]
        self.first_response = first_response
        self.drip = drip
        self.connections = []
        self.thread = threading.Thread(target=self._serve)
        self.thread.start()

    def _serve(self):
        try:
            for index in range(2):
                try:
                    connection, _ = self.server.accept()
                except socket.timeout:
                    return
                self.connections.append(connection)
                with connection:
                    connection.settimeout(2)
                    if index == 0:
                        try:
                            if self.drip:
                                for byte in self.first_response:
                                    connection.send(bytes((byte,)))
                                    time.sleep(0.01)
                            else:
                                connection.sendall(self.first_response)
                        except (OSError, socket.timeout):
                            pass
                    else:
                        try:
                            connection.recv(4096)
                            body = response("Stop")
                            connection.sendall(
                                b"HTTP/1.1 200 OK\r\nContent-Length: "
                                + str(len(body)).encode()
                                + b"\r\nConnection: close\r\n\r\n"
                                + body
                            )
                        except (OSError, socket.timeout):
                            pass
        finally:
            self.server.close()


def _real_client(port):
    client = PtzClient(
        "192.168.50.10", "user", "password", "/onvif/ptz_service", "profile"
    )
    client._host = "127.0.0.1"
    client._port = port
    return client


@pytest.mark.enable_socket
def test_real_loopback_status_header_drip_is_bounded_and_stop_succeeds(socket_enabled):
    wire = b"HTTP/1.1 200 OK\r\nX-Slow: " + (b"x" * 40) + b"\r\nContent-Length: 0\r\n\r\n"
    loopback = _Loopback(wire, drip=True)
    started = time.monotonic()
    try:
        with pytest.raises(PtzError) as error:
            _real_client(loopback.port).nudge(
                "left", 0.1, deadline_monotonic=time.monotonic() + 3
            )
    finally:
        loopback.thread.join(timeout=3)
        assert not loopback.thread.is_alive()
    assert not isinstance(error.value, PtzStopUnconfirmed)
    assert time.monotonic() - started < 1.5
    assert len(loopback.connections) == 2


@pytest.mark.enable_socket
def test_real_loopback_body_drip_is_bounded_and_stop_succeeds(socket_enabled):
    body = response("ContinuousMove")
    wire = (
        b"HTTP/1.1 200 OK\r\nContent-Length: "
        + str(len(body)).encode()
        + b"\r\nConnection: close\r\n\r\n"
        + body
    )
    loopback = _Loopback(wire, drip=True)
    try:
        with pytest.raises(PtzError) as error:
            _real_client(loopback.port).nudge(
                "left", 0.1, deadline_monotonic=time.monotonic() + 3
            )
    finally:
        loopback.thread.join(timeout=3)
        assert not loopback.thread.is_alive()
    assert not isinstance(error.value, PtzStopUnconfirmed)
    assert len(loopback.connections) == 2


@pytest.mark.enable_socket
@pytest.mark.parametrize("failure", [PtzCancelled, PtzDeadlineExceeded])
def test_post_connect_authorization_failure_sends_zero_post_bytes(socket_enabled, failure):
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    server.settimeout(2)
    port = server.getsockname()[1]
    received = []

    def serve():
        try:
            connection, _ = server.accept()
            with connection:
                connection.settimeout(2)
                try:
                    received.append(connection.recv(4096))
                except socket.timeout:
                    received.append(b"")
        except (OSError, socket.timeout):
            received.append(b"")
        finally:
            server.close()

    thread = threading.Thread(target=serve)
    thread.start()
    client = _real_client(port)
    checks = 0
    original = client._check_authorization

    def fail_after_connect(deadline, cancellation):
        nonlocal checks
        checks += 1
        if checks == 2:
            raise failure("authorization changed")
        return original(deadline, cancellation)

    client._check_authorization = fail_after_connect
    with pytest.raises(failure):
        client.nudge("left", 0.1, deadline_monotonic=time.monotonic() + 2)
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert received == [b""]
