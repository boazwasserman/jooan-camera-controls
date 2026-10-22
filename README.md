# JOOAN Camera Controls

An unofficial Home Assistant custom integration for selected JOOAN cameras.
This initial standalone distribution keeps the existing `jooan_night_vision`
domain and offers:

- YAML-configured cloud night-vision mode commands.
- Optional, explicitly configured local ONVIF PTZ nudges and Stop recovery.

Frigate is not required. Video, recording, detection, and tracking remain the
responsibility of the camera/stream integrations already installed in
Home Assistant.

## Installation

Install the repository with HACS as a custom repository of type **Integration**,
or copy `custom_components/jooan_night_vision` into the
`custom_components` directory. Restart Home Assistant after installation.

This is an unofficial community package and is not affiliated with or endorsed
by JOOAN. No configuration flow is included in this initial YAML-based release.

The manifest uses Home Assistant's required minimum-version declarations for
`defusedxml` and `paho-mqtt`; Hassfest rejects exact pins for these shared
packages. Home Assistant manages dependency resolution and updates, so the
manifest is not a standalone dependency lockfile. Validate Home Assistant and
dependency upgrades before relying on camera control.

## Cloud mode configuration

Add a select platform entry to `configuration.yaml`. Keep credentials in
`secrets.yaml`; the values below are deliberately fake placeholders:

```yaml
select:
  - platform: jooan_night_vision
    email: !secret jooan_account_email
    password: !secret jooan_account_password
    application_id: !secret jooan_application_id
    cameras:
      - key: "camera_one"
        name: "Camera One night vision"
        device_id: !secret jooan_camera_one_device_id
```

Example `secrets.yaml` entries:

```yaml
jooan_account_email: "account@example.invalid"
jooan_account_password: "NOT_A_REAL_PASSWORD"
jooan_application_id: "SYNTHETIC_APP_ID0001"
jooan_camera_one_device_id: "synthetic-device-id"
jooan_camera_ptz_host: "REPLACE_WITH_PRIVATE_RFC1918_ADDRESS"
jooan_camera_ptz_username: "NOT_A_REAL_CAMERA_USER"
jooan_camera_ptz_password: "NOT_A_REAL_CAMERA_PASSWORD"
jooan_camera_ptz_path: "/onvif/example/ptz"
jooan_camera_ptz_profile: "synthetic-profile-token"
```

The supported options are exactly **Normal night vision**, **Scheduled full
color**, and **Smart full color**. Scheduled full color is a device schedule
mode; it is not a promise of always-on full color.

The integration verifies that the configured device is uniquely owned before
publishing a command. It waits for the device acknowledgement and then exposes
the last acknowledged option as assumed state. There is no startup polling,
startup write, startup network command, or device readback; state is unknown
after startup or reload. No application ID is bundled with this package.

## Optional local PTZ

The PTZ backend is disabled unless `ptz_cameras` is explicitly configured under
the integration domain. The camera entity must already exist; this backend
creates no video or camera entity. Credentials, the camera's private RFC1918
address, ONVIF path/profile, and helper entities are all site-specific:

```yaml
jooan_night_vision:
  ptz_cameras:
    - entity_id: camera.example
      host: !secret jooan_camera_ptz_host
      username: !secret jooan_camera_ptz_username
      password: !secret jooan_camera_ptz_password
      ptz_path: !secret jooan_camera_ptz_path
      profile_token: !secret jooan_camera_ptz_profile
      pulse_seconds_entity: input_number.example_ptz_pulse_seconds
      interlocks:
        - binary_sensor.example_ptz_interlock
```

Create the pulse helper explicitly, with a range of 0.10 through 0.25 seconds.
Do not add an `initial` value if a persisted user choice should survive
restart; choose a default only through your own first-installation process:

```yaml
input_number:
  example_ptz_pulse_seconds:
    name: Example PTZ pulse seconds
    min: 0.10
    max: 0.25
    step: 0.01
    mode: box

input_boolean:
  example_ptz_enable:
    name: Enable example PTZ

template:
  - binary_sensor:
      - name: Example PTZ interlock
        unique_id: example_ptz_interlock
        state: "{{ not is_state('input_boolean.example_ptz_enable', 'on') }}"
```

The example address is documentation-only and is rejected by the runtime until
replaced with an RFC1918 IPv4 address. The backend's interlock list is
optional; this example makes it a deliberate safety requirement. The enable
helper has no `initial` value, so a fresh installation starts disabled
(interlock `on`) and Home Assistant can restore a user's persisted choice
across restart. Movement is permitted only after the user explicitly enables
PTZ. If the enable helper is unknown, unavailable, or missing, the template
interlock remains `on` and movement is blocked. Do not use an initialization
automation that overwrites a persisted choice.

PTZ requires an explicit target, profile, ONVIF path, and port 8899
qualification; this is not a universal model-compatibility claim. The only
directions are `left`, `right`, `up`, and `down`; this backend does not expose
zoom. Cancellation, shutdown, and Stop failures are handled fail-closed; a
failed Stop latches the camera until a successful Stop. No startup PTZ command
is issued. This YAML-only integration does not support unloading or reloading;
configuration changes require a Home Assistant restart. Its shutdown handler
blocks new operations and attempts Stop before Home Assistant exits.

Example service calls:

```yaml
action:
  - service: jooan_night_vision.nudge
    target:
      entity_id: camera.example
    data:
      direction: left  # left, right, up, or down
  - service: jooan_night_vision.stop
    target:
      entity_id: camera.example
```

Only one operation owns a camera at a time. A concurrent nudge is rejected,
while Stop remains the recovery action and is coalesced with another Stop.

The ONVIF request includes `PT3.6S`, but this package has not proven that the
camera physically enforces that timeout. If Stop cannot be confirmed, motion
may continue at the camera; do not treat the software latch as a physical
upper bound.

The backend uses the ONVIF PTZ protocol directly. Home Assistant Core's ONVIF
integration may be a separate alternative, but its model and safety behavior
are not assumed to be equivalent to this guarded backend.

## Limitations and privacy

- Cloud mode support depends on the JOOAN service, account, application ID,
  owned device identifiers, and vendor protocol behavior.
- Mode state is assumed from the last acknowledged command; physical readback
  is not supported.
- PTZ support is optional and only suitable for cameras whose ONVIF endpoint,
  profile, credentials, and interlocks have been independently qualified.
- Do not place credentials, private addresses, device identifiers, raw cloud
  responses, camera media, or logs in issues or pull requests.
- The repository contains synthetic examples and test fixtures only.

The vendor endpoint, package name, protocol paths, and command identifiers in
the runtime are interoperability constants. They are not vendor source or
account data; their provenance and compatibility should be independently
reviewed before relying on them.
