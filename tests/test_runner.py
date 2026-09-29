"""Deterministic execution gates. No external APIs are used."""

import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest
from test_core import config

from search_eval import core, github, runner
from search_eval.core import EvalError, atomic_json, digest, prepare_state


@pytest.fixture
def rig(tmp_path, monkeypatch):
    module = types.ModuleType("second_pr_suite")
    module.TASK_KIND = "pr_review"
    module.LIVE_ALLOWED = True
    module.REQUIRED_ENV = ()
    module.ALLOWANCE_SERVICES = ()
    tasks = [dict(id=str(i), role="development", kind="pr_review", input={}) for i in range(2)]
    module.load_tasks = lambda path: tasks
    monkeypatch.setitem(sys.modules, module.__name__, module)
    monkeypatch.setitem(core.SUITES, "second_pr", module.__name__)
    monkeypatch.delenv("SEARCH_EVAL_ROUTING", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "fixture-only")
    cfg = config(
        suite="second_pr",
        role="development",
        profiles={},
        arms=[dict(id="native", native_search=True)],
        allowance_file="allowance.json",
        suite_options={"completion_contract": {"app_id": 1, "check_name": "review"}},
    )
    atomic_json(tmp_path / "data.json", {})
    protocol = core.ROOT / "data/devdex-docs-protocol.json"
    atomic_json(tmp_path / "data/devdex-docs-protocol.json", json.loads(protocol.read_text()))
    allowance = dict(
        verified=True,
        suite=cfg.suite,
        run_id=cfg.run_id,
        role=cfg.role,
        evidence="fixture",
        max_attempts=100,
        additional_spending_usd=0,
        automatic_overage_disabled=True,
        services=["coderabbit"],
        valid_until=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
    )
    atomic_json(tmp_path / "allowance.json", allowance)
    calls = []

    def execute(config, task, arm, attempt, artifact, save, register):
        calls.append(attempt["id"])
        attempt["review_url"] = "https://github.com/example/private/pull/1"
        attempt["expires_at"] = attempt["allowance_valid_until"]
        save()
        register(attempt)
        return {"status": "completed"}

    monkeypatch.setattr(github, "execute", execute)
    return cfg, tasks, calls, allowance


def test_second_pr_suite_uses_github_and_completed_resume_never_reexecutes(rig, tmp_path):
    cfg, _, calls, _ = rig
    assert runner.run(cfg, root=tmp_path)["statuses"] == ["completed", "completed"]
    assert len(calls) == 2
    runner.run(cfg, root=tmp_path)
    assert len(calls) == 2
    route = tmp_path / ".gateway/routing/attempts.json"
    rows = json.loads(route.read_text())["attempts"].values()
    assert all(not row["authorized"] and row["status"] == "completed" for row in rows)
    assert all(row["max_search_calls"] == cfg.limits.max_search_calls for row in rows)


def test_unknown_anywhere_blocks_all_pending(rig, tmp_path):
    cfg, tasks, calls, _ = rig
    state = prepare_state(cfg, tasks, tmp_path / "runs/test-run", tmp_path)
    state["attempts"][-1]["status"] = "unknown"
    atomic_json(tmp_path / "runs/test-run/state.json", state)
    with pytest.raises(EvalError, match="Reconcile unknown"):
        runner.run(cfg, root=tmp_path)
    assert calls == []


def test_crash_stale_terminal_route_revoked_even_when_allowance_expired(rig, tmp_path):
    cfg, _, calls, allowance = rig
    runner.run(cfg, root=tmp_path)
    path = tmp_path / ".gateway/routing/attempts.json"
    route = json.loads(path.read_text())
    for row in route["attempts"].values():
        row.update(status="running", authorized=True)
    atomic_json(path, route)
    atomic_json(
        tmp_path / "allowance.json", {**allowance, "valid_until": "2020-01-01T00:00:00+00:00"}
    )
    with pytest.raises(EvalError, match="preflight"):
        runner.run(cfg, root=tmp_path)
    assert len(calls) == 2
    assert all(not row["authorized"] for row in json.loads(path.read_text())["attempts"].values())


