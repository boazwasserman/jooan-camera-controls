from __future__ import annotations

import hashlib
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.check_security_pr import (
    ProtocolError,
    fetch_commit_comments,
    validate_pull_request_event,
)

ROOT = Path(__file__).parents[1]
TEMPLATE = ROOT / ".github" / "pull_request_template.md"
WORKFLOW = ROOT / ".github" / "workflows" / "security-protocol.yml"
HEAD = "a" * 40
OWNER_ID = 4242
OWNER_LOGIN = "maintainer"
REVIEWER = "local-agent (agent-123)"


def completed_body(*, reviewed_commit: str = HEAD, reviewer: str = REVIEWER) -> str:
    body = TEMPLATE.read_text(encoding="utf-8")
    replacements = {
        "- Reviewed commit:": f"- Reviewed commit: `{reviewed_commit}`",
        "- Reviewer:": f"- Reviewer: {reviewer}",
        "- Classification: `security-impacting` / `no runtime attack surface`": "- Classification: no runtime attack surface",
        "- Scope:": "- Scope: protocol gate",
        "- Authentication/authorization:": "- Authentication/authorization: trusted owner API identity",
        "- Secrets/privacy/logging:": "- Secrets/privacy/logging: no secrets in report",
        "- Input/URL/injection checks:": "- Input/URL/injection checks: fixed HTTPS API",
        "- Dependencies/container/deployment/rollback:": "- Dependencies/container/deployment/rollback: not applicable",
        "- Tests and live checks:": "- Tests and live checks: unit tests passed at 2026-10-22T10:00:00+03:00",
        "- Findings and regression tests: `none` / details": "- Findings and regression tests: none",
        "- Residual risk or unverified behavior: `none` / details": "- Residual risk or unverified behavior: none",
        "- Decision: `PASS` / `FAIL`": "- Decision: PASS",
        "- [ ]": "- [x]",
    }
    for old, new in replacements.items():
        body = body.replace(old, new)
    return body


def event(body: str, *, author: str = "author", number: int | None = None) -> dict:
    pull_request = {
        "body": body,
        "head": {"sha": HEAD},
        "user": {"login": author},
    }
    if number is not None:
        pull_request["number"] = number
    return {
        "repository": {
            "full_name": "owner/jooan",
            "owner": {"id": OWNER_ID, "login": OWNER_LOGIN},
        },
        "pull_request": pull_request,
    }


def owner_comment(
    body: str,
    *,
    comment_id: int | None = 17,
    commit_id: str = HEAD,
    **user,
) -> dict:
    return {
        "id": comment_id,
        "commit_id": commit_id,
        "body": body,
        "user": {"id": OWNER_ID, "login": OWNER_LOGIN, "type": "User", **user},
    }


def attestation(body: str, *, reviewer: str = REVIEWER, commit: str = HEAD, body_hash: str | None = None, decision: str = "PASS") -> str:
    digest = body_hash or hashlib.sha256(body.encode()).hexdigest()
    return (
        "JOOAN security review attestation v1\n"
        f"Reviewed commit: {commit}\n"
        f"Reviewer: {reviewer}\n"
        f"Body-SHA256: {digest}\n"
        f"Decision: {decision}"
    )


