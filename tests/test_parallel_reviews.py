"""Cooperative review scheduling against a deterministic fake GitHub service."""

import json
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError
from test_github import BASE, HEAD, page, raw_review
from test_runner import rig as rig

from search_eval import core, github, runner
from search_eval.core import EvalError, atomic_json, prepare_state

REAL_EXECUTE = github.execute


@pytest.fixture
def parallel(rig, tmp_path, monkeypatch):
    cfg, tasks, _, allowance = rig
    tasks.extend(
        dict(id=str(i), role="development", kind="pr_review", input={}) for i in range(2, 5)
    )
    cfg = cfg.model_copy(
        update={
            "github_owner": "test",
            "limits": cfg.limits.model_copy(
                update={"max_concurrent_reviews": 2, "poll_seconds": 10}
            ),
            "suite_options": {
                "completion_contract": {"app_id": 99, "check_name": "CodeRabbit Review"}
            },
        }
    )
    allowance["valid_until"] = "2099-01-01T00:00:00+00:00"
    atomic_json(tmp_path / "allowance.json", allowance)

    class Service:
        current = datetime.now(timezone.utc) + timedelta(seconds=60)
        elapsed = 0
        delays = [20, 10, 30, 10, 20]
        fail_read = False
        fail_trigger = False

        def __init__(self):
            self.repos, self.triggers, self.completed, self.events = {}, {}, set(), []
            self.peak = 0

        def stamp(self, offset=0):
            return (self.current + timedelta(seconds=offset)).isoformat()

        def sleep(self, seconds):
            assert 0 < seconds <= cfg.limits.poll_seconds
            self.elapsed += seconds
            self.current += timedelta(seconds=seconds)
            # Waiting for another poll must preserve gateway access for every active review.
            routes = json.loads((tmp_path / ".gateway/routing/attempts.json").read_text())[
                "attempts"
            ]
            for repo in self.triggers.keys() - self.completed:
                row = routes[repo.split("se-")[1]]
                if datetime.fromisoformat(row["expires_at"]) > self.current:
                    assert row["authorized"] and row["status"] == "running"

        def call(self, method, path, *, body=None, params=None, missing_ok=False):
            if path == "/user":
                return {"login": "test"}
            if path == "/user/repos":
                repo = "test/" + body["name"]
                self.repos[repo] = dict(body, full_name=repo)
                return self.repos[repo]
            repo = "/".join(path.split("/")[2:4])
            suffix = "/".join(path.split("/")[4:])
            if not suffix:
                return self.repos.get(repo)
            if suffix == "actions/permissions":
                return {"enabled": False}
            pr = {
                "number": 1,
                "html_url": f"https://github.com/{repo}/pull/1",
                "head": {"sha": HEAD},
                "base": {"ref": "eval-base", "sha": BASE},
            }
            if suffix == "pulls":
                return [pr]
            if suffix == "pulls/1":
                self.events.append(("poll", repo, self.elapsed))
                if self.fail_read and self.elapsed:
                    self.fail_read = False
                    raise github.GitHubReadError("simulated transient read failure")
                return pr
            if suffix == "issues/1/comments" and method == "POST":
                assert repo not in self.triggers, "Duplicate trigger"
                delay = self.delays[len(self.triggers)]
                self.triggers[repo] = {
                    "id": len(self.triggers) + 1,
                    "created_at": self.stamp(),
                    "body": body["body"],
                    "due": self.elapsed + delay,
                    "completed_at": self.stamp(delay),
                }
                self.events.append(("trigger", repo, self.elapsed))
                self.peak = max(self.peak, len(self.triggers.keys() - self.completed))
                if self.fail_trigger and len(self.triggers) == self.fail_trigger:
                    raise RuntimeError("simulated lost trigger response")
                return self.triggers[repo]
            raise AssertionError((method, path))

        def pages(self, path, key=None):
            repo = "/".join(path.split("/")[2:4])
            suffix = "/".join(path.split("/")[4:])
            trigger = self.triggers.get(repo)
            if suffix == "issues/1/comments":
                return page([trigger] if trigger else [])
            ready = trigger and self.elapsed >= trigger["due"]
            raw = raw_review()
            if suffix.endswith("check-runs"):
                if not ready:
                    return page([])
                self.completed.add(repo)
                self.events.append(("complete", repo, self.elapsed))
                check = raw["checks"][0]["items"][0]
                check.update(started_at=trigger["created_at"], completed_at=trigger["completed_at"])
                return raw["checks"]
            if suffix == "pulls/1/reviews" and ready:
                raw["reviews"][0]["items"][0]["submitted_at"] = trigger["completed_at"]
                return raw["reviews"]
            return page([])

    service = Service()
    monkeypatch.setattr(github, "GitHub", lambda token: service)
    monkeypatch.setattr(github, "execute", REAL_EXECUTE)
    monkeypatch.setattr(github, "now", service.stamp)
    monkeypatch.setattr(runner, "now", service.stamp)
    monkeypatch.setattr(github.time, "time", lambda: service.current.timestamp())
    monkeypatch.setattr(github.time, "sleep", service.sleep)

    def push(api, task, settings, repo, artifact, attempt, save):
        attempt["expected_base"] = BASE
        save()
        return HEAD

    monkeypatch.setattr(github, "push_inputs", push)
    return cfg, tasks, service


