# Security contribution protocol

Every application pull request must contain the complete `Security review`
section from the pull-request template. A separate reviewer must inspect the
complete final diff and the affected runtime paths before merge. The reviewer
may be a separate local AI agent under the same maintainer GitHub account; this
is an owner-authenticated agent attestation, not a second human or GitHub
account approval.

The report must identify the reviewed commit, reviewer, classification, scope,
authentication and authorization, secrets/privacy/logging, input/URL/injection
checks, dependencies/container/deployment/rollback, tests and live checks,
findings and regression tests, residual risk or unverified behavior, and a
`Decision: PASS` result. Every checked validation item must be true for the
named final commit. Testable findings require deterministic regression tests.

The trusted `pull_request_target` workflow verifies the report against the
immutable 40-character head SHA and the repository owner's numeric ID from the
trusted event. It fetches all pages of commit comments from the fixed
`https://api.github.com` endpoint using the read-only `GITHUB_TOKEN`. At least
one current-head owner comment must contain exactly these five lines:

```text
JOOAN security review attestation v1
Reviewed commit: <40 lowercase hex>
Reviewer: <exact Reviewer field from the report>
Body-SHA256: <64 lowercase hex SHA-256 of the complete PR body>
Decision: PASS
```

The comment author must have the trusted owner ID, owner login, and GitHub
`User` type. Malformed, duplicate, failed, stale, conflicting, or
body/reviewer-mismatched current-head owner comments fail closed. Identical
repeated PASS comments are allowed. Unrelated and non-owner comments are
ignored. The CLI also accepts a locally constructed trusted event, so the exact
pushed commit can be attested before a PR exists; the commit must be
addressable in the canonical repository. Fork-only commits cannot be verified
by this gate. Re-run the local check and workflow after every new commit or
body change. Because the body digest is exact, update the existing owner
attestation comment (or add an identical new one) after every report-body edit
before pre-PR or workflow validation; an old same-head hash is a conflict.

Do not include credentials, private addresses, device or account identifiers,
raw responses, logs, media, or screenshots containing private information.
Live evidence, when applicable, uses ISO 8601 timestamps and sanitized
observations. Documentation-only changes must still receive the structured
review so the repository's gate remains auditable.
