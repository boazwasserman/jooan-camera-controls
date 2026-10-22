"""Bounded local ONVIF PTZ transport for qualified JOOAN cameras."""

from __future__ import annotations

import base64
import hashlib
import http.client
import io
import ipaddress
import math
import re
import secrets
import socket
import threading
import time
from datetime import datetime, timezone
from typing import Callable
from xml.etree import ElementTree

from defusedxml import ElementTree as SafeElementTree
from defusedxml.common import DefusedXmlException

SOAP_ENV = "http://www.w3.org/2003/05/soap-envelope"
WSSE = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-secext-1.0.xsd"
)
WSU = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-wssecurity-utility-1.0.xsd"
)
PTZ = "http://www.onvif.org/ver20/ptz/wsdl"
TT = "http://www.onvif.org/ver10/schema"
PASSWORD_DIGEST = (
    "http://docs.oasis-open.org/wss/2004/01/"
    "oasis-200401-wss-username-token-profile-1.0#PasswordDigest"
)
PRIVATE_NETWORKS = tuple(
    ipaddress.IPv4Network(network)
    for network in ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
)
MAX_RESPONSE_BYTES = 262144
MAX_TIMEOUT = 0.5
VELOCITY = 0.1


class PtzError(Exception):
    """Base error for bounded PTZ operations."""


class PtzStopUnconfirmed(PtzError):
    """A safety stop could not be confirmed."""


class PtzDeadlineExceeded(PtzError):
    """The caller's original execution deadline expired."""


class PtzCancelled(PtzError):
    """The caller canceled the operation."""


class _DeadlineRaw(io.RawIOBase):
    """Socket reader whose every receive is bounded by one absolute deadline."""

    def __init__(
        self,
        sock: socket.socket,
        deadline: float,
        monotonic: Callable[[], float],
        counter: list[int],
    ):
        self._sock = sock
        self._deadline = deadline
        self._monotonic = monotonic
        self._counter = counter

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        remaining = self._deadline - self._monotonic()
        if remaining <= 0:
            raise socket.timeout("PTZ response deadline expired")
        self._sock.settimeout(remaining)
        try:
            received = self._sock.recv_into(buffer)
            self._counter[0] += received
            if self._counter[0] > MAX_RESPONSE_BYTES:
                raise PtzError("ONVIF response exceeded its bound")
            return received
        except socket.timeout:
            raise
        except OSError:
            raise


class _DeadlineSocket:
    """Minimal public socket adapter accepted by http.client.HTTPResponse."""

    def __init__(
        self,
        sock: socket.socket,
        deadline: float,
        monotonic: Callable[[], float],
        counter: list[int],
    ):
        self._sock = sock
        self._deadline = deadline
        self._monotonic = monotonic
        self._counter = counter

    def makefile(self, mode="rb", buffering=None):
        if "r" not in mode:
            raise ValueError("read-only PTZ response")
        return io.BufferedReader(
            _DeadlineRaw(self._sock, self._deadline, self._monotonic, self._counter)
        )

    def close(self) -> None:
        self._sock.close()


class _OwnedResponse:
    """HTTP response with explicit ownership of its underlying socket."""

    def __init__(
        self, response: http.client.HTTPResponse, sock: socket.socket
    ) -> None:
        self._response = response
        self._sock = sock
        self._closed = False

    def read(self, *args, **kwargs):
        return self._response.read(*args, **kwargs)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        cleanup_error: OSError | None = None
        try:
            self._response.close()
        except OSError as err:
            cleanup_error = err
        try:
            self._sock.close()
        except OSError as err:
            if cleanup_error is None:
                cleanup_error = err
        if cleanup_error is not None:
            raise PtzError("ONVIF response cleanup failed") from None


