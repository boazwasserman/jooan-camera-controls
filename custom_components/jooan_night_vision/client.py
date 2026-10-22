"""Bounded synchronous JOOAN cloud and MQTT client."""

from __future__ import annotations

import ipaddress
import json
import secrets
import ssl
import threading
import time
from collections.abc import Mapping
from urllib.parse import urlsplit

import paho.mqtt.client as mqtt
import requests

BASE_URL = "https://use1api.jooaniot.com"
PACKAGE_NAME = "CAM720.vmsToolsApp"
COMMAND = 66435
_HOST_SUFFIXES = (".jooaniot.com", ".qalink.cn")
_EXACT_HOST = "huiyancommserver.lenovo.com.cn"
_MAX_JSON = 1024 * 1024
_MAX_REPLY = 8192
_CONNECT_TIMEOUT = 8
_SUBSCRIBE_TIMEOUT = 10
_PUBACK_TIMEOUT = 10
_REPLY_TIMEOUT = 10


class JooanClientError(Exception):
    """A sanitized error safe to expose in Home Assistant."""


class _ResponseCleanupError(JooanClientError):
    """A parsed response whose stream could not be closed."""

    def __init__(self, payload: dict) -> None:
        super().__init__("The JOOAN response cleanup failed")
        self.payload = payload
        self.session_values: dict[str, str] | None = None


def _success(value: object) -> bool:
    return (type(value) is int and value == 0) or (
        type(value) is str and value == "0"
    )


def _actual_int(value: object) -> bool:
    return type(value) is int


def _reason_value(value: object) -> int | None:
    """Normalize Paho ints and ReasonCode.value without accepting bool/string."""
    value = getattr(value, "value", value)
    return value if type(value) is int else None


def _nested_list(payload: Mapping[str, object], key: str) -> list | None:
    pending = [payload]
    while pending:
        current = pending.pop()
        if not isinstance(current, dict):
            continue
        if isinstance(current.get(key), list):
            return current[key]
        for wrapper in ("data", "result", "payload"):
            child = current.get(wrapper)
            if isinstance(child, dict):
                pending.append(child)
    return None


