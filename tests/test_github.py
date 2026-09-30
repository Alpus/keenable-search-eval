"""Deterministic transport/lifecycle fixtures, never live GitHub traffic."""

import copy
import json
from datetime import datetime
from types import SimpleNamespace

import httpx
import pytest

from search_eval import github as gh
from search_eval.core import EvalError

HEAD = "a" * 40
BASE = "c" * 40
START = "2026-09-29T10:00:00Z"
REPO = "test/se-attempt"


def page(records):
    return [{"page": 1, "per_page": 30, "items": records}]


def raw_review(*, findings=False):
    return {
        "checks": page(
            [
                {
                    "id": 22,
                    "head_sha": HEAD,
                    "app": {"id": 99},
                    "name": "CodeRabbit Review",
                    "status": "completed",
                    "conclusion": "success",
                    "started_at": "2026-09-29T10:00:01Z",
                    "completed_at": "2026-09-29T10:00:04Z",
                }
            ]
        ),
        "reviews": page(
            [
                {
                    "id": 23,
                    "commit_id": HEAD,
                    "user": {"login": "coderabbitai[bot]", "type": "Bot"},
                    "submitted_at": "2026-09-29T10:00:05Z",
                    "body": "Possible issue" if findings else "",
                }
            ]
        ),
        "issue_comments": page([]),
        "review_comments": page([]),
    }


def terminal(raw, **kwargs):
    return gh.terminal_review(raw, HEAD, START, 99, "CodeRabbit Review", **kwargs)


@pytest.mark.parametrize("findings", [True, False])
def test_fresh_app_sha_bound_completion_including_zero_findings(findings):
    result = terminal(raw_review(findings=findings))
    assert result["status"] == "completed"
    assert result["duration_seconds"] == 5
    assert result["check_id"] == 22 and result["review_id"] == 23


@pytest.mark.parametrize(
    "field,value",
    [
        ("head_sha", "b" * 40),
        ("app", {"id": 100}),
        ("name", "CI"),
        ("status", "in_progress"),
        ("started_at", "2026-09-29T09:59:59Z"),
        ("completed_at", None),
    ],
)
def test_wrong_or_old_check_never_completes(field, value):
    raw = raw_review()
    raw["checks"][0]["items"][0][field] = value
    assert terminal(raw) is None


def test_old_check_id_even_with_new_timestamp_never_completes():
    assert terminal(raw_review(), old_checks=[22]) is None


@pytest.mark.parametrize("mutation", ["missing", "wrong_sha", "old", "human"])
def test_green_check_requires_a_fresh_matching_bot_review(mutation):
    raw = raw_review()
    review = raw["reviews"][0]["items"][0]
    if mutation == "missing":
        raw["reviews"] = page([])
    elif mutation == "wrong_sha":
        review["commit_id"] = "b" * 40
    elif mutation == "old":
        review["submitted_at"] = "2026-09-29T09:59:59Z"
    else:
        review["user"]["login"] = "user"
    assert terminal(raw) is None


def test_failed_check_is_not_zero_findings_success():
    raw = raw_review()
    raw["checks"][0]["items"][0]["conclusion"] = "failure"
    assert terminal(raw)["status"] == "failed"
    with pytest.raises(EvalError, match="App ID"):
        gh.terminal_review(raw, HEAD, START, None, "CodeRabbit Review")


def test_collect_all_pages_preserves_boundaries_and_authors():
    observed = []

    def transport(request):
        number = int(request.url.params["page"])
        observed.append((request.url.path, dict(request.url.params)))
        records = [
            {"id": i, "user": {"type": "User" if i % 2 else "Bot"}}
            for i in (range(30) if number == 1 else range(30, 32))
        ]
        return httpx.Response(
            200,
            json={"check_runs": records} if request.url.path.endswith("check-runs") else records,
        )

    api = gh.GitHub(
        "fake",
        httpx.Client(base_url="https://api.github.test", transport=httpx.MockTransport(transport)),
    )
    raw = gh.collect(api, REPO, 1, HEAD)
    for endpoint in ("reviews", "review_comments", "issue_comments", "checks", "commit_statuses"):
        assert [len(p["items"]) for p in raw[endpoint]] == [30, 2]
        assert len(gh.items(raw, endpoint)) == 32
        assert raw[endpoint][0]["parameters"] == {"per_page": 30, "page": 1}
    assert len(observed) == 10