class PtzClient:
    """Synchronous ONVIF ContinuousMove client with explicit safety stops."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        ptz_path: str,
        profile_token: str,
        *,
        port: int = 8899,
        timeout: float = MAX_TIMEOUT,
        now: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._validate_endpoint(host, port, ptz_path)
        if (
            not isinstance(profile_token, str)
            or not 1 <= len(profile_token) <= 256
            or any(ord(char) < 32 or ord(char) == 127 for char in profile_token)
        ):
            raise ValueError("invalid profile token")
        if (
            not isinstance(username, str)
            or not username
            or not isinstance(password, str)
            or not password
        ):
            raise ValueError("invalid camera credentials")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError("invalid timeout")
        if not math.isfinite(timeout) or not 0 < timeout <= MAX_TIMEOUT:
            raise ValueError("invalid timeout")
        self._host = host
        self._path = ptz_path
        self._port = port
        self._username = username
        self._password = password
        self._profile_token = profile_token
        self._timeout = float(timeout)
        self._now = now
        self._monotonic = monotonic
        self._sleep = sleep

    @staticmethod
    def _validate_endpoint(host: str, port: int, path: str) -> None:
        if not isinstance(host, str):
            raise ValueError("host must be an IPv4 address")
        try:
            address = ipaddress.ip_address(host)
        except (TypeError, ValueError):
            raise ValueError("host must be an IPv4 address") from None
        if address.version != 4 or not any(
            address in network for network in PRIVATE_NETWORKS
        ):
            raise ValueError("host must be an RFC1918 IPv4 address")
        if isinstance(port, bool) or not isinstance(port, int) or port != 8899:
            raise ValueError("invalid ONVIF port")
        if not isinstance(path, str) or not re.fullmatch(
            r"/onvif/(?:[A-Za-z0-9_-]+/)*[A-Za-z0-9_-]+", path
        ):
            raise ValueError("invalid ONVIF PTZ path")

    def nudge(
        self,
        direction: str,
        duration_seconds: float,
        *,
        deadline_monotonic: float,
        cancellation: threading.Event | None = None,
        initial_stop: bool = False,
    ) -> None:
        """Move briefly in one direction and always confirm a safety stop."""
        if direction not in {"left", "right", "up", "down"}:
            raise ValueError("invalid direction")
        if (
            isinstance(duration_seconds, bool)
            or not isinstance(duration_seconds, (int, float))
            or not math.isfinite(duration_seconds)
            or not 0.1 <= duration_seconds <= 0.25
        ):
            raise ValueError("invalid duration")
        if isinstance(deadline_monotonic, bool) or not isinstance(
            deadline_monotonic, (int, float)
        ) or not math.isfinite(deadline_monotonic):
            raise ValueError("invalid deadline")
        self._check_authorization(deadline_monotonic, cancellation)

        if initial_stop:
            try:
                self._baseline_stop(deadline_monotonic, cancellation)
            except (PtzCancelled, PtzDeadlineExceeded):
                raise
            except PtzError:
                raise PtzStopUnconfirmed("PTZ stop was not confirmed") from None
            self._check_authorization(deadline_monotonic, cancellation)

        attempted = False
        motion_started: float | None = None

        def authorize_move() -> None:
            nonlocal attempted, motion_started
            self._check_authorization(deadline_monotonic, cancellation)
            attempted = True
            motion_started = self._monotonic()

        try:
            self._post(
                self._continuous_move_xml(direction),
                "ContinuousMove",
                before_dispatch=authorize_move,
            )
            self._hold(
                motion_started if motion_started is not None else self._monotonic(),
                duration_seconds,
                deadline_monotonic,
                cancellation,
            )
        finally:
            if attempted:
                try:
                    self.stop()
                except PtzError:
                    raise PtzStopUnconfirmed("PTZ stop was not confirmed") from None

    def stop(self) -> None:
        """Send an explicit PanTilt and Zoom stop."""
        self._post(self._stop_xml(), "Stop")

    def _baseline_stop(
        self,
        deadline_monotonic: float,
        cancellation: threading.Event | None,
    ) -> None:
        self._check_authorization(deadline_monotonic, cancellation)
        self._post(
            self._stop_xml(),
            "Stop",
            before_dispatch=lambda: self._check_authorization(
                deadline_monotonic, cancellation
            ),
        )

    def _check_authorization(
        self, deadline_monotonic: float, cancellation: threading.Event | None
    ) -> None:
        if cancellation is not None and cancellation.is_set():
            raise PtzCancelled("PTZ operation canceled")
        if self._monotonic() >= deadline_monotonic:
            raise PtzDeadlineExceeded("PTZ operation deadline expired")

    def _hold(
        self,
        motion_started: float,
        duration: float,
        deadline_monotonic: float,
        cancellation: threading.Event | None,
    ) -> None:
        end = motion_started + duration
        while True:
            self._check_authorization(deadline_monotonic, cancellation)
            remaining = end - self._monotonic()
            if remaining <= 0:
                return
            self._sleep(min(remaining, 0.01))

    def _continuous_move_xml(self, direction: str) -> bytes:
        x = -VELOCITY if direction == "left" else VELOCITY if direction == "right" else 0.0
        y = VELOCITY if direction == "up" else -VELOCITY if direction == "down" else 0.0
        envelope, body = self._envelope()
        operation = ElementTree.SubElement(body, f"{{{PTZ}}}ContinuousMove")
        ElementTree.SubElement(operation, f"{{{PTZ}}}ProfileToken").text = self._profile_token
        velocity = ElementTree.SubElement(operation, f"{{{PTZ}}}Velocity")
        ElementTree.SubElement(
            velocity, f"{{{TT}}}PanTilt", {"x": str(x), "y": str(y)}
        )
        ElementTree.SubElement(operation, f"{{{PTZ}}}Timeout").text = "PT3.6S"
        return ElementTree.tostring(envelope, encoding="utf-8", xml_declaration=True)

    def _stop_xml(self) -> bytes:
        envelope, body = self._envelope()
        operation = ElementTree.SubElement(body, f"{{{PTZ}}}Stop")
        ElementTree.SubElement(operation, f"{{{PTZ}}}ProfileToken").text = self._profile_token
        ElementTree.SubElement(operation, f"{{{PTZ}}}PanTilt").text = "true"
        ElementTree.SubElement(operation, f"{{{PTZ}}}Zoom").text = "true"
        return ElementTree.tostring(envelope, encoding="utf-8", xml_declaration=True)

    def _envelope(self) -> tuple[ElementTree.Element, ElementTree.Element]:
        envelope = ElementTree.Element(f"{{{SOAP_ENV}}}Envelope")
        header = ElementTree.SubElement(envelope, f"{{{SOAP_ENV}}}Header")
        security = ElementTree.SubElement(header, f"{{{WSSE}}}Security")
        security.set(f"{{{SOAP_ENV}}}mustUnderstand", "1")
        token = ElementTree.SubElement(security, f"{{{WSSE}}}UsernameToken")
        ElementTree.SubElement(token, f"{{{WSSE}}}Username").text = self._username
        nonce = secrets.token_bytes(20)
        created = datetime.fromtimestamp(self._now(), timezone.utc).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        digest = base64.b64encode(
            hashlib.sha1(nonce + created.encode() + self._password.encode()).digest()
        ).decode()
        password = ElementTree.SubElement(token, f"{{{WSSE}}}Password")
        password.set("Type", PASSWORD_DIGEST)
        password.text = digest
        nonce_node = ElementTree.SubElement(token, f"{{{WSSE}}}Nonce")
        nonce_node.set(
            "EncodingType",
            "http://docs.oasis-open.org/wss/2004/01/"
            "oasis-200401-wss-soap-message-security-1.0#Base64Binary",
        )
        nonce_node.text = base64.b64encode(nonce).decode()
        ElementTree.SubElement(token, f"{{{WSU}}}Created").text = created
        return envelope, ElementTree.SubElement(envelope, f"{{{SOAP_ENV}}}Body")

    def _post(
        self,
        payload: bytes,
        operation: str,
        *,
        before_dispatch: Callable[[], None] | None = None,
    ) -> None:
        response = None
        try:
            response_deadline = self._monotonic() + self._timeout
            response = self._post_socket(
                payload, operation, response_deadline, before_dispatch
            )
            content = bytearray()
            while True:
                if self._monotonic() >= response_deadline:
                    raise PtzError("ONVIF response timed out")
                chunk = response.read(16384)
                if not chunk:
                    break
                content.extend(chunk)
                if len(content) > MAX_RESPONSE_BYTES:
                    raise PtzError("ONVIF response exceeded its bound")
            try:
                root = SafeElementTree.fromstring(
                    content, forbid_dtd=True, forbid_entities=True
                )
            except (ElementTree.ParseError, DefusedXmlException):
                raise PtzError("invalid ONVIF response") from None
            if root.tag != f"{{{SOAP_ENV}}}Envelope":
                raise PtzError("invalid ONVIF response")
            body = root.find(f"{{{SOAP_ENV}}}Body")
            if body is None:
                raise PtzError("invalid ONVIF response")
            if body.find(f"{{{SOAP_ENV}}}Fault") is not None:
                raise PtzError("ONVIF request faulted")
            if body.find(f"{{{PTZ}}}{operation}Response") is None:
                raise PtzError("invalid ONVIF response")
        except (http.client.HTTPException, OSError):
            raise PtzError("ONVIF request failed") from None
        finally:
            if response is not None:
                response.close()

    def _post_socket(
        self,
        payload: bytes,
        operation: str,
        deadline: float,
        before_dispatch: Callable[[], None] | None,
    ):
        remaining = deadline - self._monotonic()
        if remaining <= 0:
            raise PtzError("ONVIF response timed out")
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        owned_response = None
        try:
            sock.settimeout(remaining)
            sock.connect((self._host, self._port))
            request = (
                f"POST {self._path} HTTP/1.1\r\n"
                f"Host: {self._host}:{self._port}\r\n"
                "Connection: close\r\n"
                "Content-Type: "
                f'application/soap+xml; charset=utf-8; action="{PTZ}/{operation}"\r\n'
                f"Content-Length: {len(payload)}\r\n\r\n"
            ).encode() + payload
            if before_dispatch is not None:
                before_dispatch()
            view = memoryview(request)
            while view:
                remaining = deadline - self._monotonic()
                if remaining <= 0:
                    raise socket.timeout("PTZ response deadline expired")
                sock.settimeout(remaining)
                sent = sock.send(view)
                if not sent:
                    raise OSError("PTZ request send failed")
                view = view[sent:]
            response = http.client.HTTPResponse(
                _DeadlineSocket(sock, deadline, self._monotonic, [0])
            )
            owned_response = _OwnedResponse(response, sock)
            response.begin()
            if response.status != 200:
                owned_response.close()
                raise PtzError("ONVIF request failed")
            return owned_response
        except Exception:
            if owned_response is not None:
                owned_response.close()
            else:
                sock.close()
            raise