def state(tmp_path):
    return json.loads((tmp_path / "runs/test-run/state.json").read_text())


def test_bounded_outstanding_reviews_overlap_and_refill(parallel, tmp_path):
    cfg, _, service = parallel
    assert runner.run(cfg, root=tmp_path)["statuses"] == ["completed"] * 5
    assert service.peak == 2
    starts = [e for e in service.events if e[0] == "trigger"]
    assert [e[2] for e in starts[:3]] == [0, 0, 12]
    assert service.triggers[starts[0][1]]["due"] == 20
    assert len(service.triggers) == 5
    routes = json.loads((tmp_path / ".gateway/routing/attempts.json").read_text())["attempts"]
    assert all(not row["authorized"] and row["status"] == "completed" for row in routes.values())
    assert all(a["completed_at"] <= a["expires_at"] for a in state(tmp_path)["attempts"])


def test_resume_polls_confirmed_before_pending_and_never_retriggers(parallel, tmp_path):
    cfg, _, service = parallel
    service.fail_read = True
    with pytest.raises(github.GitHubReadError):
        runner.run(cfg, root=tmp_path)
    saved = state(tmp_path)
    assert [a["status"] for a in saved["attempts"]] == ["read_pending", "read_pending"] + [
        "pending"
    ] * 3
    routes = json.loads((tmp_path / ".gateway/routing/attempts.json").read_text())["attempts"]
    assert all(routes[a["id"]]["authorized"] for a in saved["attempts"][:2])
    assert all(routes[a["id"]]["status"] == "running" for a in saved["attempts"][:2])
    original = {a["id"]: a["expires_at"] for a in saved["attempts"][:2]}
    # An interrupted state may list pending work before its active reviews.
    saved["attempts"] = saved["attempts"][2:] + saved["attempts"][:2]
    atomic_json(tmp_path / "runs/test-run/state.json", saved)
    before = len(service.events)
    assert runner.run(cfg, root=tmp_path)["statuses"] == ["completed"] * 5
    resumed = service.events[before:]
    first_trigger = next(i for i, event in enumerate(resumed) if event[0] == "trigger")
    assert {event[1] for event in resumed[:first_trigger] if event[0] == "poll"} >= set(
        list(service.triggers)[:2]
    )
    assert len(service.triggers) == 5
    for a in state(tmp_path)["attempts"]:
        if a["id"] in original:
            assert a["expires_at"] == original[a["id"]]


def test_quota_blocks_new_start_but_continues_active_reads(parallel, tmp_path):
    cfg, tasks, service = parallel
    del tasks[2:]
    cfg.limits.max_review_events_per_hour = 1
    assert runner.run(cfg, root=tmp_path)["statuses"] == ["completed"] * 2
    starts = [e for e in service.events if e[0] == "trigger"]
    assert starts[1][2] >= 3622  # First completion at 20, plus the 3602-second window.
    assert any(e[0] == "poll" and e[2] == 10 for e in service.events)
    assert any(e[0] == "complete" and e[2] == 20 for e in service.events)
    assert service.peak == 1


@pytest.mark.parametrize("fail_on", [1, 2])
def test_unknown_trigger_stops_admission_and_resume(parallel, tmp_path, fail_on):
    cfg, _, service = parallel
    service.fail_trigger = fail_on
    with pytest.raises(RuntimeError, match="lost trigger"):
        runner.run(cfg, root=tmp_path)
    assert len(service.triggers) == fail_on
    saved = state(tmp_path)
    assert [a["status"] for a in saved["attempts"]] == (
        ["read_pending"] * (fail_on - 1) + ["unknown"] + ["pending"] * (5 - fail_on)
    )
    uncertain = saved["attempts"][fail_on - 1]
    assert uncertain["trigger_intent"] and "trigger_id" not in uncertain
    routes = json.loads((tmp_path / ".gateway/routing/attempts.json").read_text())["attempts"]
    assert not routes[uncertain["id"]]["authorized"]
    for attempt in saved["attempts"][: fail_on - 1]:
        assert routes[attempt["id"]]["authorized"]
        assert routes[attempt["id"]]["status"] == "running"
    with pytest.raises(EvalError, match="unknown attempts"):
        runner.run(cfg, root=tmp_path)
    assert len(service.triggers) == fail_on


