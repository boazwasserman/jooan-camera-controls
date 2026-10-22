from __future__ import annotations

import unittest
from pathlib import Path

from tools.check_security_pr import ProtocolError, validate_pull_request_event

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / ".github" / "pull_request_template.md"
WORKFLOW = ROOT / ".github" / "workflows" / "security-protocol.yml"
HEAD = "a" * 40


def completed_body(*, reviewed_commit: str = HEAD, reviewer: str = "review-agent"):
    body = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "- Reviewed commit:": f"- Reviewed commit: `{reviewed_commit}`",
        "- Reviewer:": f"- Reviewer: {reviewer}",
        "- Classification: `security-impacting` / `no runtime attack surface`": "- Classification: no runtime attack surface",
        "- Scope:": "- Scope: documentation and validation workflow",
        "- Authentication/authorization:": "- Authentication/authorization: no runtime route change",
        "- Secrets/privacy/logging:": "- Secrets/privacy/logging: sanitized pattern checks passed",
        "- Input/URL/injection checks:": "- Input/URL/injection checks: event fields parsed without execution",
        "- Dependencies/container/deployment/rollback:": "- Dependencies/container/deployment/rollback: not applicable",
        "- Tests and live checks:": "- Tests and live checks: unit suite passed at 2026-10-22T10:00:00+03:00",
        "- Findings and regression tests: `none` / details": "- Findings and regression tests: none",
        "- Residual risk or unverified behavior: `none` / details": "- Residual risk or unverified behavior: none",
        "- Decision: `PASS` / `FAIL`": "- Decision: PASS",
        "- [ ]": "- [x]",
    }
    for old, new in replacements.items():
        body = body.replace(old, new)
    return body


def event(body: str, *, author: str = "author"):
    return {"pull_request": {"body": body, "head": {"sha": HEAD}, "user": {"login": author}}}


class SecurityProtocolTests(unittest.TestCase):
    def test_completed_report_passes(self):
        validate_pull_request_event(event(completed_body()))

    def test_rejects_stale_commit_self_review_and_non_pass(self):
        for body in (
            completed_body(reviewed_commit="b" * 40),
            completed_body(reviewer="author"),
            completed_body().replace("- Decision: PASS", "- Decision: FAIL"),
        ):
            with self.subTest(body=body):
                with self.assertRaises(ProtocolError):
                    validate_pull_request_event(event(body))

    def test_rejects_unchecked_gate(self):
        with self.assertRaises(ProtocolError):
            validate_pull_request_event(event(completed_body().replace("- [x]", "- [ ]", 1)))

    def test_workflow_uses_trusted_base_and_pinned_actions(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", workflow)
        self.assertIn("github.event.repository.default_branch", workflow)
        self.assertNotIn("github.event.pull_request.head", workflow)
        self.assertRegex(workflow, r"actions/checkout@[0-9a-f]{40}")
        self.assertRegex(workflow, r"actions/setup-python@[0-9a-f]{40}")


if __name__ == "__main__":
    unittest.main()