class Server:
    """Stateful mock records server-side effects even when a write response is lost."""

    def __init__(self):
        self.repo = None
        self.actions = True
        self.comments = []
        self.review_raw = raw_review()
        self.calls = []
        self.lose_create = False
        self.lose_trigger = False
        self.ignore_actions = False
        self.actions_readbacks = []
        self.pr = {
            "number": 1,
            "html_url": "https://github.com/" + REPO + "/pull/1",
            "head": {"sha": HEAD},
            "base": {"ref": "eval-base", "sha": BASE},
        }

    def transport(self, request):
        path, method = request.url.path, request.method
        body = json.loads(request.content) if request.content else None
        self.calls.append((method, path, body))
        if path == "/user":
            return httpx.Response(200, json={"login": "test"})
        if path == "/repos/" + REPO and method == "GET":
            return httpx.Response(200, json=self.repo) if self.repo else httpx.Response(404)
        if path == "/user/repos" and method == "POST":
            self.repo = {**body, "full_name": REPO}
            if self.lose_create:
                self.lose_create = False
                raise httpx.ReadTimeout("fixture lost create response")
            return httpx.Response(201, json=self.repo)
        if path.endswith("/actions/permissions"):
            if method == "PUT" and not self.ignore_actions:
                self.actions = body["enabled"]
            if method == "GET" and self.actions_readbacks:
                return httpx.Response(200, json={"enabled": self.actions_readbacks.pop(0)})
            return httpx.Response(200, json={"enabled": self.actions})
        if path == "/repos/" + REPO + "/pulls":
            return httpx.Response(200, json=[self.pr])
        if path == "/repos/" + REPO + "/pulls/1":
            return httpx.Response(200, json=self.pr)
        if path.endswith("/issues/1/comments"):
            if method == "POST":
                comment = {**body, "id": 1, "created_at": START}
                self.comments.append(comment)
                if self.lose_trigger:
                    self.lose_trigger = False
                    raise httpx.ReadTimeout("fixture lost trigger response")
                return httpx.Response(201, json=comment)
            return httpx.Response(200, json=self.comments)
        if path.endswith("/statuses"):
            return httpx.Response(200, json=gh.items(self.review_raw, "commit_statuses"))
        if path.endswith("/check-runs"):
            records = gh.items(self.review_raw, "checks") if self.comments else []
            return httpx.Response(200, json={"check_runs": records})
        if path.endswith("/pulls/1/reviews"):
            return httpx.Response(200, json=gh.items(self.review_raw, "reviews"))
        if path.endswith("/pulls/1/comments"):
            return httpx.Response(200, json=[])
        raise AssertionError(f"Unexpected fixture route {method} {path}")

    def api(self):
        return gh.GitHub(
            "fake",
            httpx.Client(
                base_url="https://api.github.test", transport=httpx.MockTransport(self.transport)
            ),
        )


def test_private_repository_creation_actions_disabled_and_safe_reuse():
    server, attempt, saved = Server(), {}, []
    api = server.api()
    gh.ensure_repo(
        api, "test", "se-attempt", "marker", lambda: saved.append(copy.deepcopy(attempt)), attempt
    )
    gh.ensure_repo(api, "test", "se-attempt", "marker", lambda: None, attempt)
    assert server.repo["private"] is True and server.actions is False
    assert sum(m == "POST" for m, _, _ in server.calls) == 1
    assert saved[0]["repository_intent"] == REPO
    assert "repository" not in saved[0]


@pytest.mark.parametrize("mismatch", ["public", "marker"])
def test_existing_repository_mismatch_has_no_writes(mismatch):
    server = Server()
    server.repo = {
        "private": mismatch != "public",
        "description": "wrong" if mismatch == "marker" else "marker",
    }
    with pytest.raises(EvalError, match="identity/visibility"):
        gh.ensure_repo(server.api(), "test", "se-attempt", "marker", lambda: None, {})
    assert all(m == "GET" for m, _, _ in server.calls)