def validate_uid(value: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= 128:
        raise ValueError("device_id must be 1..128 characters")
    if any(not 0x21 <= ord(char) <= 0x7E for char in value):
        raise ValueError("device_id must contain non-whitespace printable ASCII")
    if any(char in value for char in "+#/\\"):
        raise ValueError("device_id contains an MQTT topic separator")
    return value


def validate_broker_url(value: str) -> tuple[str, int]:
    if not isinstance(value, str) or "://" in value:
        raise JooanClientError("The broker address is invalid")
    if any(ord(char) < 0x20 or ord(char) == 0x7F or char.isspace() for char in value):
        raise JooanClientError("The broker address is invalid")
    try:
        parsed = urlsplit(f"//{value}")
    except ValueError:
        raise JooanClientError("The broker address is invalid") from None
    if (
        parsed.username
        or parsed.password
        or parsed.path not in ("",)
        or parsed.query
        or parsed.fragment
        or not parsed.hostname
    ):
        raise JooanClientError("The broker address is invalid")
    try:
        port = parsed.port
    except ValueError:
        raise JooanClientError("The broker address is invalid") from None
    if port != 443:
        raise JooanClientError("The broker address is invalid")
    host = parsed.hostname
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if host != host.lower() or not (
            host == _EXACT_HOST or host.endswith(_HOST_SUFFIXES)
        ):
            raise JooanClientError("The broker address is not an approved vendor host")
    else:
        raise JooanClientError("The broker address is not an approved vendor host")
    return host, port


class JooanClient:
    """Perform one authenticated, one-publish mode command."""

    def __init__(
        self, *, email: str, password: str, application_id: str, device_id: str
    ) -> None:
        self.email = email
        self.password = password
        self.application_id = application_id
        self.device_id = validate_uid(device_id)

    def set_mode(self, mode: int) -> None:
        if not _actual_int(mode) or mode not in (0, 1, 2):
            raise JooanClientError("The night-vision mode is invalid")
        session = requests.Session()
        session.trust_env = False
        session_values: dict[str, str] | None = None
        failure: JooanClientError | None = None
        try:
            session_values = self._login(session)
            self._verify_owned_device(session, session_values)
            broker = self._discover_broker(session, session_values)
            self._mqtt_command(broker, session_values, mode)
        except _ResponseCleanupError as err:
            if err.session_values is not None:
                session_values = err.session_values
            failure = err
        except JooanClientError as err:
            failure = err
        except (
            OSError,
            TypeError,
            ValueError,
            AttributeError,
            RuntimeError,
            requests.RequestException,
        ):
            failure = JooanClientError("The JOOAN night-vision command failed")
        finally:
            cleanup_failure: Exception | None = None
            if session_values is not None:
                try:
                    self._logout(session, session_values)
                except (
                    OSError,
                    TypeError,
                    ValueError,
                    AttributeError,
                    RuntimeError,
                    requests.RequestException,
                    JooanClientError,
                ):
                    cleanup_failure = JooanClientError("JOOAN session cleanup failed")
            try:
                session.close()
            except (OSError, RuntimeError, AttributeError):
                cleanup_failure = JooanClientError("JOOAN session cleanup failed")
            if cleanup_failure is not None:
                failure = cleanup_failure
        if failure is not None:
            raise failure

    def _request(
        self, session: requests.Session, path: str, body: Mapping[str, object]
    ) -> dict:
        response = None
        result: dict | None = None
        failure: JooanClientError | None = None
        try:
            response = session.post(
                f"{BASE_URL}{path}",
                json=body,
                headers={"Content-Type": "application/json"},
                timeout=(12, 12),
                allow_redirects=False,
                stream=True,
                verify=True,
            )
            if not 200 <= response.status_code < 300:
                raise JooanClientError("The JOOAN cloud request failed")
            content = bytearray()
            for chunk in response.iter_content(chunk_size=8192):
                content.extend(chunk)
                if len(content) > _MAX_JSON:
                    raise JooanClientError("The JOOAN response was too large")
            payload = json.loads(bytes(content))
            if not isinstance(payload, dict):
                raise JooanClientError("The JOOAN response was invalid")
            result = payload
        except JooanClientError as err:
            failure = err
        except (OSError, TypeError, ValueError, requests.RequestException):
            failure = JooanClientError("The JOOAN cloud request failed")
        finally:
            if response is not None:
                try:
                    response.close()
                except (
                    OSError,
                    TypeError,
                    ValueError,
                    AttributeError,
                    RuntimeError,
                    requests.RequestException,
                ):
                    if result is not None:
                        failure = _ResponseCleanupError(result)
                    else:
                        failure = JooanClientError(
                            "The JOOAN response cleanup failed"
                        )
        if failure is not None:
            raise failure
        assert result is not None
        return result

    def _header(
        self, values: Mapping[str, str], *, device_list: list[str] | None = None
    ) -> dict[str, object]:
        header: dict[str, object] = {
            "seqno": int(time.time() * 1000),
            "package_name": PACKAGE_NAME,
            "user_id": values["user_id"],
            "token": values["token"],
        }
        if device_list is not None:
            header["device_list"] = device_list
        return header

    def _login(self, session: requests.Session) -> dict[str, str]:
        try:
            payload = self._request(
                session,
                "/api/vms/user/v3/pcLogin",
                {
                    "header": {
                        "seqno": int(time.time() * 1000),
                        "package_name": PACKAGE_NAME,
                    },
                    "login_mode": 1,
                    "email": self.email,
                    "password": self.password,
                    "client_id": self.application_id,
                    "pcClientId": "",
                    "package_name": PACKAGE_NAME,
                },
            )
        except _ResponseCleanupError as err:
            values = self._parse_login(err.payload)
            err.session_values = values
            raise
        return self._parse_login(payload)

    @staticmethod
    def _parse_login(payload: dict) -> dict[str, str]:
        if not _success(payload.get("error_code")):
            raise JooanClientError("JOOAN login failed")
        containers = [
            value for key, value in payload.items() if key in ("body", "data")
        ]
        valid = [
            value
            for value in containers
            if isinstance(value, dict)
            and isinstance(value.get("user_id"), str)
            and bool(value.get("user_id"))
            and isinstance(value.get("token"), str)
            and bool(value.get("token"))
        ]
        if len(valid) != 1:
            raise JooanClientError("JOOAN login response was invalid")
        data = valid[0]
        user_id, token = data.get("user_id"), data.get("token")
        if not isinstance(user_id, str) or not user_id:
            raise JooanClientError("JOOAN login response was invalid")
        if not isinstance(token, str) or not token:
            raise JooanClientError("JOOAN login response was invalid")
        return {"user_id": user_id, "token": token}

    def _verify_owned_device(
        self, session: requests.Session, values: Mapping[str, str]
    ) -> None:
        payload = self._request(
            session,
            "/api/vms/device/v3/query_device_list",
            {
                "header": {
                    "client_version": "1.0.0.0",
                    **self._header(values),
                },
                "force_query_bind": "1",
                "page": {"mode": 1, "size": 0, "from": 0, "shareFrom": 0},
            },
        )
        if not _success(payload.get("error_code")):
            raise JooanClientError("JOOAN device lookup failed")
        devices = _nested_list(payload, "device_info_list")
        if devices is None:
            raise JooanClientError("JOOAN device lookup was invalid")
        owned = []
        for item in devices:
            if not isinstance(item, dict):
                continue
            info = item.get("jooan_info")
            props = info.get("properties") if isinstance(info, dict) else None
            if isinstance(props, dict) and props.get("device_id") == self.device_id:
                owned.append(item)
        if len(owned) != 1:
            raise JooanClientError("Configured JOOAN camera was not uniquely owned")

    def _discover_broker(
        self, session: requests.Session, values: Mapping[str, str]
    ) -> tuple[str, int]:
        payload = self._request(
            session,
            "/api/vms/device/v3/query_device_mqttcluster",
            {"header": self._header(values, device_list=[self.device_id])},
        )
        if not _success(payload.get("error_code")):
            raise JooanClientError("JOOAN broker lookup failed")
        addresses = _nested_list(payload, "device_mqtt_address")
        if addresses is None:
            raise JooanClientError("JOOAN broker lookup was invalid")
        matches = [
            item
            for item in addresses
            if isinstance(item, dict) and item.get("device_id") == self.device_id
        ]
        if (
            len(matches) != 1
            or not isinstance(matches[0].get("cluster_id"), str)
            or not matches[0]["cluster_id"].isdigit()
        ):
            raise JooanClientError("JOOAN broker lookup was invalid")
        return validate_broker_url(matches[0].get("url", ""))

    def _logout(self, session: requests.Session, values: Mapping[str, str]) -> None:
        payload = self._request(
            session, "/api/vms/user/v3/pcLogout", {"header": self._header(values)}
        )
        if not _success(payload.get("error_code")):
            raise JooanClientError("JOOAN logout failed")

    def _mqtt_command(
        self, broker: tuple[str, int], values: Mapping[str, str], mode: int
    ) -> None:
        host, port = broker
        mqtt_client = None
        failure: JooanClientError | None = None
        try:
            mqtt_client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                protocol=mqtt.MQTTv311,
                client_id="ha-" + secrets.token_hex(8),
                clean_session=True,
                reconnect_on_failure=False,
            )
            mqtt_client.username_pw_set("pc" + values["user_id"], values["token"])
            mqtt_client.tls_set_context(ssl.create_default_context())
            state = _MqttState(self.device_id, mode)
            mqtt_client.on_connect = state.on_connect
            mqtt_client.on_subscribe = state.on_subscribe
            mqtt_client.on_publish = state.on_publish
            mqtt_client.on_message = state.on_message
            rc = mqtt_client.connect(host, port, keepalive=60)
            if _reason_value(rc) != 0:
                raise JooanClientError("JOOAN MQTT connection failed")
            loop_result = mqtt_client.loop_start()
            if _reason_value(loop_result) != 0:
                raise JooanClientError("JOOAN MQTT loop failed")
            if not state.connected.wait(_CONNECT_TIMEOUT) or state.connection_reason != 0:
                raise JooanClientError("JOOAN MQTT connection failed")
            rc, subscribe_mid = mqtt_client.subscribe(
                f"qaiot/mqtt/user/{self.device_id}", qos=1
            )
            if _reason_value(rc) != 0 or not _actual_int(subscribe_mid):
                raise JooanClientError("JOOAN MQTT subscription failed")
            state.subscription_mid = subscribe_mid
            if not state.wait_for_subscription(_SUBSCRIBE_TIMEOUT):
                raise JooanClientError("JOOAN MQTT subscription failed")
            state.request_phase = True
            info = mqtt_client.publish(
                f"qaiot/mqtt/{self.device_id}",
                json.dumps({"cmd_type": "request", "cmd": COMMAND, "value": mode}),
                qos=1,
                retain=False,
            )
            if _reason_value(getattr(info, "rc", None)) != 0 or not _actual_int(
                getattr(info, "mid", None)
            ):
                raise JooanClientError("JOOAN MQTT publish failed")
            state.publish_mid = info.mid
            if not state.wait_for_puback(_PUBACK_TIMEOUT):
                raise JooanClientError("JOOAN MQTT publish was not acknowledged")
            if not state.wait_for_reply(_REPLY_TIMEOUT):
                raise JooanClientError("JOOAN device did not acknowledge the mode")
        except JooanClientError as err:
            failure = err
        except (
            OSError,
            TypeError,
            ValueError,
            AttributeError,
            RuntimeError,
            mqtt.MQTTException,
        ):
            failure = JooanClientError("JOOAN MQTT command failed")
        finally:
            cleanup_failure: Exception | None = None
            if mqtt_client is not None:
                try:
                    result = mqtt_client.disconnect()
                    if _reason_value(result) not in (
                        0, mqtt.MQTT_ERR_NO_CONN.value
                    ):
                        raise OSError
                except (
                    OSError,
                    TypeError,
                    ValueError,
                    AttributeError,
                    RuntimeError,
                    mqtt.MQTTException,
                ):
                    cleanup_failure = JooanClientError("JOOAN MQTT cleanup failed")
                try:
                    result = mqtt_client.loop_stop()
                    if _reason_value(result) not in (
                        0, mqtt.MQTT_ERR_INVAL.value
                    ):
                        raise OSError
                except (
                    OSError,
                    TypeError,
                    ValueError,
                    AttributeError,
                    RuntimeError,
                    mqtt.MQTTException,
                ):
                    cleanup_failure = JooanClientError("JOOAN MQTT cleanup failed")
            if cleanup_failure is not None:
                failure = cleanup_failure
        if failure is not None:
            raise failure


