#!/usr/bin/env python3
"""Validate the security report and owner attestation for a pull request."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

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
HEAD_RE = re.compile(r"[0-9a-f]{40}")
BODY_HASH_RE = re.compile(r"[0-9a-f]{64}")
MAX_COMMENT_PAGES = 100
MAX_COMMENTS = 10_000
MAX_RESPONSE_BYTES = 1_000_000
API_ROOT = "https://api.github.com"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_open_api = build_opener(_NoRedirect()).open


class ProtocolError(ValueError):
    """Raised when a pull request has not completed the security protocol."""


def _field(body: str, name: str) -> str:
    matches = list(
        re.finditer(
            rf"^[ \t]*-[ \t]+(?:-[ \t]+)?{re.escape(name)}:[ \t]*(.*?)[ \t]*$",
            body,
            re.MULTILINE,
        )
    )
    if len(matches) != 1:
        raise ProtocolError(f"Missing security field: {name}")
    match = matches[0]
    value = match.group(1).strip().strip("`")
    normalized = value.replace("`", "").lower()
    if normalized in PLACEHOLDER_VALUES or value.startswith("<"):
        raise ProtocolError(f"Security field is blank or placeholder: {name}")
    return value


def _context(event: dict[str, Any]) -> tuple[dict[str, Any], str, str, int, str, str]:
    pull_request = event.get("pull_request")
    repository = event.get("repository")
    if isinstance(pull_request, dict):
        body = pull_request.get("body") or ""
        head = pull_request.get("head") or {}
        author = str((pull_request.get("user") or {}).get("login") or "")
    else:
        # Local PRE-PR events intentionally contain only trusted maintainer-built data.
        body = event.get("body") or ""
        head = event.get("head") or {}
        author = str(event.get("author") or "")
    if not isinstance(repository, dict):
        raise ProtocolError("Trusted event does not contain repository metadata")
    repo_name = str(repository.get("full_name") or "")
    owner = repository.get("owner") or {}
    owner_id = owner.get("id")
    owner_login = str(owner.get("login") or "").lower()
    head_sha = str(head.get("sha") or "")
    if not HEAD_RE.fullmatch(head_sha):
        raise ProtocolError("Pull request head SHA is missing or invalid")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo_name):
        raise ProtocolError("Trusted repository full_name is missing or invalid")
    if (
        isinstance(owner_id, bool)
        or not isinstance(owner_id, int)
        or owner_id <= 0
        or not owner_login
    ):
        raise ProtocolError("Trusted repository owner identity is missing or invalid")
    if not isinstance(body, str) or not author:
        raise ProtocolError("Trusted event body or pull request author is missing")
    return repository, body, head_sha, owner_id, owner_login, author


def _next_page(link: str | None, repository: str, commit: str, page: int) -> int | None:
    if not link:
        return None
    entries = link.split(",")
    expected_path = f"/repos/{repository}/commits/{commit}/comments"
    next_pages: list[int] = []
    for entry in entries:
        match = re.fullmatch(r'\s*<([^>]+)>;\s*rel=["\']([^"\']+)["\']\s*', entry)
        if not match:
            raise ProtocolError("GitHub pagination link is malformed")
        parsed = urlsplit(match.group(1))
        if (
            parsed.scheme != "https"
            or parsed.netloc != "api.github.com"
            or parsed.path != expected_path
        ):
            raise ProtocolError("GitHub pagination endpoint is not trusted")
        try:
            query = parse_qs(parsed.query, strict_parsing=True)
        except ValueError:
            raise ProtocolError("GitHub pagination query is malformed") from None
        if set(query) != {"page", "per_page"} or query["per_page"] != ["100"]:
            raise ProtocolError("GitHub pagination query is malformed")
        try:
            linked_page = int(query["page"][0])
        except (KeyError, ValueError):
            raise ProtocolError("GitHub pagination page is malformed") from None
        if match.group(2) == "next":
            next_pages.append(linked_page)
    if len(next_pages) > 1:
        raise ProtocolError("GitHub pagination has duplicate next links")
    if not next_pages:
        return None
    if next_pages[0] != page + 1:
        raise ProtocolError("GitHub pagination is not sequential")
    return next_pages[0]


def fetch_commit_comments(
    repository: str,
    commit: str,
    token: str,
    *,
    opener: Callable[..., Any] = _open_api,
) -> list[dict[str, Any]]:
    """Fetch every comment page from the fixed GitHub HTTPS API endpoint."""
    path = f"/repos/{quote(repository, safe='/')}/commits/{commit}/comments"
    page = 1
    comments: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    while page <= MAX_COMMENT_PAGES:
        url = f"{API_ROOT}{path}?{urlencode({'per_page': 100, 'page': page})}"
        if url in seen_urls:
            raise ProtocolError("GitHub pagination repeated a page")
        seen_urls.add(url)
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "User-Agent": "jooan-security-protocol",
            },
        )
        try:
            with opener(request, timeout=20) as response:
                if hasattr(response, "geturl") and response.geturl() != url:
                    raise ProtocolError("GitHub API redirected to an unexpected URL")
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ProtocolError("GitHub response exceeds the size limit")
                payload = json.loads(raw.decode("utf-8"))
                link = response.headers.get("Link")
        except (HTTPError, URLError, OSError, json.JSONDecodeError, UnicodeDecodeError) as err:
            raise ProtocolError("GitHub commit comments could not be fetched") from err
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ProtocolError("GitHub commit comments response is malformed")
        if len(payload) > 100:
            raise ProtocolError("GitHub page exceeds the maximum size")
        comments.extend(payload)
        if len(comments) > MAX_COMMENTS:
            raise ProtocolError("GitHub comments exceed the size limit")
        next_page = _next_page(link, repository, commit, page)
        if next_page is not None:
            page = next_page
        elif len(payload) < 100:
            return comments
        else:
            page += 1
    raise ProtocolError("GitHub commit comments pagination exceeded the maximum")


def fetch_pull_request(
    repository: str,
    number: int,
    token: str,
    *,
    opener: Callable[..., Any] = _open_api,
) -> dict[str, Any]:
    """Fetch live PR metadata from the fixed GitHub HTTPS API endpoint."""
    url = f"{API_ROOT}/repos/{quote(repository, safe='/')}/pulls/{number}"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "jooan-security-protocol",
        },
    )
    try:
        with opener(request, timeout=20) as response:
            if hasattr(response, "geturl") and response.geturl() != url:
                raise ProtocolError("GitHub API redirected to an unexpected URL")
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ProtocolError("GitHub response exceeds the size limit")
            payload = json.loads(raw.decode("utf-8"))
    except ProtocolError:
        raise
    except (HTTPError, URLError, OSError, json.JSONDecodeError, UnicodeDecodeError) as err:
        raise ProtocolError("GitHub pull request could not be fetched") from err
    if not isinstance(payload, dict):
        raise ProtocolError("GitHub pull request response is malformed")
    return payload


def _parse_attestation(text: str) -> tuple[str, str, str]:
    lines = text.splitlines()
    if len(lines) != 5 or lines[0] != "JOOAN security review attestation v1":
        raise ProtocolError("Owner attestation has an invalid format")
    fields: dict[str, str] = {}
    for line in lines[1:]:
        match = re.fullmatch(r"(Reviewed commit|Reviewer|Body-SHA256|Decision): (.+)", line)
        if not match or match.group(1) in fields:
            raise ProtocolError("Owner attestation has duplicate or malformed fields")
        fields[match.group(1)] = match.group(2)
    if set(fields) != {"Reviewed commit", "Reviewer", "Body-SHA256", "Decision"}:
        raise ProtocolError("Owner attestation is missing a field")
    if not HEAD_RE.fullmatch(fields["Reviewed commit"]) or not BODY_HASH_RE.fullmatch(
        fields["Body-SHA256"]
    ):
        raise ProtocolError("Owner attestation has an invalid commit or body hash")
    if not fields["Reviewer"] or "\n" in fields["Reviewer"] or fields["Decision"] != "PASS":
        raise ProtocolError("Owner attestation is not a PASS for one reviewer")
    return fields["Reviewed commit"], fields["Reviewer"], fields["Body-SHA256"]


def _validate_attestations(
    comments: list[dict[str, Any]],
    *,
    head_sha: str,
    body: str,
    owner_id: int,
    owner_login: str,
    reviewer: str,
) -> int:
    expected_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
    accepted: list[tuple[int, tuple[str, str, str]]] = []
    for comment in comments:
        user = comment.get("user") or {}
        if user.get("id") != owner_id or str(user.get("type") or "") != "User":
            continue
        if str(user.get("login") or "").lower() != owner_login:
            raise ProtocolError("Owner identity in attestation comment does not match trusted owner")
        if comment.get("commit_id") != head_sha:
            continue
        if not str(comment.get("body") or "").startswith("JOOAN security review attestation v1"):
            continue
        try:
            attestation = _parse_attestation(str(comment.get("body") or ""))
        except ProtocolError:
            raise ProtocolError("Current-head owner attestation is malformed or conflicting")
        if attestation != (head_sha, reviewer, expected_hash):
            raise ProtocolError("Current-head owner attestation does not match the report")
        comment_id = comment.get("id")
        if isinstance(comment_id, bool) or not isinstance(comment_id, int) or comment_id <= 0:
            raise ProtocolError("Owner attestation comment ID is invalid")
        accepted.append((comment_id, attestation))
    if not accepted:
        raise ProtocolError("No valid current-head owner attestation was found")
    return accepted[0][0]


def validate_pull_request_event(
    event: dict[str, Any],
    *,
    fetch_comments: Callable[[str, str, str], list[dict[str, Any]]] | None = None,
    fetch_live_pr: Callable[[str, int, str], dict[str, Any]] | None = None,
) -> tuple[int, str, str]:
    repository, body, head_sha, owner_id, owner_login, author = _context(event)
    pull_request = event.get("pull_request") or {}
    values = {name: _field(body, name) for name in FIELDS}
    reviewed_commit = values["Reviewed commit"]
    if reviewed_commit != head_sha:
        raise ProtocolError("Reviewed commit must exactly match PR HEAD")
    reviewer = values["Reviewer"]
    if reviewer.casefold().lstrip("@") == author.casefold():
        raise ProtocolError("Security reviewer must differ from the PR author")
    if values["Classification"] not in {"security-impacting", "no runtime attack surface"}:
        raise ProtocolError("Classification must use a documented value")
    if values["Decision"] != "PASS":
        raise ProtocolError("Final security decision must be PASS")
    for label in CHECKBOXES:
        if not re.search(rf"^- \[[xX]\] {re.escape(label)}\s*$", body, re.MULTILINE):
            raise ProtocolError(f"Required validation checkbox is not checked: {label}")
    if fetch_comments is None:
        token = os.environ.get("GITHUB_TOKEN")
        if not token:
            raise ProtocolError("GITHUB_TOKEN is required to verify the owner attestation")

        def fetcher(repo: str, commit: str, _token: str) -> list[dict[str, Any]]:
            return fetch_commit_comments(repo, commit, token)

    else:
        fetcher = fetch_comments
        token = ""
    comments = fetcher(str(repository["full_name"]), head_sha, token)
    number = pull_request.get("number")
    if number is not None:
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ProtocolError("Pull request number is invalid")
        live_fetcher = fetch_live_pr or (
            lambda repo, pr_number, _token: fetch_pull_request(repo, pr_number, token)
        )
        live = live_fetcher(str(repository["full_name"]), number, token)
        live_head = ((live.get("head") or {}).get("sha"))
        live_body = live.get("body") or ""
        live_author = ((live.get("user") or {}).get("login"))
        if (
            live_head != head_sha
            or live_body != body
            or not isinstance(live_author, str)
            or live_author.casefold() != author.casefold()
        ):
            raise ProtocolError("Live pull request changed during validation")
    comment_id = _validate_attestations(
        comments,
        head_sha=head_sha,
        body=body,
        owner_id=owner_id,
        owner_login=owner_login,
        reviewer=reviewer,
    )
    print(
        f"accepted owner attestation comment={comment_id} "
        f"commit={head_sha} reviewer={reviewer}",
        file=sys.stdout,
    )
    return comment_id, head_sha, reviewer


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