def test_actions_disable_waits_for_read_only_propagation(monkeypatch):
    server, attempt, sleeps = Server(), {}, []
    server.actions_readbacks = [True, False]
    monkeypatch.setattr(gh.time, "sleep", sleeps.append)
    gh.ensure_repo(server.api(), "test", "se-attempt", "marker", lambda: None, attempt)
    assert attempt["repository"] == REPO
    permissions = [m for m, p, _ in server.calls if p.endswith("/actions/permissions")]
    assert permissions == ["PUT", "GET", "GET"]
    assert sleeps == [2]
    assert sum(m == "POST" for m, _, _ in server.calls) == 1


def test_actions_disable_must_be_verified(monkeypatch):
    server, attempt, sleeps = Server(), {}, []
    server.ignore_actions = True
    monkeypatch.setattr(gh.time, "sleep", sleeps.append)
    with pytest.raises(EvalError, match="Actions"):
        gh.ensure_repo(server.api(), "test", "se-attempt", "marker", lambda: None, attempt)
    assert "repository" not in attempt
    permissions = [m for m, p, _ in server.calls if p.endswith("/actions/permissions")]
    assert permissions == ["PUT"] + ["GET"] * 10
    assert sleeps == [2] * 9
    assert sum(m == "POST" for m, _, _ in server.calls) == 1


def test_uncertain_repository_creation_reconciles_without_second_post():
    server, attempt = Server(), {}
    server.lose_create = True
    with pytest.raises(httpx.ReadTimeout):
        gh.ensure_repo(server.api(), "test", "se-attempt", "marker", lambda: None, attempt)
    assert attempt["repository_intent"] == REPO
    gh.ensure_repo(server.api(), "test", "se-attempt", "marker", lambda: None, attempt)
    assert sum(m == "POST" for m, _, _ in server.calls) == 1


@pytest.fixture
def lifecycle(monkeypatch, tmp_path):
    server = Server()
    monkeypatch.setenv("GITHUB_TOKEN", "fixture")
    api = server.api()
    monkeypatch.setattr(gh, "GitHub", lambda token: api)
    monkeypatch.setattr(gh, "push_inputs", lambda *args: HEAD)
    monkeypatch.setattr(gh, "coderabbit_settings", lambda *args: {"reviews": {}})
    monkeypatch.setattr(gh.time, "time", lambda: datetime.fromisoformat(START).timestamp() + 10)
    config = SimpleNamespace(
        github_owner="test",
        profiles={},
        limits=SimpleNamespace(review_timeout_seconds=60, poll_seconds=1),
        suite_options={"completion_contract": {"app_id": 99, "check_name": "CodeRabbit Review"}},
    )
    attempt, saved, registered = (
        {
            "id": "attempt",
            "expected_base": BASE,
            "allowance_valid_until": "2099-01-01T00:00:00+00:00",
            "expires_at": "2026-09-29T10:01:00+00:00",
        },
        [],
        [],
    )

    def run():
        return gh.execute(
            config,
            {"input": {}},
            None,
            attempt,
            tmp_path,
            lambda: saved.append(copy.deepcopy(attempt)),
            lambda a: registered.append(copy.deepcopy(a)),
        )

    return server, attempt, saved, registered, run


def test_execute_trigger_and_completion(lifecycle):
    server, attempt, saved, registered, run = lifecycle
    assert run()["status"] == "completed"
    trigger_posts = [c for c in server.calls if c[0] == "POST" and c[1].endswith("/comments")]
    assert len(trigger_posts) == 1
    assert registered[0]["head_sha"] == HEAD
    assert any("trigger_intent" in a and "trigger_id" not in a for a in saved)
    assert attempt["trigger_id"] == 1


def test_uncertain_trigger_resume_does_not_duplicate(lifecycle):
    server, attempt, _, _, run = lifecycle
    server.lose_trigger = True
    with pytest.raises(httpx.ReadTimeout):
        run()
    assert "trigger_intent" in attempt and len(server.comments) == 1
    assert run()["status"] == "completed"
    assert len(server.comments) == 1
    assert sum(m == "POST" and p.endswith("/comments") for m, p, _ in server.calls) == 1


def test_unreconciled_trigger_intent_never_resends(lifecycle):
    server, attempt, _, _, run = lifecycle
    attempt["trigger_intent"] = START
    with pytest.raises(EvalError, match="never automatically resend"):
        run()
    assert not server.comments


def test_duplicate_trigger_detected(lifecycle):
    server, _, _, _, run = lifecycle
    server.comments = [{"body": "<!-- search-eval:attempt -->"}] * 2
    with pytest.raises(EvalError, match="Duplicate review triggers"):
        run()
    assert not any(m == "POST" and p.endswith("/comments") for m, p, _ in server.calls)


