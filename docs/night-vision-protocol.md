# JOOAN night-vision protocol notes

Interoperability notes for the user's own device, derived from the vendor
desktop client's native code and QML. Values are synthetic; no account,
device or credential data belongs here.

| Item | Wire form | Evidence |
| --- | --- | --- |
| Mode set | `{"cmd_type":"request","cmd":66435,"value":N}` to `qaiot/mqtt/<device>` | Desktop night-vision radio buttons: 0 Normal night vision, 1 Scheduled full color, 2 Smart full color. Device answers `cmd_type: response`, `cmd: 66435`, same `value`. |
| Schedule set | `{"cmd_type":"request","cmd":66452,"newflood_light_schedule":{...}}` | Native `setTimingFullColor` builder. Device answers `cmd: 66452` with `status` and the stored schedule. |
| Mode read | Cloud device list, `jooan_info.properties.floodlight` | Desktop client assigns `nightVisionMode = floodlight`. Updated within seconds of a `66435` write in live testing. |
| Schedule read | Cloud device list, `jooan_info.properties.newflood_light_schedule` | Same record; updated immediately after a `66452` write. |

Schedule object: `{"week":[1,2,3,4,5,6,7],"week1":[...],...,"week7":[...]}`
where each list holds `HH:00:00-HH:00:00` ranges. The window 20:00-06:00 is
stored as `["00:00:00-06:00:00","20:00:00-24:00:00"]` for every day. The
desktop client works in whole hours, so the integration only accepts whole
hours.

| Off | `{"cmd_type":"request","cmd":66435,"value":3}` | Android app 5.4.0.98 (`NewLightModelActivity.showCloseDialog` calls `CommandFactory.setLightModel(3)`, offered when `support_turn_off_night_mode` is `Y`). Live: device acknowledged and the cloud `floodlight` property read `3`; mode 1 and the 20:00-06:00 schedule were restored afterwards. |
| Time zone set | `{"cmd_type":"request","cmd":66357,"value":"GMT+02:00"}` | Android app `TimeZoneActivity` -> `CommandFactory.getTimezoneCommand`; the app's list is 34 `GMT` offsets. Live: acknowledged, cloud `timezone` changed from `GMT+03:00` to `GMT+02:00` and the camera's on-screen clock (checked on a Frigate frame) corrected by one hour. |
| Time zone read | Cloud device list, `jooan_info.properties.timezone` | Mirrors the last `GMT+hh:mm` value. |

Time zone notes. The app also contains an unused `TimeZoneCommand` that carries
`timezone_id` and summer-time fields. A live test sending `timezone_id` along
with the value left the cloud `timezone_id` property unchanged, so that
property is written at provisioning only and the integration does not send it.
The `GMT` value is a fixed offset; the camera is not told about daylight saving.
The app also defines `66482` (`setDeviceTime`, an integer value, never called
by the app). Sending the current UTC seconds got no acknowledgement from the
camera, so the integration does not use it. The `66511` fixed-time reboot
command was not touched.

Discovery was from the public Android package `com.jooan.Androidcam` 5.4.0.98
decompiled offline with jadx; the app was never run. No command identifiers
were brute-forced.