class SecurityProtocolTests(unittest.TestCase):
    def check(self, body: str, comments: list[dict] | None = None, **kwargs):
        return validate_pull_request_event(
            event(body, **kwargs),
            fetch_comments=lambda _repo, _commit, _token: comments or [],
        )

    def test_owner_attested_local_agent_report_passes(self):
        body = completed_body()
        result = self.check(body, [owner_comment(attestation(body))])
        self.assertEqual(result, (17, HEAD, REVIEWER))

    def test_local_pre_pr_event_without_pull_request_wrapper_passes(self):
        body = completed_body()
        local_event = {
            "repository": event(body)["repository"],
            "head": {"sha": HEAD},
            "body": body,
            "author": "author",
        }
        result = validate_pull_request_event(
            local_event,
            fetch_comments=lambda _repo, _commit, _token: [owner_comment(attestation(body))],
        )
        self.assertEqual(result[1:], (HEAD, REVIEWER))

    def test_body_only_reviewer_cannot_bypass_attestation(self):
        with self.assertRaises(ProtocolError):
            self.check(completed_body())

    def test_rejects_fabricated_user_and_owner_id_mismatch(self):
        body = completed_body()
        fabricated = owner_comment(attestation(body), id=999, login="attacker")
        wrong_id = owner_comment(attestation(body), id=OWNER_ID + 1)
        for comment in (fabricated, wrong_id):
            with self.subTest(comment=comment):
                with self.assertRaises(ProtocolError):
                    self.check(body, [comment])

    def test_rejects_stale_commit_body_and_reviewer_mismatch(self):
        body = completed_body()
        cases = [
            attestation(body, commit="b" * 40),
            attestation(body, reviewer="other-agent"),
            attestation(body, body_hash="0" * 64),
        ]
        for text in cases:
            with self.subTest(text=text):
                with self.assertRaises(ProtocolError):
                    self.check(body, [owner_comment(text)])

    def test_rejects_conflicting_fail_malformed_and_duplicate_fields(self):
        body = completed_body()
        malformed = attestation(body) + "\nDecision: PASS"
        for text in (attestation(body, decision="FAIL"), malformed):
            with self.subTest(text=text):
                with self.assertRaises(ProtocolError):
                    self.check(body, [owner_comment(text)])

    def test_valid_pass_plus_conflicting_fail_is_rejected(self):
        body = completed_body()
        with self.assertRaises(ProtocolError):
            self.check(
                body,
                [owner_comment(attestation(body)), owner_comment(attestation(body, decision="FAIL"))],
            )

    def test_unrelated_owner_discussion_is_ignored(self):
        body = completed_body()
        result = self.check(
            body,
            [
                owner_comment("Thanks for the update."),
                owner_comment(attestation(body), comment_id=19),
            ],
        )
        self.assertEqual(result[0], 19)

    def test_missing_attestation_id_is_rejected(self):
        body = completed_body()
        with self.assertRaises(ProtocolError):
            self.check(body, [owner_comment(attestation(body), comment_id=None)])

    def test_stale_api_commit_id_is_not_accepted(self):
        body = completed_body()
        with self.assertRaises(ProtocolError):
            self.check(body, [owner_comment(attestation(body), commit_id="b" * 40)])

    def test_local_self_review_is_rejected(self):
        body = completed_body(reviewer="author")
        with self.assertRaises(ProtocolError):
            self.check(body, [owner_comment(attestation(body, reviewer="author"))])

    def test_duplicate_report_field_is_rejected(self):
        body = completed_body() + "\n- Scope: duplicate"
        with self.assertRaises(ProtocolError):
            self.check(body, [owner_comment(attestation(body))])

    def test_identical_duplicate_passes_are_unambiguous(self):
        body = completed_body()
        result = self.check(
            body,
            [owner_comment(attestation(body)), owner_comment(attestation(body), comment_id=18)],
        )
        self.assertEqual(result[1:], (HEAD, REVIEWER))

    def test_changed_live_pr_metadata_is_rejected(self):
        body = completed_body()
        for live in (
            {"head": {"sha": "b" * 40}, "body": body, "user": {"login": "author"}},
            {"head": {"sha": HEAD}, "body": body + "changed", "user": {"login": "author"}},
            {"head": {"sha": HEAD}, "body": body, "user": {"login": "other"}},
        ):
            with self.subTest(live=live):
                with self.assertRaises(ProtocolError):
                    validate_pull_request_event(
                        event(body, number=7),
                        fetch_comments=lambda *_: [owner_comment(attestation(body))],
                        fetch_live_pr=lambda *_: live,
                    )

    def test_missing_token_fails_without_network(self):
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ProtocolError):
                validate_pull_request_event(event(completed_body()))

    def test_pagination_fetches_multiple_pages(self):
        requests = []

        class Response:
            def __init__(self, payload, url, link=""):
                self.headers = {"Link": link}
                self.payload = payload
                self.url = url

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def geturl(self):
                return self.url

            def read(self, _limit=None):
                import json
                return json.dumps(self.payload).encode()

        def opener(request, timeout):
            requests.append(request.full_url)
            self.assertEqual(request.headers["Authorization"], "Bearer token")
            link = "" if len(requests) == 2 else (
                '<https://api.github.com/repos/owner/jooan/commits/'
                + HEAD
                + '/comments?per_page=100&page=2>; rel="next"'
            )
            payload = [{} for _ in range(100)] if len(requests) == 1 else []
            return Response(payload, request.full_url, link)

        comments = fetch_commit_comments("owner/jooan", HEAD, "token", opener=opener)
        self.assertEqual(len(comments), 100)
        self.assertEqual(len(requests), 2)

    def test_short_page_with_next_and_exact_final_page_probe(self):
        requests = []
        canonical = "https://api.github.com/repos/owner/jooan/commits/" + HEAD + "/comments"

        class Response:
            def __init__(self, payload, url, link=""):
                self.payload = payload
                self.url = url
                self.headers = {"Link": link}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def geturl(self):
                return self.url

            def read(self, _limit=None):
                import json
                return json.dumps(self.payload).encode()

        def opener(request, timeout):
            requests.append(request.full_url)
            if len(requests) == 1:
                link = f'<{canonical}?per_page=100&page=2>; rel="next"'
                return Response([{"id": 1}], request.full_url, link)
            return Response([], request.full_url)

        self.assertEqual(
            fetch_commit_comments("owner/jooan", HEAD, "token", opener=opener),
            [{"id": 1}],
        )
        self.assertEqual(len(requests), 2)

        requests.clear()

        def exact_page_opener(request, timeout):
            requests.append(request.full_url)
            payload = [{"id": index} for index in range(100)] if len(requests) == 1 else []
            link = '<https://api.github.com/repos/owner/jooan/commits/' + HEAD + '/comments?per_page=100&page=1>; rel="prev"' if len(requests) == 1 else ""
            return Response(payload, request.full_url, link)

        self.assertEqual(
            len(fetch_commit_comments("owner/jooan", HEAD, "token", opener=exact_page_opener)),
            100,
        )
        self.assertEqual(len(requests), 2)

    def test_last_page_non_next_links_are_valid_and_malicious_short_links_fail(self):
        canonical = "https://api.github.com/repos/owner/jooan/commits/" + HEAD + "/comments"

        class Response:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def geturl(self):
                return "https://api.github.com/repos/owner/jooan/commits/" + HEAD + "/comments?per_page=100&page=1"

            def read(self, _limit=None):
                import json
                return json.dumps([{"id": 1}]).encode()

        class PrevOnlyResponse(Response):
            headers = {
                "Link": f'<{canonical}?per_page=100&page=1>; rel="prev", '
                f'<{canonical}?per_page=100&page=3>; rel="last"'
            }

        self.assertEqual(
            fetch_commit_comments(
                "owner/jooan", HEAD, "token", opener=lambda *_args, **_kwargs: PrevOnlyResponse()
            ),
            [{"id": 1}],
        )

        class ForeignResponse(Response):
            headers = {
                "Link": '<https://evil.example/repos/owner/jooan/commits/'
                + HEAD
                + '/comments?per_page=100&page=2>; rel="next"'
            }

        with self.assertRaises(ProtocolError):
            fetch_commit_comments(
                "owner/jooan", HEAD, "token", opener=lambda *_args, **_kwargs: ForeignResponse()
            )

    def test_pagination_error_and_truncation_fail_closed(self):
        class ErrorResponse:
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def read(self, _limit=None):
                return b"{"

        with self.assertRaises(ProtocolError):
            fetch_commit_comments("owner/jooan", HEAD, "token", opener=lambda *_args, **_kwargs: ErrorResponse())

        class EndlessResponse(ErrorResponse):
            def read(self, _limit=None):
                import json
                return json.dumps([{} for _ in range(100)]).encode()

            headers = {"Link": '<https://api.github.com/next>; rel="next"'}

        with patch("tools.check_security_pr.MAX_COMMENT_PAGES", 2):
            with self.assertRaises(ProtocolError):
                fetch_commit_comments("owner/jooan", HEAD, "token", opener=lambda *_args, **_kwargs: EndlessResponse())

    def test_workflow_uses_trusted_base_pinned_actions_and_read_only_permissions(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("pull_request_target:", workflow)
        self.assertIn("github.event.repository.default_branch", workflow)
        self.assertNotIn("github.event.pull_request.head", workflow)
        self.assertRegex(workflow, r"actions/checkout@[0-9a-f]{40}")
        self.assertRegex(workflow, r"actions/setup-python@[0-9a-f]{40}")
        self.assertIn("GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("pull-requests: read", workflow)


if __name__ == "__main__":
    unittest.main()