def test_allowance_rechecked_before_every_attempt(rig, tmp_path, monkeypatch):
    cfg, _, calls, allowance = rig
    original = github.execute

    def expire(*args):
        result = original(*args)
        atomic_json(
            tmp_path / "allowance.json", {**allowance, "valid_until": "2020-01-01T00:00:00+00:00"}
        )
        return result

    monkeypatch.setattr(github, "execute", expire)
    with pytest.raises(EvalError):
        runner.run(cfg, root=tmp_path)
    assert len(calls) == 1
    state = json.loads((tmp_path / "runs/test-run/state.json").read_text())
    assert [a["status"] for a in state["attempts"]] == ["completed", "pending"]


def test_resume_never_extends_expired_attempt(rig, tmp_path):
    cfg, tasks, calls, _ = rig
    state = prepare_state(cfg, tasks, tmp_path / "runs/test-run", tmp_path)
    state["attempts"][0].update(status="running", expires_at="2020-01-01T00:00:00+00:00")
    atomic_json(tmp_path / "runs/test-run/state.json", state)
    with pytest.raises(EvalError, match="deadline expired"):
        runner.run(cfg, root=tmp_path)
    assert not calls
    saved = json.loads((tmp_path / "runs/test-run/state.json").read_text())
    assert saved["attempts"][0]["status"] == "unknown"
    assert saved["attempts"][0]["expires_at"] == "2020-01-01T00:00:00+00:00"


def test_acceptance_requires_identity_and_artifact_integrity(rig, tmp_path, monkeypatch):
    cfg, _, _, _ = rig
    path = tmp_path / "acceptance.json"
    atomic_json(path, {"passed": True, "suite": cfg.suite})
    with pytest.raises(EvalError, match="identity"):
        runner.verify_acceptance(cfg, tmp_path, path)
    artifact = tmp_path / "trace.json"
    artifact.write_text("fixture trace")
    record = dict(
        passed=True,
        suite=cfg.suite,
        **runner.acceptance_identity(cfg, tmp_path),
        artifacts=[{"path": "trace.json", "sha256": digest(artifact.read_bytes())}],
    )
    state = prepare_state(cfg, rig[1], tmp_path / "source", tmp_path)
    state["attempts"][0]["status"] = "completed"
    atomic_json(tmp_path / "source/state.json", state)
    state_hash = digest((tmp_path / "source/state.json").read_bytes())
    record["artifacts"].append({"path": "source/state.json", "sha256": state_hash})
    record["source_runs"] = [
        {
            "state_path": "source/state.json",
            "state_sha256": state_hash,
            "run_fingerprint": state["fingerprint"],
            "code_sha256": state["input_identity"]["code"],
        }
    ]
    atomic_json(path, record)
    runner.verify_acceptance(cfg, tmp_path, path)
    changed = cfg.model_copy(
        update={
            "run_id": "pilot",
            "role": "scored",
            "repeats": 2,
            "limits": cfg.limits.model_copy(update={"max_attempts": 50}),
        }
    )
    assert runner.acceptance_identity(changed, tmp_path) == runner.acceptance_identity(
        cfg, tmp_path
    )
    modified = cfg.model_copy(
        update={"limits": cfg.limits.model_copy(update={"max_search_calls": 2})}
    )
    with pytest.raises(EvalError, match="protocol"):
        runner.verify_acceptance(modified, tmp_path, path)
    artifact.write_text("tampered")
    with pytest.raises(EvalError, match="artifact hash"):
        runner.verify_acceptance(cfg, tmp_path, path)


def test_offline_suite_cannot_launch_and_kind_must_match(rig, tmp_path):
    cfg, tasks, calls, _ = rig
    module = core.suite_module(cfg.suite)
    module.LIVE_ALLOWED = False
    with pytest.raises(EvalError, match="offline-only"):
        runner.run(cfg, root=tmp_path)
    assert not calls
    tasks[0]["kind"] = "developer_retrieval"
    with pytest.raises(EvalError, match="kind"):
        core.schedule(cfg, tasks)


