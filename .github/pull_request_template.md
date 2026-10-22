## What changed

- Acceptance criteria:
- Runtime/data flow affected:
- Rollback boundary:

## Validation

- [ ] Targeted tests passed
- [ ] Full wrapper passed, or every blocker/failure is documented with equivalent validation
- [ ] Relevant dependency audits/builds passed or are not applicable
- [ ] Live checks include ISO timestamps and sanitized evidence, or are not applicable
- [ ] Documentation and deployment inputs are consistent

## Security review

- Reviewed commit:
- Reviewer:
- Classification: `security-impacting` / `no runtime attack surface`
- Scope:
- Authentication/authorization:
- Secrets/privacy/logging:
- Input/URL/injection checks:
- Dependencies/container/deployment/rollback:
- Tests and live checks:
- Findings and regression tests: `none` / details
- Residual risk or unverified behavior: `none` / details
- Decision: `PASS` / `FAIL`

- [ ] A separate reviewer inspected the complete final diff and affected runtime paths
- [ ] Every testable security finding has a deterministic regression test
- [ ] The final reviewed commit is named above

See [`SECURITY_CONTRIBUTION_PROTOCOL.md`](SECURITY_CONTRIBUTION_PROTOCOL.md).