def test_resume_keeps_original_deadline_and_does_not_retrigger(lifecycle, monkeypatch):
    server, attempt, _, _, run = lifecycle
    server.comments = [{"body": "<!-- search-eval:attempt -->", "id": 1, "created_at": START}]
    attempt["trigger_intent"] = START
    monkeypatch.setattr(gh.time, "time", lambda: datetime.fromisoformat(START).timestamp() + 61)
    result = run()
    assert result["status"] == "completed"
    assert attempt["expires_at"] == "2026-09-29T10:01:00+00:00"
    assert not any(m == "POST" and p.endswith("/comments") for m, p, _ in server.calls)


def test_pr_head_drift_rejected_before_trigger(lifecycle):
    server, _, _, _, run = lifecycle
    server.pr["head"]["sha"] = "b" * 40
    with pytest.raises(EvalError, match="PR identity/head"):
        run()
    assert not server.comments


def test_neutral_check_is_nonterminal_even_with_submitted_review():
    raw = raw_review()
    raw["checks"][0]["items"][0]["conclusion"] = "neutral"
    assert terminal(raw) is None


def test_pr_base_drift_rejected_before_trigger(lifecycle):
    server, _, _, _, run = lifecycle
    server.pr["base"]["sha"] = "d" * 40
    with pytest.raises(EvalError, match="PR identity/head"):
        run()
    assert not server.comments


def test_poll_detects_base_drift_after_trigger(lifecycle):
    server, _, _, _, run = lifecycle

    class Comments(list):
        def append(self, item):
            super().append(item)
            server.pr["base"]["sha"] = "d" * 40

    server.comments = Comments()
    with pytest.raises(EvalError, match="drift"):
        run()


@pytest.mark.parametrize("base,expected", [(BASE, BASE), ("d" * 40, BASE), (BASE, None)])
def test_push_resume_reconciles_base_sha_without_mutation(tmp_path, base, expected):
    calls = []

    def transport(request):
        calls.append(request.method)
        return httpx.Response(
            200, json={"object": {"sha": HEAD if request.url.path.endswith("eval-head") else base}}
        )

    api = gh.GitHub(
        "fixture",
        httpx.Client(base_url="https://api.github.test", transport=httpx.MockTransport(transport)),
    )
    task = {"input": {"source_repo": "source/repo", "base_sha": "1" * 40, "head_sha": "2" * 40}}
    attempt = {"expected_head": HEAD, "expected_base": expected}
    if base == expected:
        assert gh.push_inputs(api, task, {}, REPO, tmp_path, attempt, lambda: None) == HEAD
    else:
        with pytest.raises(EvalError, match="reconciled"):
            gh.push_inputs(api, task, {}, REPO, tmp_path, attempt, lambda: None)
    assert calls == ["GET", "GET"]


def test_deadline_starts_at_trigger_not_source_preparation(lifecycle, monkeypatch):
    server, attempt, _, registered, run = lifecycle
    attempt.pop("expires_at")
    monkeypatch.setattr(gh, "now", lambda: START)
    assert run()["status"] == "completed"
    assert attempt["expires_at"] == "2026-09-29T10:01:00+00:00"
    assert registered[0]["expires_at"] == attempt["expires_at"]


def test_allowance_expired_during_preparation_never_triggers(lifecycle, monkeypatch):
    server, attempt, _, registered, run = lifecycle
    attempt.pop("expires_at")
    attempt["allowance_valid_until"] = START
    monkeypatch.setattr(gh, "now", lambda: START)
    with pytest.raises(EvalError, match="expired during source"):
        run()
    assert registered == [] and server.comments == []


def test_empty_repository_conflict_is_missing_ref_for_bootstrap():
    seen = []

    def transport(request):
        seen.append((request.method, request.url.path))
        return httpx.Response(409, json={"message": "Git Repository is empty.", "status": "409"})

    api = gh.GitHub(
        "fixture",
        httpx.Client(base_url="https://api.github.test", transport=httpx.MockTransport(transport)),
    )
    for branch in ("eval-head", "eval-base"):
        assert (
            api.call(
                "GET",
                f"/repos/Alpus/se-completion-bootstrap-001/git/ref/heads/{branch}",
                missing_ok=True,
            )
            is None
        )
    assert len(seen) == 2 and all(method == "GET" for method, _ in seen)