def test_gateway_subsets_reuse_profiles_and_fingerprint_actual_startup(tmp_path, monkeypatch):
    monkeypatch.delenv("SEARCH_EVAL_ROUTING", raising=False)
    cfg = config()
    route = runner.routing_store(cfg, tmp_path)
    subset = cfg.model_copy(update={"profiles": {"keenable": cfg.profiles["keenable"]}})
    assert runner.routing_store(subset, tmp_path) == route
    assert set(json.loads(route.read_text())["profiles"]) == {"keenable", "exa"}
    with pytest.raises(EvalError, match="startup"):
        runner.gateway_identity(cfg, tmp_path)
    settings = tmp_path / ".gateway/traces/gateway-config.json"
    atomic_json(
        settings,
        {
            "version": 1,
            "runtime_sha256": core.shared_runtime_identity(tmp_path),
            "reader": {"max_bytes": 1000},
        },
    )
    first = runner.gateway_identity(cfg, tmp_path)
    monkeypatch.setenv("SEARCH_EVAL_READER_MAX_BYTES", "12")
    assert runner.gateway_identity(cfg, tmp_path) == first
    atomic_json(
        settings,
        {
            "version": 1,
            "runtime_sha256": core.shared_runtime_identity(tmp_path),
            "reader": {"max_bytes": 2000},
        },
    )
    assert runner.gateway_identity(cfg, tmp_path) != first


def test_worker_timeout_kills_process_group_and_whitelists_env(rig, tmp_path, monkeypatch):
    cfg, tasks, _, _ = rig
    core.suite_module(cfg.suite).TASK_KIND = "developer_retrieval"
    for task in tasks:
        task["kind"] = "developer_retrieval"
    prepare_state(cfg, tasks, tmp_path / "runs/test-run", tmp_path)
    monkeypatch.setattr(runner, "register_attempt", lambda *a: None)
    monkeypatch.setenv("MARTIAN_API_KEY", "judge-secret")
    monkeypatch.setenv("EXA_API_KEY", "search-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "model-secret")
    seen = {}

    class Process:
        pid = 1234

        def communicate(self, timeout=None):
            if timeout:
                raise runner.subprocess.TimeoutExpired("worker", timeout)
            return "", ""

    def popen(command, **kwargs):
        seen.update(kwargs)
        return Process()

    monkeypatch.setattr(runner.subprocess, "Popen", popen)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, signal: seen.update(killed=pid))
    attempt = {"id": "a", "repeat": 1}
    out = tmp_path / "artifact"
    out.mkdir()
    result = runner.execute_devdex(
        cfg, tasks[0], cfg.arms[0], attempt, out, None, lambda: None, tmp_path
    )
    assert result["status"] == "unknown"
    assert seen["killed"] == 1234 and seen["start_new_session"] is True
    assert seen["env"]["ANTHROPIC_API_KEY"] == "model-secret"
    assert not {"GITHUB_TOKEN", "EXA_API_KEY", "MARTIAN_API_KEY"} & seen["env"].keys()


def test_judge_preflight_checks_all_required_settings_before_execution(rig, tmp_path, monkeypatch):
    cfg, _, calls, _ = rig
    module = core.suite_module(cfg.suite)
    module.ALLOWANCE_SERVICES = ("martian_judge",)
    monkeypatch.delenv("MARTIAN_BASE_URL", raising=False)
    result, _ = runner.checks(cfg, tmp_path)
    text = " ".join(result["blockers"])
    assert "Historical judge" in text
    assert "max_judge_calls_per_attempt" in text
    assert "MARTIAN_BASE_URL" in text
    with pytest.raises(EvalError, match="preflight"):
        runner.run(cfg, root=tmp_path)
    assert not calls


def test_register_reads_frozen_blocklist_not_mutable_manifest(rig, tmp_path):
    cfg, tasks, _, _ = rig
    atomic_json(
        tmp_path / "data.json",
        {"answer_source_rules": {"url_prefixes": ["https://github.com/source/pull/1"]}},
    )
    state = prepare_state(cfg, tasks, tmp_path / "runs/test-run", tmp_path)
    atomic_json(tmp_path / "data.json", {})
    path = runner.routing_store(cfg, tmp_path)
    attempt = state["attempts"][0]
    attempt["expires_at"] = "2099-01-01T00:00:00+00:00"
    runner.register_attempt(path, cfg, attempt, tasks[0], cfg.arms[0], root=tmp_path)
    assert json.loads(path.read_text())["attempts"][attempt["id"]]["answer_patterns"] == [
        "https://github.com/source/pull/1*"
    ]


