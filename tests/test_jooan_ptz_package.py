from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml
from homeassistant.core import HomeAssistant, State
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import mock_restore_cache

import custom_components.jooan_night_vision.ptz_services as ptz_services
from custom_components.jooan_night_vision import CONFIG_SCHEMA, DOMAIN
from custom_components.jooan_night_vision import async_setup as async_setup_integration
from custom_components.jooan_night_vision.ptz_services import validate_ptz_config
from custom_components.jooan_night_vision.select import PLATFORM_SCHEMA


def _readme_yaml_blocks() -> list[dict]:
    text = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    blocks = re.findall(r"```yaml\n(.*?)\n```", text, re.DOTALL)

    class Loader(yaml.SafeLoader):
        pass

    Loader.add_constructor("!secret", lambda loader, node: {
        "jooan_account_email": "account@example.invalid",
        "jooan_account_password": "synthetic-password",
        "jooan_application_id": "SYNTHETIC_APP_ID0001",
        "jooan_camera_one_device_id": "synthetic-device-id",
        "jooan_camera_ptz_host": "192.168.50.10",
        "jooan_camera_ptz_username": "synthetic-user",
        "jooan_camera_ptz_password": "synthetic-password",
        "jooan_camera_ptz_path": "/onvif/example/ptz",
        "jooan_camera_ptz_profile": "synthetic-profile-token",
    }[loader.construct_scalar(node)])
    return [yaml.load(block, Loader=Loader) for block in blocks]


def test_domain_schema_defaults_to_no_ptz_commands():
    assert CONFIG_SCHEMA({}) == {DOMAIN: {"ptz_cameras": []}}


@pytest.mark.asyncio
async def test_setup_without_ptz_does_not_register_services(hass: HomeAssistant):
    await async_setup_integration(hass, {DOMAIN: {}})
    assert not hass.services.has_service(DOMAIN, "nudge")
    assert not hass.services.has_service(DOMAIN, "stop")


def test_readme_yaml_examples_validate_as_public_configuration():
    select_config, _secrets, ptz_config, helpers, actions = _readme_yaml_blocks()
    PLATFORM_SCHEMA(select_config["select"][0])
    validated = CONFIG_SCHEMA(ptz_config)
    assert validate_ptz_config(validated[DOMAIN]["ptz_cameras"])
    assert "initial" not in helpers["input_boolean"]["example_ptz_enable"]

    nudge = actions["action"][0]
    stop = actions["action"][1]
    ptz_services._NUDGE_SCHEMA(
        {**nudge["target"], **nudge["data"]}
    )
    ptz_services._STOP_SCHEMA(stop["target"])


@pytest.mark.asyncio
async def test_readme_interlock_is_disabled_by_default_and_fails_closed(
    hass: HomeAssistant,
):
    _select_config, _secrets, _ptz_config, helpers, _actions = _readme_yaml_blocks()
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": helpers["input_boolean"]}
    )
    assert await async_setup_component(hass, "template", helpers)
    await hass.async_block_till_done()

    interlock = hass.states.get("binary_sensor.example_ptz_interlock")
    assert interlock is not None
    assert interlock.state == "on"

    await hass.services.async_call(
        "input_boolean",
        "turn_on",
        {"entity_id": "input_boolean.example_ptz_enable"},
        blocking=True,
    )
    assert hass.states.get("binary_sensor.example_ptz_interlock").state == "off"

    hass.states.async_remove("input_boolean.example_ptz_enable")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.example_ptz_interlock").state == "on"

    hass.states.async_set("input_boolean.example_ptz_enable", "on")
    await hass.async_block_till_done()
    assert hass.states.get("binary_sensor.example_ptz_interlock").state == "off"
    for state in ("unknown", "unavailable"):
        hass.states.async_set("input_boolean.example_ptz_enable", state)
        await hass.async_block_till_done()
        assert hass.states.get("binary_sensor.example_ptz_interlock").state == "on"


@pytest.mark.asyncio
@pytest.mark.parametrize("restored", ["on", "off"])
async def test_readme_interlock_preserves_restored_enable_choice(hass, restored):
    _select, _secrets, _ptz, helpers, _actions = _readme_yaml_blocks()
    mock_restore_cache(hass, [State("input_boolean.example_ptz_enable", restored)])
    assert await async_setup_component(
        hass, "input_boolean", {"input_boolean": helpers["input_boolean"]}
    )
    assert await async_setup_component(hass, "template", helpers)
    await hass.async_block_till_done()
    assert hass.states.get("input_boolean.example_ptz_enable").state == restored
    assert hass.states.get("binary_sensor.example_ptz_interlock").state == (
        "off" if restored == "on" else "on"
    )