class _MqttState:
    def __init__(self, device_id: str, mode: int) -> None:
        self.device_id = device_id
        self.mode = mode
        self.connected = threading.Event()
        self.connection_reason: int | None = None
        self.subscription_mid: int | None = None
        self.subscription_results: dict[int, bool] = {}
        self.publish_mid: int | None = None
        self.pubacks: dict[int, int | None] = {}
        self.reply_received = False
        self.request_phase = False
        self._condition = threading.Condition()

    def on_connect(self, _client, _userdata, _flags, reason_code, _properties) -> None:
        with self._condition:
            self.connection_reason = _reason_value(reason_code)
            self.connected.set()
            self._condition.notify_all()

    def on_subscribe(
        self, _client, _userdata, mid, reason_codes, _properties=None
    ) -> None:
        with self._condition:
            values = [_reason_value(code) for code in reason_codes]
            self.subscription_results[mid] = (
                _actual_int(mid) and len(values) == 1 and values[0] == 1
            )
            self._condition.notify_all()

    def on_publish(
        self, _client, _userdata, mid, reason_code=None, _properties=None
    ) -> None:
        with self._condition:
            if _actual_int(mid):
                self.pubacks[mid] = _reason_value(reason_code)
            self._condition.notify_all()

    def on_message(self, _client, _userdata, message) -> None:
        with self._condition:
            if not self.request_phase:
                return
            if message.topic != f"qaiot/mqtt/user/{self.device_id}" or message.retain:
                return
            if len(message.payload) > _MAX_REPLY:
                return
            try:
                payload = json.loads(message.payload)
            except (TypeError, ValueError):
                return
            if not isinstance(payload, dict):
                return
            if (
                payload.get("cmd_type") != "response"
                or not _actual_int(payload.get("cmd"))
                or payload["cmd"] != COMMAND
                or not _actual_int(payload.get("value"))
                or payload["value"] != self.mode
            ):
                return
            if "deviceId" in payload and payload["deviceId"] != self.device_id:
                return
            self.reply_received = True
            self._condition.notify_all()

    def wait_for_subscription(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self.subscription_results.get(self.subscription_mid, False):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True

    def wait_for_puback(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while True:
                if self.publish_mid in self.pubacks:
                    return self.pubacks[self.publish_mid] == 0
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)

    def wait_for_reply(self, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._condition:
            while not self.reply_received:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(remaining)
            return True