@pytest.mark.parametrize(
    "url,valid",
    [
        ("http://search-mcp:8765", True),
        ("http://localhost:8765", True),
        ("http://127.0.0.1:8765", True),
        ("http://example.com", False),
        ("http://search-mcp.evil.test", False),
    ],
)
def test_retrieval_gateway_allows_only_explicit_internal_http(rig, tmp_path, url, valid):
    cfg, _, _, _ = rig
    module = core.suite_module(cfg.suite)
    module.TASK_KIND = "developer_retrieval"
    cfg = cfg.model_copy(
        update={
            "arms": [core.Arm(id="keenable", mcp=["keenable"])],
            "profiles": config().profiles,
            "public_mcp_url": url,
        }
    )
    result, _ = runner.checks(cfg, tmp_path)
    assert any("local HTTP" in issue for issue in result["blockers"]) is not valid
    module.TASK_KIND = "pr_review"
    result, _ = runner.checks(cfg, tmp_path)
    assert any("local HTTP" in issue for issue in result["blockers"])


def test_devdex_startup_failure_stops_batch_and_resume_without_replay(rig, tmp_path, monkeypatch):
    cfg, tasks, calls, _ = rig
    core.suite_module(cfg.suite).TASK_KIND = "developer_retrieval"
    for task in tasks:
        task["kind"] = "developer_retrieval"
    monkeypatch.setattr(runner, "checks", lambda *args: ({"live_ready": True}, tasks))
    monkeypatch.setattr(
        runner, "verify_allowance", lambda *args: {"valid_until": "2099-01-01T00:00:00+00:00"}
    )

    def execute(config, task, arm, attempt, artifact, route, save, root):
        calls.append(attempt["id"])
        atomic_json(
            artifact / "record.json",
            {
                "attempt_id": attempt["id"],
                "actual_models": [],
                "observed_usage": [],
                "error": "Command failed with exit code 1",
            },
        )
        (artifact / "worker.stderr.txt").write_text(
            "--dangerously-skip-permissions cannot be used with root/sudo privileges"
        )
        return {"status": "failed"}

    monkeypatch.setattr(runner, "execute_devdex", execute)
    for _ in range(2):
        with pytest.raises(EvalError, match="Batch stopped"):
            runner.run(cfg, root=tmp_path)
    assert len(calls) == 1
    state_path = tmp_path / "runs/test-run/state.json"
    state = json.loads(state_path.read_text())
    assert [a["status"] for a in state["attempts"]] == ["failed", "pending"]
    assert state["circuit_breaker"]["attempt_id"] == calls[0]
    # A crash between saving terminal evidence and the breaker cannot bypass the stop.
    del state["circuit_breaker"]
    atomic_json(state_path, state)
    with pytest.raises(EvalError, match="Batch stopped"):
        runner.run(cfg, root=tmp_path)
    assert len(calls) == 1


@pytest.mark.parametrize(
    "models,usage,error,expected",
    [
        ([], [], "Command failed with exit code 1", True),
        (["model"], [], "Command failed with exit code 1", False),
        ([], [{"input_tokens": 1}], "Command failed with exit code 1", False),
        ([], [], "Answer schema invalid", False),
        ([], [], "", False),
    ],
)
def test_startup_breaker_requires_sdk_error_and_no_observed_inference(
    tmp_path, models, usage, error, expected
):
    atomic_json(
        tmp_path / "record.json",
        {"attempt_id": "a", "actual_models": models, "observed_usage": usage, "error": error},
    )
    assert runner.devdex_startup_failure(tmp_path, {"id": "a", "status": "failed"}) is expected


