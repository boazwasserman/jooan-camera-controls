#!/usr/bin/env python3
"""Validate the mandatory security report in a GitHub pull-request event."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

FIELDS = (
    "Reviewed commit",
    "Reviewer",
    "Classification",
    "Scope",
    "Authentication/authorization",
    "Secrets/privacy/logging",
    "Input/URL/injection checks",
    "Dependencies/container/deployment/rollback",
    "Tests and live checks",
    "Findings and regression tests",
    "Residual risk or unverified behavior",
    "Decision",
)

CHECKBOXES = (
    "Targeted tests passed",
    "Full wrapper passed, or every blocker/failure is documented with equivalent validation",
    "Relevant dependency audits/builds passed or are not applicable",
    "Live checks include ISO timestamps and sanitized evidence, or are not applicable",
    "Documentation and deployment inputs are consistent",
    "A separate reviewer inspected the complete final diff and affected runtime paths",
    "Every testable security finding has a deterministic regression test",
    "The final reviewed commit is named above",
)

PLACEHOLDER_VALUES = {
    "",
    "details",
    "none / details",
    "none / details ",
    "security-impacting / no runtime attack surface",
    "pass / fail",
}


class ProtocolError(ValueError):
    """Raised when a pull request has not completed the security protocol."""


def _field(body: str, name: str) -> str:
    match = re.search(
        rf"^[ \t]*-[ \t]+(?:-[ \t]+)?{re.escape(name)}:[ \t]*(.*?)[ \t]*$",
        body,
        re.MULTILINE,
    )
    if not match:
        raise ProtocolError(f"Missing security field: {name}")
    value = match.group(1).strip().strip("`")
    normalized = value.replace("`", "").lower()
    if normalized in PLACEHOLDER_VALUES or value.startswith("<"):
        raise ProtocolError(f"Security field is blank or placeholder: {name}")
    return value


def validate_pull_request_event(event: dict[str, Any]) -> None:
    pull_request = event.get("pull_request")
    if not isinstance(pull_request, dict):
        raise ProtocolError("Event does not contain a pull_request object")

    body = pull_request.get("body") or ""
    head = pull_request.get("head") or {}
    head_sha = str(head.get("sha") or "").lower()
    author = str((pull_request.get("user") or {}).get("login") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise ProtocolError("Pull request head SHA is missing or invalid")
    if not author:
        raise ProtocolError("Pull request author is missing")

    values = {name: _field(body, name) for name in FIELDS}
    reviewed_commit = values["Reviewed commit"].lower()
    if (
        not re.fullmatch(r"[0-9a-f]{7,40}", reviewed_commit)
        or not head_sha.startswith(reviewed_commit)
    ):
        raise ProtocolError("Reviewed commit does not match PR HEAD")

    reviewer = values["Reviewer"].lower().lstrip("@")
    if reviewer == author:
        raise ProtocolError("Security reviewer must differ from the PR author")
    if values["Classification"] not in {
        "security-impacting",
        "no runtime attack surface",
    }:
        raise ProtocolError("Classification must use a documented value")
    if values["Decision"] != "PASS":
        raise ProtocolError("Final security decision must be PASS")

    for label in CHECKBOXES:
        if not re.search(rf"^- \[[xX]\] {re.escape(label)}\s*$", body, re.MULTILINE):
            raise ProtocolError(f"Required validation checkbox is not checked: {label}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--event", required=True, type=Path)
    args = parser.parse_args()
    try:
        event = json.loads(args.event.read_text(encoding="utf-8"))
        validate_pull_request_event(event)
    except (OSError, json.JSONDecodeError, ProtocolError) as err:
        print(f"security protocol failed: {err}", file=sys.stderr)
        return 1
    print("security protocol passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
