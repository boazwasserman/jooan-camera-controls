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

Not found: a command value for a day-only "Off" mode. The device lists
`support_turn_off_night_mode`, but the desktop client offers only modes 0-2, so
Off is not exposed. No other command identifiers were probed on a live device.