@pytest.mark.parametrize(
    "contract,valid",
    [
        (
            {
                "kind": "commit_status",
                "context": "CodeRabbit",
                "creator_id": 136622811,
                "creator_login": "coderabbitai[bot]",
                "success_description": "Observed exact completion",
            },
            True,
        ),
        ({"app_id": 1, "check_name": "review"}, True),
        ({"kind": "check_run", "app_id": 1, "check_name": "review"}, True),
        ({"kind": "unknown", "app_id": 1, "check_name": "review"}, False),
        (
            {
                "kind": "commit_status",
                "context": "CodeRabbit",
                "creator_id": 136622811,
                "creator_login": "coderabbitai[bot]",
            },
            False,
        ),
        (
            {
                "kind": "commit_status",
                "context": "CodeRabbit",
                "creator_id": True,
                "creator_login": "coderabbitai[bot]",
                "success_description": "done",
            },
            False,
        ),
        (
            {
                "kind": "commit_status",
                "context": "CodeRabbit",
                "creator_id": 0,
                "creator_login": "coderabbitai[bot]",
                "success_description": "done",
            },
            False,
        ),
        (
            {
                "kind": "commit_status",
                "context": " ",
                "creator_id": 1,
                "creator_login": "coderabbitai[bot]",
                "success_description": "done",
            },
            False,
        ),
        (
            {
                "kind": "commit_status",
                "context": "CodeRabbit",
                "creator_id": 1,
                "creator_login": " ",
                "success_description": "done",
            },
            False,
        ),
        (
            {
                "kind": "commit_status",
                "context": "CodeRabbit",
                "creator_id": 1,
                "creator_login": "coderabbitai[bot]",
                "success_description": " ",
            },
            False,
        ),
    ],
)
def test_pr_completion_contract_preflight(rig, tmp_path, contract, valid):
    cfg, _, calls, _ = rig
    cfg = cfg.model_copy(update={"suite_options": {"completion_contract": contract}})
    result, _ = runner.checks(cfg, tmp_path)
    assert any("completion contract" in issue for issue in result["blockers"]) is not valid
    assert calls == []


@pytest.mark.parametrize("change", ["missing", "source", "lock"])
def test_gateway_identity_rejects_missing_or_drifted_image(tmp_path, change):
    cfg = config()
    (tmp_path / "src").mkdir()
    (tmp_path / "src/gateway.py").write_text("version=1")
    (tmp_path / "uv.lock").write_text("version=1")
    record = {"version": 1, "reader": {}, "runtime_sha256": core.shared_runtime_identity(tmp_path)}
    if change == "missing":
        del record["runtime_sha256"]
    elif change == "source":
        (tmp_path / "src/gateway.py").write_text("version=2")
    else:
        (tmp_path / "uv.lock").write_text("version=2")
    atomic_json(tmp_path / ".gateway/traces/gateway-config.json", record)
    with pytest.raises(EvalError, match="Gateway runtime"):
        runner.gateway_identity(cfg, tmp_path)


def test_shared_image_identity_does_not_need_vendor_or_gold(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src/gateway.py").write_text("version=1")
    (tmp_path / "uv.lock").write_text("version=1")
    identity = core.shared_runtime_identity(tmp_path)
    atomic_json(tmp_path / "vendor/gold.json", {"secret": "not in gateway"})
    assert core.shared_runtime_identity(tmp_path) == identity


def test_older_live_acceptance_needs_honest_revalidation_bridge(rig, tmp_path):
    cfg, tasks, _, _ = rig
    state = prepare_state(cfg, tasks, tmp_path / "source", tmp_path)
    state["attempts"][0]["status"] = "completed"
    atomic_json(tmp_path / "source/state.json", state)
    source = {
        "state_path": "source/state.json",
        "state_sha256": digest((tmp_path / "source/state.json").read_bytes()),
        "run_fingerprint": state["fingerprint"],
        "code_sha256": state["input_identity"]["code"],
    }
    record = {"suite": cfg.suite, "source_runs": [source]}
    with pytest.raises(EvalError, match="Older live runtime"):
        runner.verify_live_evidence_binding(record, tmp_path, {"source/state.json"}, "new-runtime")
    record["targeted_revalidation"] = [
        {
            "source_code_sha256": source["code_sha256"],
            "target_code_sha256": "new-runtime",
            "changed_behavior": ["HTTP endpoint is explicit; mocked transport parity verified"],
            "proof_artifacts": ["proof.json"],
        }
    ]
    runner.verify_live_evidence_binding(
        record, tmp_path, {"source/state.json", "proof.json"}, "new-runtime"
    )
    source["code_sha256"] = "new-runtime"
    with pytest.raises(EvalError, match="source-run identity"):
        runner.verify_live_evidence_binding(
            record, tmp_path, {"source/state.json", "proof.json"}, "new-runtime"
        )


def test_gateway_health_is_authenticated_and_checks_running_identity(tmp_path, monkeypatch):
    import httpx

    cfg = config(public_mcp_url="https://tunnel.example")
    monkeypatch.setenv("A", "private-fixture")
    startup = {
        "version": 1,
        "runtime_sha256": core.shared_runtime_identity(tmp_path),
        "reader": {"max_bytes": 1000},
    }
    atomic_json(tmp_path / ".gateway/traces/gateway-config.json", startup)
    original = httpx.Client
    observed = []

    def handle(request):
        observed.append(request)
        assert request.headers["authorization"] == "Bearer private-fixture"
        return httpx.Response(
            200, json={"runtime_sha256": startup["runtime_sha256"], "reader": startup["reader"]}
        )

    monkeypatch.setattr(
        httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handle), **kwargs)
    )
    runner.verify_gateway_health(cfg, next(a for a in cfg.arms if a.id == "keenable"), tmp_path)
    assert (
        len(observed) == 1 and str(observed[0].url) == "https://tunnel.example/mcp/search-a/health"
    )
    startup["runtime_sha256"] = "old-image"
    with pytest.raises(EvalError, match="health failed"):
        runner.verify_gateway_health(cfg, next(a for a in cfg.arms if a.id == "keenable"), tmp_path)


