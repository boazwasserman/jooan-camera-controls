# Security policy

## Reporting a vulnerability

Please do not disclose credentials, private camera addresses, device IDs,
account data, raw cloud responses, or other sensitive material in a public
issue. Report suspected vulnerabilities through GitHub's private security
advisory mechanism for this repository, if enabled. Otherwise open a minimal
issue containing only a reproducible description with all private values
removed.

## Scope

This project is an unofficial Home Assistant custom integration. Reviewers
should pay particular attention to credential handling, outbound endpoint
validation, device ownership checks, XML parsing, MQTT topic validation, and
PTZ cancellation/Stop recovery.