@pytest.mark.parametrize(
    "method,path,missing_ok,payload",
    [
        (
            "GET",
            "/repos/owner/repo/git/ref/heads/main",
            False,
            {"message": "Git Repository is empty."},
        ),
        (
            "POST",
            "/repos/owner/repo/git/ref/heads/main",
            True,
            {"message": "Git Repository is empty."},
        ),
        ("GET", "/repos/owner/repo/pulls", True, {"message": "Git Repository is empty."}),
        (
            "GET",
            "/repos/owner/repo/git/ref/heads/main",
            True,
            {"message": "Reference update conflict"},
        ),
        ("GET", "/repos/owner/repo/git/ref/heads/main", True, ["Git Repository is empty."]),
        ("GET", "/repos/owner/repo/git/ref/heads/main", True, None),
    ],
)
def test_other_github_conflicts_are_not_suppressed(method, path, missing_ok, payload):
    def transport(request):
        return (
            httpx.Response(409, json=payload)
            if payload is not None
            else httpx.Response(409, text="Not JSON")
        )

    api = gh.GitHub(
        "fixture",
        httpx.Client(base_url="https://api.github.test", transport=httpx.MockTransport(transport)),
    )
    with pytest.raises(EvalError, match="HTTP 409"):
        api.call(method, path, missing_ok=missing_ok)


STATUS_CONTRACT = {
    "kind": "commit_status",
    "context": "CodeRabbit",
    "creator_id": 136622811,
    "creator_login": "coderabbitai[bot]",
    "success_description": "Fixture completed review",
}


def status_raw():
    raw = raw_review()
    raw["checks"] = page([])
    raw["reviews"][0]["items"][0]["user"]["id"] = STATUS_CONTRACT["creator_id"]
    raw["commit_statuses"] = [
        {
            "page": 1,
            "per_page": 30,
            "endpoint": f"/repos/{REPO}/commits/{HEAD}/statuses",
            "items": [
                {
                    "id": 101,
                    "creator": {
                        "id": STATUS_CONTRACT["creator_id"],
                        "login": STATUS_CONTRACT["creator_login"],
                    },
                    "context": "CodeRabbit",
                    "state": "success",
                    "description": STATUS_CONTRACT["success_description"],
                    "created_at": "2026-09-29T10:00:03Z",
                    "updated_at": "2026-09-29T10:00:04Z",
                    "url": f"https://api.github.com/repos/{REPO}/statuses/{HEAD}",
                }
            ],
        }
    ]
    return raw


def status_terminal(raw, **kwargs):
    return gh.terminal_review(
        raw, HEAD, START, completion_contract=STATUS_CONTRACT, repo=REPO, **kwargs
    )


def test_commit_status_completion_requires_fresh_review_and_binds_status_identity():
    result = status_terminal(status_raw())
    assert result["status"] == "completed" and result["completion_kind"] == "commit_status"
    assert result["status_id"] == 101 and result["review_id"] == 23
    assert result["duration_seconds"] == 5


@pytest.mark.parametrize(
    "mutation",
    [
        "creator_id",
        "creator_login",
        "context",
        "status_sha",
        "endpoint_sha",
        "created_old",
        "updated_old",
        "missing_time",
        "skipped_description",
        "pending",
        "missing_review",
        "review_sha",
        "review_creator",
        "review_old",
        "old_status_id",
    ],
)
def test_commit_status_rejects_incomplete_or_mismatched_evidence(mutation):
    raw = status_raw()
    status = raw["commit_statuses"][0]["items"][0]
    review = raw["reviews"][0]["items"][0]
    if mutation == "creator_id":
        status["creator"]["id"] += 1
    elif mutation == "creator_login":
        status["creator"]["login"] = "another-bot"
    elif mutation == "context":
        status["context"] = "Unrelated check"
    elif mutation == "status_sha":
        status["url"] = status["url"].replace(HEAD, BASE)
    elif mutation == "endpoint_sha":
        raw["commit_statuses"][0]["endpoint"] = raw["commit_statuses"][0]["endpoint"].replace(
            HEAD, BASE
        )
    elif mutation == "created_old":
        status["created_at"] = "2026-09-29T09:59:59Z"
    elif mutation == "updated_old":
        status["updated_at"] = "2026-09-29T09:59:59Z"
    elif mutation == "missing_time":
        status.pop("created_at")
    elif mutation == "skipped_description":
        status["description"] = "Review skipped: automatic reviews are disabled"
    elif mutation == "pending":
        status["state"] = "pending"
    elif mutation == "missing_review":
        raw["reviews"] = page([])
    elif mutation == "review_sha":
        review["commit_id"] = BASE
    elif mutation == "review_creator":
        review["user"]["id"] += 1
    elif mutation == "review_old":
        review["submitted_at"] = "2026-09-29T09:59:59Z"
    assert status_terminal(raw, old_statuses=[101] if mutation == "old_status_id" else []) is None