def test_confirmed_read_failure_is_resumable_without_new_trigger(rig, tmp_path, monkeypatch):
    cfg, _, _, _ = rig
    triggers = []
    health = []
    fail_once = True
    monkeypatch.setattr(
        runner, "verify_gateway_health", lambda *args, **kwargs: health.append(True)
    )

    def execute(config, task, arm, attempt, artifact, save, register):
        nonlocal fail_once
        if not github.confirmed_trigger(attempt):
            triggers.append(attempt["id"])
            attempt.update(
                repository="test/repo",
                pr_number=1,
                head_sha="a" * 40,
                expected_base="b" * 40,
                trigger_id=1,
                trigger_time=datetime.now(timezone.utc).isoformat(),
                expires_at=attempt["allowance_valid_until"],
                review_url="https://github.com/test/repo/pull/1",
            )
            save()
            register(attempt)
        if fail_once:
            fail_once = False
            raise github.GitHubReadError("temporary GET timeout")
        return {"status": "completed"}

    monkeypatch.setattr(github, "execute", execute)
    with pytest.raises(github.GitHubReadError):
        runner.run(cfg, root=tmp_path)
    state = json.loads((tmp_path / "runs/test-run/state.json").read_text())
    assert state["attempts"][0]["status"] == "read_pending"
    assert runner.run(cfg, root=tmp_path)["statuses"] == ["completed", "completed"]
    assert len(triggers) == 2 and len(health) == 2


def test_readonly_reconciliation_ignores_expired_model_allowance(rig, tmp_path, monkeypatch):
    cfg, tasks, _, allowance = rig
    run_dir = tmp_path / "runs/test-run"
    state = prepare_state(cfg, tasks, run_dir, tmp_path)
    state["attempts"][0].update(
        status="read_pending",
        repository="test/repo",
        pr_number=1,
        head_sha="a" * 40,
        expected_base="b" * 40,
        trigger_id=1,
        trigger_time="2026-09-29T10:00:00Z",
        expires_at="2026-09-29T10:01:00+00:00",
    )
    state["attempts"][1].update(status="unknown", trigger_intent="2026-09-29T10:00:00Z")
    atomic_json(run_dir / "state.json", state)
    atomic_json(
        tmp_path / "allowance.json", {**allowance, "valid_until": "2020-01-01T00:00:00+00:00"}
    )
    seen = []

    def collect(api, config, attempt, artifact, *, final_only):
        assert final_only
        seen.append(attempt["id"])
        return {"status": "completed", "completed_at": "2026-09-29T10:00:50+00:00"}

    monkeypatch.setattr(github, "collect_review", collect)
    result = runner.reconcile_reviews(run_dir, root=tmp_path)
    assert result["read_only"] and result["review_triggers"] == 0 and len(seen) == 1
    saved = json.loads((run_dir / "state.json").read_text())
    assert saved["attempts"][0]["status"] == "completed"
    assert saved["attempts"][0]["expires_at"] == state["attempts"][0]["expires_at"]
    assert saved["attempts"][1] == state["attempts"][1]
