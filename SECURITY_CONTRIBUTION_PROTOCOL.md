# Security contribution protocol

Every application pull request must contain the complete `Security review`
section from the pull-request template. A separate reviewer must inspect the
complete final diff and the affected runtime paths before merge.

The report must identify the reviewed commit, reviewer, classification, scope,
authentication and authorization, secrets/privacy/logging, input/URL/injection
checks, dependencies/container/deployment/rollback, tests and live checks,
findings and regression tests, residual risk or unverified behavior, and a
`Decision: PASS` result. Every checked validation item must be true for the
named final commit. Testable findings require deterministic regression tests.

Do not include credentials, private addresses, device or account identifiers,
raw responses, logs, media, or screenshots containing private information.
Live evidence, when applicable, uses ISO 8601 timestamps and sanitized
observations. Documentation-only changes must still receive the structured
review so the repository's gate remains auditable.