@pytest.mark.parametrize("reverse", [False, True])
def test_latest_pending_status_blocks_older_success_regardless_of_collection_order(reverse):
    raw = status_raw()
    statuses = raw["commit_statuses"][0]["items"]
    pending = copy.deepcopy(statuses[0])
    pending.update(
        id=102,
        state="pending",
        description="Review in progress",
        created_at="2026-09-29T10:00:06Z",
        updated_at="2026-09-29T10:00:06Z",
    )
    statuses.append(pending)
    if reverse:
        statuses.reverse()
    assert status_terminal(raw) is None


def test_commit_status_contract_needs_observed_success_description():
    contract = dict(STATUS_CONTRACT)
    contract.pop("success_description")
    with pytest.raises(EvalError, match="observed creator, context and success description"):
        gh.terminal_review(status_raw(), HEAD, START, completion_contract=contract, repo=REPO)


def test_latest_status_failure_is_terminal_but_unknown_state_is_not_completion():
    raw = status_raw()
    raw["commit_statuses"][0]["items"][0]["state"] = "failure"
    assert status_terminal(raw)["status"] == "failed"
    raw["commit_statuses"][0]["items"][0]["state"] = "unknown"
    assert status_terminal(raw) is None


def test_sanitized_observed_bootstrap_status_sequence():
    # Observed 2026-09-29 bootstrap. Repository/SHA are replaced by fixture values;
    # status/review IDs, timestamps, bot identity and descriptions are preserved.
    raw = status_raw()
    template = raw["commit_statuses"][0]["items"][0]
    observed = [
        (55189785866, "success", "Review completed", "2026-09-29T16:06:51Z"),
        (55189564769, "pending", "Review in progress", "2026-09-29T16:04:25Z"),
        (
            55189525056,
            "success",
            "Review skipped: automatic reviews are disabled",
            "2026-09-29T16:03:59Z",
        ),
    ]
    raw["commit_statuses"][0]["items"] = [
        {
            **copy.deepcopy(template),
            "id": ident,
            "state": state,
            "description": description,
            "created_at": stamp,
            "updated_at": stamp,
        }
        for ident, state, description, stamp in observed
    ]
    raw["reviews"][0]["items"][0].update(
        id=5355255648, state="COMMENTED", submitted_at="2026-09-29T16:06:42Z"
    )
    contract = {**STATUS_CONTRACT, "success_description": "Review completed"}
    result = gh.terminal_review(
        raw, HEAD, "2026-09-29T16:03:48Z", completion_contract=contract, repo=REPO
    )
    assert result["status"] == "completed" and result["duration_seconds"] == 183
    raw["commit_statuses"][0]["items"].pop(0)
    assert (
        gh.terminal_review(
            raw, HEAD, "2026-09-29T16:03:48Z", completion_contract=contract, repo=REPO
        )
        is None
    )
    raw["commit_statuses"][0]["items"].pop(0)
    assert (
        gh.terminal_review(
            raw, HEAD, "2026-09-29T16:03:48Z", completion_contract=contract, repo=REPO
        )
        is None
    )


ZERO_TRIGGER = "2026-09-29T16:11:51Z"
ZERO_CONTRACT = {
    **STATUS_CONTRACT,
    "success_description": "Review completed",
    "zero_findings_app_id": 347564,
}