def test_devdex_remains_sequential_with_review_concurrency(rig, tmp_path, monkeypatch):
    cfg, tasks, _, _ = rig
    cfg.limits.max_concurrent_reviews = 3
    core.suite_module(cfg.suite).TASK_KIND = "developer_retrieval"
    for task in tasks:
        task["kind"] = "developer_retrieval"
    monkeypatch.setattr(runner, "checks", lambda *args: ({"live_ready": True}, tasks))
    monkeypatch.setattr(
        runner, "run_reviews", lambda *args: pytest.fail("DevDex used PR scheduler")
    )
    calls = []

    def execute(config, task, arm, attempt, artifact, route, save, root):
        saved = state(tmp_path)
        assert sum(a["status"] == "running" for a in saved["attempts"]) == 1
        assert sum(a["status"] == "completed" for a in saved["attempts"]) == len(calls)
        calls.append(attempt["id"])
        return {"status": "completed"}

    monkeypatch.setattr(runner, "execute_devdex", execute)
    assert runner.run(cfg, root=tmp_path)["statuses"] == ["completed", "completed"]
    assert len(calls) == 2


def test_concurrency_changes_run_and_acceptance_identity(rig, tmp_path):
    cfg, tasks, _, _ = rig
    run_dir = tmp_path / "runs/test-run"
    prepare_state(cfg, tasks, run_dir, tmp_path)
    parallel = cfg.model_copy(
        update={"limits": cfg.limits.model_copy(update={"max_concurrent_reviews": 3})}
    )
    assert runner.acceptance_identity(cfg, tmp_path) != runner.acceptance_identity(
        parallel, tmp_path
    )
    with pytest.raises(EvalError, match="fingerprint changed"):
        prepare_state(parallel, tasks, run_dir, tmp_path)
    assert cfg.limits.max_concurrent_reviews == 1


@pytest.mark.parametrize("value", [0, -1, True, 1.5, 101])
def test_invalid_concurrency_rejected(value):
    with pytest.raises(ValidationError):
        core.Limits(max_attempts=10, max_concurrent_reviews=value)


def test_original_deadline_times_out_and_refills_slot(parallel, tmp_path):
    cfg, tasks, service = parallel
    del tasks[3:]
    cfg.limits.review_timeout_seconds = 30
    service.delays = [100, 100, 10]
    assert runner.run(cfg, root=tmp_path)["statuses"] == ["timed_out", "timed_out", "completed"]
    starts = [e for e in service.events if e[0] == "trigger"]
    assert [e[2] for e in starts] == [0, 0, 40]
    saved = state(tmp_path)
    for attempt in saved["attempts"][:2]:
        assert (
            datetime.fromisoformat(attempt["expires_at"])
            - datetime.fromisoformat(attempt["trigger_time"])
        ).total_seconds() == 30
    routes = json.loads((tmp_path / ".gateway/routing/attempts.json").read_text())["attempts"]
    assert all(not row["authorized"] for row in routes.values())


@pytest.mark.parametrize("concurrency", [1, 2])
def test_completion_between_endpoint_reads_preserves_published_inline_finding(
    parallel, tmp_path, monkeypatch, concurrency
):
    cfg, tasks, service = parallel
    del tasks[1:]
    cfg.limits.max_concurrent_reviews = concurrency
    service.delays = [0]
    original_pages = service.pages
    published = False
    inline_reads = []
    finding = {
        "id": 901,
        "user": {"login": "coderabbitai[bot]", "type": "Bot"},
        "path": "src/example.py",
        "line": 12,
        "body": "This condition drops valid results.",
        "created_at": service.stamp(),
    }

    def pages(path, key=None):
        nonlocal published
        if path.endswith("/pulls/1/comments"):
            # Inline comments are read before the review/check completion endpoints.
            records = [finding] if published else []
            inline_reads.append(len(records))
            return page(records)
        if path.endswith("/pulls/1/reviews"):
            published = True
        return original_pages(path, key)

    monkeypatch.setattr(service, "pages", pages)
    assert runner.run(cfg, root=tmp_path)["statuses"] == ["completed"]
    assert inline_reads == [0, 1]
    attempt = state(tmp_path)["attempts"][0]
    raw = json.loads(
        (tmp_path / "runs/test-run" / attempt["artifact_dir"] / "github.json").read_text()
    )
    assert github.items(raw, "review_comments") == [finding]
    assert service.elapsed == 2
    assert len(service.triggers) == 1


def test_nonblocking_recollection_returns_pending_when_completion_is_no_longer_verified(
    parallel, tmp_path, monkeypatch
):
    cfg, _, service = parallel
    attempt = {
        "id": "known",
        "repository": "test/se-known",
        "pr_number": 1,
        "head_sha": HEAD,
        "expected_base": BASE,
        "trigger_id": 1,
        "trigger_time": service.stamp(),
        "expires_at": service.stamp(30),
    }
    completed = raw_review()
    completed["checks"][0]["items"][0].update(
        started_at=service.stamp(), completed_at=service.stamp()
    )
    completed["reviews"][0]["items"][0]["submitted_at"] = service.stamp()
    observations = iter([completed, {**completed, "checks": page([])}])
    monkeypatch.setattr(github, "collect", lambda *args: next(observations))
    sleeps = []
    monkeypatch.setattr(github.time, "sleep", sleeps.append)
    result = github.collect_review(service, cfg, attempt, tmp_path, nonblocking=True)
    assert result["status"] == "read_pending"
    assert sleeps == [2]
    assert service.triggers == {}