def zero_findings_raw():
    """Sanitized minimal fields from the observed 2026-09-29 no-actionable walkthrough."""
    raw = status_raw()
    raw["reviews"] = page([])
    raw["commit_statuses"][0]["items"][0].update(
        id=55190561694,
        description="Review completed",
        created_at="2026-09-29T16:15:35Z",
        updated_at="2026-09-29T16:15:35Z",
    )
    body = (
        "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->\n"
        "<!-- recent_review_start -->\n\n"
        "No actionable comments were generated in the recent review. 🎉\n\n"
        "<details>\n<summary>📥 Commits</summary>\n\n"
        f"Reviewing files that changed from the base of the PR and between {BASE} and {HEAD}.\n"
        "</details>\n<!-- recent_review_end -->\n"
        "<!-- walkthrough_start -->\nThe function body is unchanged.\n<!-- walkthrough_end -->\n"
        "<!-- final_review_risk_start -->\n"
        '<!-- final_review_risk_coverage:{"sourceCommitId":"'
        + HEAD
        + '","coveredCommitId":"'
        + HEAD
        + '","kind":"reviewed"} -->\n'
        "<!-- final_review_risk_end -->\n"
    )
    raw["issue_comments"] = [
        {
            "page": 1,
            "per_page": 30,
            "endpoint": f"/repos/{REPO}/issues/1/comments",
            "items": [
                {
                    "id": 5894070781,
                    "user": {"id": 136622811, "login": "coderabbitai[bot]"},
                    "performed_via_github_app": {"id": 347564},
                    "created_at": "2026-09-29T16:11:59Z",
                    "updated_at": "2026-09-29T16:15:31Z",
                    "body": body,
                }
            ],
        }
    ]
    return raw


def zero_terminal(raw, contract=None):
    return gh.terminal_review(
        raw, HEAD, ZERO_TRIGGER, completion_contract=contract or ZERO_CONTRACT, repo=REPO
    )


def test_observed_zero_findings_walkthrough_completes_without_submitted_review():
    result = zero_terminal(zero_findings_raw())
    assert result["status"] == "completed"
    assert result["completion_evidence"] == "zero_findings_walkthrough"
    assert result["walkthrough_comment_id"] == 5894070781
    assert result["status_id"] == 55190561694 and result["duration_seconds"] == 224
    assert "review_id" not in result


@pytest.mark.parametrize(
    "mutation",
    [
        "app",
        "null_app",
        "user_id",
        "user_login",
        "updated_old",
        "range_head",
        "source_head",
        "covered_head",
        "coverage_kind",
        "missing_marker",
        "marker_outside_recent",
        "generic_reply",
        "duplicate_coverage",
        "wrong_endpoint",
        "pending_status",
        "skipped_status",
        "missing_app_contract",
    ],
)
def test_zero_findings_completion_fails_closed(mutation):
    raw = zero_findings_raw()
    comment = raw["issue_comments"][0]["items"][0]
    contract = dict(ZERO_CONTRACT)
    if mutation == "app":
        comment["performed_via_github_app"]["id"] += 1
    elif mutation == "null_app":
        comment["performed_via_github_app"] = None
    elif mutation == "user_id":
        comment["user"]["id"] += 1
    elif mutation == "user_login":
        comment["user"]["login"] = "untrusted"
    elif mutation == "updated_old":
        comment["updated_at"] = "2026-09-29T16:11:50Z"
    elif mutation == "range_head":
        comment["body"] = comment["body"].replace(f"and {HEAD}.", f"and {BASE}.")
    elif mutation == "source_head":
        comment["body"] = comment["body"].replace(
            f'"sourceCommitId":"{HEAD}"', f'"sourceCommitId":"{BASE}"'
        )
    elif mutation == "covered_head":
        comment["body"] = comment["body"].replace(
            f'"coveredCommitId":"{HEAD}"', f'"coveredCommitId":"{BASE}"'
        )
    elif mutation == "coverage_kind":
        comment["body"] = comment["body"].replace('"kind":"reviewed"', '"kind":"pending"')
    elif mutation == "missing_marker":
        comment["body"] = comment["body"].replace(
            "No actionable comments were generated in the recent review.", "No issues."
        )
    elif mutation == "marker_outside_recent":
        marker = "No actionable comments were generated in the recent review. 🎉"
        comment["body"] = comment["body"].replace(marker, "") + "\n" + marker
    elif mutation == "generic_reply":
        comment["body"] = (
            "<!-- This is an auto-generated reply by CodeRabbit -->\nFull review finished."
        )
    elif mutation == "duplicate_coverage":
        comment["body"] = comment["body"].replace(
            "<!-- final_review_risk_end -->",
            "<!-- final_review_risk_coverage:{} -->\n<!-- final_review_risk_end -->",
        )
    elif mutation == "wrong_endpoint":
        raw["issue_comments"][0]["endpoint"] = "/repos/other/repo/issues/1/comments"
    elif mutation == "pending_status":
        raw["commit_statuses"][0]["items"][0]["state"] = "pending"
    elif mutation == "skipped_status":
        raw["commit_statuses"][0]["items"][0]["description"] = (
            "Review skipped: automatic reviews are disabled"
        )
    elif mutation == "missing_app_contract":
        contract.pop("zero_findings_app_id")
    assert zero_terminal(raw, contract) is None


def test_resumed_existing_trigger_skips_quota_wait(lifecycle, monkeypatch):
    server, attempt, _, _, run = lifecycle
    server.comments = [{"body": "<!-- search-eval:attempt -->", "id": 1, "created_at": START}]
    attempt["trigger_intent"] = START
    monkeypatch.setattr(
        gh, "pace_review_trigger", lambda *args: pytest.fail("resume must not reserve quota")
    )
    assert run()["status"] == "completed"
    assert not any(m == "POST" and p.endswith("/comments") for m, p, _ in server.calls)


def test_new_trigger_waits_before_intent_and_deadline(lifecycle, monkeypatch):
    _, attempt, _, registered, run = lifecycle
    attempt.pop("expires_at")

    def pacing(*args):
        assert "trigger_intent" not in attempt and "expires_at" not in attempt
        monkeypatch.setattr(gh, "now", lambda: START)

    monkeypatch.setattr(gh, "pace_review_trigger", pacing)
    assert run()["status"] == "completed"
    assert registered[0]["trigger_intent"] == START
    assert attempt["expires_at"] == "2026-09-29T10:01:00+00:00"


def test_transient_read_after_trigger_resumes_only_gets(lifecycle, monkeypatch):
    server, attempt, _, _, run = lifecycle
    original = gh.collect
    failed = False

    def collect(*args):
        nonlocal failed
        if not failed:
            failed = True
            raise gh.GitHubReadError("GitHub GET fixture: HTTP 503")
        return original(*args)

    monkeypatch.setattr(gh, "collect", collect)
    with pytest.raises(gh.GitHubReadError):
        run()
    assert gh.confirmed_trigger(attempt)
    previous = len(server.calls)
    assert run()["status"] == "completed"
    assert all(method == "GET" for method, _, _ in server.calls[previous:])
    assert (
        sum(method == "POST" and path.endswith("/comments") for method, path, _ in server.calls)
        == 1
    )


@pytest.mark.parametrize(
    "completion,expected",
    [("2026-09-29T10:00:50Z", "completed"), ("2026-09-29T10:01:01Z", "timed_out")],
)
def test_expired_confirmed_review_gets_one_final_read(lifecycle, monkeypatch, completion, expected):
    server, attempt, _, _, run = lifecycle
    assert run()["status"] == "completed"
    server.review_raw["reviews"][0]["items"][0]["submitted_at"] = completion
    server.review_raw["checks"][0]["items"][0]["completed_at"] = completion
    monkeypatch.setattr(gh.time, "time", lambda: datetime.fromisoformat(START).timestamp() + 120)
    before = len(server.calls)
    result = run()
    assert result["status"] == expected
    assert all(method == "GET" for method, _, _ in server.calls[before:])
    assert sum(path.endswith("/reviews") for _, path, _ in server.calls[before:]) == 1
    if expected == "timed_out":
        assert result["late_completion"]["status"] == "completed"


@pytest.mark.parametrize("method,error", [("GET", gh.GitHubReadError), ("POST", EvalError)])
def test_transient_http_status_is_resumable_only_for_get(method, error):
    client = httpx.Client(
        base_url="https://api.github.test",
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    )
    with pytest.raises(error):
        gh.GitHub("fixture", client).call(method, "/fixture")


def test_updated_existing_zero_findings_walkthrough_completes():
    raw = zero_findings_raw()
    raw["issue_comments"][0]["items"][0]["created_at"] = "2026-09-29T15:00:00Z"
    assert zero_terminal(raw)["status"] == "completed"
