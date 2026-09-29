"""Sequential execution with explicit live gates and immutable evidence."""

from __future__ import annotations

import json
import os
import secrets
import signal
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .core import (
    ROOT,
    TERMINAL,
    EvalError,
    Experiment,
    atomic_json,
    code_identity,
    digest,
    frozen_manifest,
    lock,
    now,
    prepare_state,
    schedule,
    shared_runtime_identity,
    suite_contract,
    suite_module,
    verify_allowance,
)


def gateway_identity(config, root):
    if not any(arm.mcp for arm in config.arms):
        return None
    path = root / ".gateway/traces/gateway-config.json"
    if not path.is_file():
        raise EvalError("Missing effective gateway startup settings")
    data = json.loads(path.read_text())
    if data.get("version") != 1 or not isinstance(data.get("reader"), dict):
        raise EvalError("Invalid gateway startup settings")
    if data.get("runtime_sha256") != shared_runtime_identity(root):
        raise EvalError(
            "Gateway runtime is missing or differs from runner source/lock; rebuild gateway before execution"
        )
    return digest(path.read_bytes())


def verify_gateway_health(config, arm, root=ROOT):
    """Authenticated zero-provider liveness gate before new PR side effects."""
    if not arm.mcp:
        return
    import httpx

    gateway_identity(config, root)
    startup = json.loads((root / ".gateway/traces/gateway-config.json").read_text())
    with httpx.Client(timeout=5, follow_redirects=False, trust_env=False) as client:
        for name in arm.mcp:
            profile = config.profiles[name]
            token = os.getenv(profile.token_env, "")
            if not token:
                raise EvalError("Missing gateway health authentication")
            try:
                response = client.get(
                    config.public_mcp_url.rstrip("/") + "/mcp/" + profile.endpoint + "/health",
                    headers={"Authorization": "Bearer " + token},
                )
                if response.status_code != 200 or response.json() != {
                    "runtime_sha256": startup["runtime_sha256"],
                    "reader": startup["reader"],
                }:
                    raise ValueError()
            except (httpx.HTTPError, ValueError, KeyError):
                raise EvalError(
                    "Authenticated gateway health failed; no new review may start"
                ) from None


def verify_live_evidence_binding(evidence, root, artifact_paths, target_code):
    sources = evidence.get("source_runs")
    if not isinstance(sources, list) or not sources:
        raise EvalError("Acceptance needs preserved development source-run identity")
    for source in sources:
        relative = source.get("state_path")
        if relative not in artifact_paths:
            raise EvalError("Development source state must be a hashed acceptance artifact")
        raw = (root / relative).read_bytes()
        state = json.loads(raw)
        identity = state.get("input_identity", {})
        if (
            digest(raw) != source.get("state_sha256")
            or state.get("fingerprint") != source.get("run_fingerprint")
            or digest(identity) != state.get("fingerprint")
            or identity.get("code") != source.get("code_sha256")
            or identity.get("config") != state.get("config")
            or state.get("config", {}).get("role") != "development"
            or state.get("config", {}).get("suite") != evidence["suite"]
            or not any(a.get("status") == "completed" for a in state.get("attempts", []))
        ):
            raise EvalError("Development source-run identity or completed evidence mismatch")
        if identity["code"] == target_code:
            continue
        bridges = evidence.get("targeted_revalidation", [])
        bridge = next(
            (
                b
                for b in bridges
                if b.get("source_code_sha256") == identity["code"]
                and b.get("target_code_sha256") == target_code
            ),
            None,
        )
        if (
            not bridge
            or not bridge.get("changed_behavior")
            or not bridge.get("proof_artifacts")
            or not set(bridge["proof_artifacts"]) <= artifact_paths
        ):
            raise EvalError(
                "Older live runtime requires explicit targeted revalidation and hashed proofs"
            )


def acceptance_identity(config: Experiment, root=ROOT):
    """Live evidence binds inputs and behavior, excluding run ID, role and batch size."""
    options = {k: v for k, v in config.suite_options.items() if k != "acceptance_evidence"}
    from .effective_inputs import resolve_inputs

    effective = resolve_inputs(config, root)
    protocol = {
        "agent": effective["agent"],
        "judge": effective["judge"],
        "suite": config.suite,
        "arms": [a.model_dump() for a in config.arms],
        "profiles": {k: p.model_dump() for k, p in config.profiles.items()},
        "tool_caps": {
            "search": config.limits.max_search_calls,
            "fetch": config.limits.max_fetch_calls,
        },
        "timeouts": {
            "review": config.limits.review_timeout_seconds,
            "episode": config.limits.episode_timeout_seconds,
            "poll": config.limits.poll_seconds,
        },
        "suite_options": options,
        "gateway_config_sha256": gateway_identity(config, root),
    }
    return {
        "manifest_sha256": digest((root / config.manifest).read_bytes()),
        "protocol_sha256": digest(protocol),
        "code_sha256": code_identity(root),
    }


def verify_acceptance(config, root, path):
    evidence = json.loads(path.read_text())
    if (
        not isinstance(evidence, dict)
        or evidence.get("passed") is not True
        or evidence.get("suite") != config.suite
    ):
        raise EvalError("Unpassed or wrong-suite live acceptance")
    for key, value in acceptance_identity(config, root).items():
        if evidence.get(key) != value:
            raise EvalError(f"Live acceptance identity mismatch: {key}")
    artifacts = evidence.get("artifacts", [])
    if not isinstance(artifacts, list) or not artifacts:
        raise EvalError("Live acceptance has no evidence artifacts")
    for artifact in artifacts:
        if not isinstance(artifact, dict) or not isinstance(artifact.get("path"), str):
            raise EvalError("Invalid live acceptance artifact record")
        file = (root / artifact["path"]).resolve()
        if not file.is_relative_to(root.resolve()) or not file.is_file():
            raise EvalError("Live acceptance artifact is missing or outside workspace")
        if digest(file.read_bytes()) != artifact.get("sha256"):
            raise EvalError("Live acceptance artifact hash differs")
    verify_live_evidence_binding(
        evidence, root, {a["path"] for a in artifacts}, code_identity(root)
    )


def checks(config: Experiment, root=ROOT):
    problems = []
    from .effective_inputs import resolve_inputs

    try:
        resolve_inputs(config, root)
    except (ValueError, OSError, KeyError) as exc:
        problems.append(f"Effective inputs: {exc}")
    module = suite_module(config.suite)
    contract = suite_contract(config.suite)
    kind = contract["task_kind"]
    if not contract["live_allowed"]:
        problems.append("This suite is offline-only and cannot launch live execution")
    try:
        tasks = module.load_tasks(root / config.manifest)
        planned = schedule(config, tasks)
    except (OSError, KeyError, ValueError, EvalError) as exc:
        problems.append(f"Inputs: {exc}")
        tasks, planned = [], []
    services = {config.profiles[p].provider for a in config.arms for p in a.mcp}
    required = []
    if any(
        p.provider == "keenable" and p.tier == "authenticated"
        for name, p in config.profiles.items()
        if any(name in a.mcp for a in config.arms)
    ):
        required.append("KEENABLE_API_KEY")
    if "exa" in services:
        required.append("EXA_API_KEY")
    required += [p.token_env for p in config.profiles.values()] if kind == "pr_review" else []
    required += list(contract["required_env"])
    if kind == "pr_review":
        required.append("GITHUB_TOKEN")
    problems += [f"Missing environment variable: {key}" for key in required if not os.getenv(key)]
    if any(a.mcp for a in config.arms):
        base = urlsplit(config.public_mcp_url)
        allowed = base.scheme == "https" and bool(base.hostname)
        if kind == "developer_retrieval":
            allowed |= base.scheme == "http" and base.hostname in {
                "localhost",
                "127.0.0.1",
                "search-mcp",
            }
        if not allowed or base.username or base.password:
            problems.append(
                "Set HTTPS public_mcp_url, or a local HTTP gateway for developer retrieval"
            )
    allowance = None
    try:
        allowance = verify_allowance(config, root, len(planned))
    except (ValueError, EvalError, TypeError) as exc:
        problems.append(str(exc))
    if kind == "pr_review":
        completion = config.suite_options.get("completion_contract", {})
        completion_kind = completion.get("kind", "check_run")
        if completion_kind == "commit_status":
            valid = (
                type(completion.get("creator_id")) is int
                and completion["creator_id"] > 0
                and all(
                    isinstance(completion.get(field), str) and completion[field].strip()
                    for field in ("context", "creator_login", "success_description")
                )
            )
        elif completion_kind == "check_run":
            valid = (
                type(completion.get("app_id")) is int
                and completion["app_id"] > 0
                and isinstance(completion.get("check_name"), str)
                and bool(completion["check_name"].strip())
            )
        else:
            valid = False
        if not valid:
            problems.append("Missing or invalid observed SHA-bound CodeRabbit completion contract")
    if "martian_judge" in contract.get("allowance_services", ()):
        from .grading import judge_problems

        manifest = json.loads((root / config.manifest).read_text())
        problems.extend(judge_problems(config, manifest, allowance))
    try:
        gateway_identity(config, root)
    except (EvalError, ValueError, OSError) as exc:
        problems.append(str(exc))
    gates = config.suite_options.get("acceptance_evidence", [])
    if config.role != "development":
        if not gates:
            problems.append("Scored runs need saved suite-specific live acceptance evidence")
        for gate in gates:
            path = root / gate
            if not path.is_file():
                problems.append(f"Missing live acceptance evidence: {gate}")
                continue
            try:
                verify_acceptance(config, root, path)
            except (OSError, ValueError, KeyError, TypeError, EvalError) as exc:
                problems.append(f"Invalid live acceptance {gate}: {exc}")
    return {
        "suite": config.suite,
        "run_id": config.run_id,
        "checked_at": now(),
        "live_ready": not problems,
        "planned_attempts": len(planned),
        "blockers": problems,
        "network_calls": 0,
        "model_calls": 0,
        "provider_calls": 0,
    }, tasks


def routing_store(config, root):
    base = Path(os.getenv("SEARCH_EVAL_ROUTING", str(root / ".gateway/routing/attempts.json")))
    data = json.loads(base.read_text()) if base.exists() else {"profiles": {}, "attempts": {}}
    expected = {}
    for key, profile in config.profiles.items():
        token = os.getenv(profile.token_env, "")
        expected[key] = {
            "provider": profile.provider,
            "endpoint": profile.endpoint,
            "mode": profile.mode,
            "tier": profile.tier,
            "connection_token_sha256": digest(token.encode()) if token else "",
        }
    for key, value in expected.items():
        if key in data["profiles"] and data["profiles"][key] != value:
            raise EvalError("Gateway profile changed; refusing attribution drift")
        if any(
            k != key and p["endpoint"] == value["endpoint"] for k, p in data["profiles"].items()
        ):
            raise EvalError("Gateway endpoint is already assigned to another profile")
        data["profiles"][key] = value
    atomic_json(base, data)
    return base


def register_attempt(path, config, attempt, task, arm, token=None, root=ROOT):
    data = json.loads(path.read_text())
    run_dir = root / "runs" / config.run_id
    state = json.loads((run_dir / "state.json").read_text())
    manifest = frozen_manifest(run_dir, state)
    patterns = list(manifest.get("answer_patterns", []))
    patterns += [
        prefix + "*" for prefix in manifest.get("answer_source_rules", {}).get("url_prefixes", [])
    ]
    row = {
        "review_url": attempt.get("review_url"),
        "allowed_profiles": arm.mcp,
        "authorized": True,
        "status": "running",
        "configuration": arm.id,
        "expected_gateway_runtime_sha256": shared_runtime_identity(root),
        "expires_at": attempt["expires_at"],
        "max_search_calls": config.limits.max_search_calls,
        "max_fetch_calls": config.limits.max_fetch_calls,
        "answer_patterns": patterns,
    }
    if token:
        row.update({"profile": arm.mcp[0], "token_sha256": digest(token.encode())})
    old = data["attempts"].get(attempt["id"])
    if old and old != row:
        raise EvalError("Attempt routing mapping changed; refusing attribution drift")
    data["attempts"][attempt["id"]] = row
    atomic_json(path, data)


def finish_route(path, attempt):
    data = json.loads(path.read_text())
    if attempt["id"] in data["attempts"]:
        data["attempts"][attempt["id"]]["status"] = attempt["status"]
        data["attempts"][attempt["id"]]["authorized"] = False
        atomic_json(path, data)


def execute_devdex(config, task, arm, attempt, artifact, route, save, root=ROOT):
    # Never launch again after an uncertain external call. Recover terminal output instead.
    terminal_path = artifact / "terminal.json"
    if terminal_path.exists():
        terminal = json.loads(terminal_path.read_text())
        if terminal["attempt_id"] != attempt["id"]:
            raise EvalError("Worker terminal identity mismatch")
        return terminal
    if attempt.get("worker_intent"):
        raise EvalError("DevDex worker outcome is unknown; reconcile before any new model call")
    token = secrets.token_urlsafe(32)
    register_attempt(route, config, attempt, task, arm, token, root)
    atomic_json(artifact / "visible-task.json", task)
    from .effective_inputs import resolve_agent

    agent = resolve_agent(config.suite_options, root)
    saved_state = json.loads((root / "runs" / config.run_id / "state.json").read_text())
    if agent != saved_state["input_identity"]["effective_inputs"]["agent"]:
        raise EvalError("Agent protocol changed since run preparation")
    worker_config = {
        "attempt_id": attempt["id"],
        "task_id": task["id"],
        "configuration": arm.id,
        "repeat": attempt["repeat"],
        "agent": agent,
        "agent_access_verified": True,
        "allowance_verified": True,
    }
    atomic_json(artifact / "worker-config.json", worker_config)
    attempt["worker_intent"] = now()
    save()
    env = dict(
        {
            k: v
            for k, v in os.environ.items()
            if k
            in {
                "PATH",
                "HOME",
                "TMPDIR",
                "TEMP",
                "TMP",
                "LANG",
                "LC_ALL",
                "SYSTEMROOT",
                "ANTHROPIC_API_KEY",
            }
        },
        SEARCH_EVAL_MCP_URL=config.public_mcp_url.rstrip("/") + "/mcp/devdex",
        SEARCH_EVAL_MCP_TOKEN=token,
    )
    start = time.monotonic()
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "search_eval.devdex_worker",
            "--task",
            str(artifact / "visible-task.json"),
            "--config",
            str(artifact / "worker-config.json"),
            "--output",
            str(artifact),
        ],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=config.limits.episode_timeout_seconds)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.communicate()
        return {
            "status": "unknown",
            "reason": "Worker timed out; remote model execution may be unresolved",
        }
    # Raw SDK stderr can contain endpoint auth. Preserve only sanitized diagnostic text.
    diagnostic = stderr
    for key, value in env.items():
        if value and any(word in key for word in ("KEY", "TOKEN", "SECRET")):
            diagnostic = diagnostic.replace(value, "[REDACTED]")
    (artifact / "worker.stderr.txt").write_text(diagnostic)
    if not terminal_path.exists():
        return {
            "status": "unknown",
            "reason": f"Worker exit {process.returncode} without terminal evidence",
        }
    terminal = json.loads(terminal_path.read_text())
    if terminal.get("attempt_id") != attempt["id"]:
        raise EvalError("Worker returned another attempt")
    terminal["duration_seconds"] = time.monotonic() - start
    return terminal


def devdex_startup_failure(artifact: Path, attempt: dict) -> bool:
    """Recognize observed SDK startup failures, not ordinary model/task failures."""
    if attempt.get("status") != "failed":
        return False
    path = artifact / "record.json"
    if not path.is_file():
        return False
    record = json.loads(path.read_text())
    if record.get("attempt_id") != attempt["id"]:
        raise EvalError("Worker record identity mismatch")
    if record.get("actual_models") != [] or record.get("observed_usage") != []:
        return False
    error = str(record.get("error", "")).lower()
    stderr_path = artifact / "worker.stderr.txt"
    stderr = stderr_path.read_text().lower() if stderr_path.exists() else ""
    return bool(error) and (
        "command failed with exit code" in error
        or "failed to start" in error
        or "cli not found" in error
        or "--dangerously-skip-permissions cannot be used" in stderr
    )


def stop_on_startup_failure(state: dict, run_dir: Path) -> None:
    """Persist a circuit breaker, including recovery after terminal-save crashes."""
    if not state.get("circuit_breaker"):
        for attempt in state["attempts"]:
            if devdex_startup_failure(run_dir / attempt["artifact_dir"], attempt):
                state["circuit_breaker"] = {
                    "reason": "devdex_sdk_startup_failure",
                    "attempt_id": attempt["id"],
                    "record": str(Path(attempt["artifact_dir"]) / "record.json"),
                    "created_at": now(),
                }
                atomic_json(run_dir / "state.json", state)
                break
    if state.get("circuit_breaker"):
        raise EvalError(
            "Batch stopped after SDK startup failure before observed model output. "
            "Repair infrastructure and use a new run_id; this run will not retry automatically"
        )


def run(config: Experiment, *, root=ROOT, plan_only=False):
    run_dir = root / "runs" / config.run_id
    with lock(root / ".gateway/runner.lock"):
        # Revoke known terminal/unknown routes even if fresh allowance preflight fails.
        existing_state = run_dir / "state.json"
        existing_route = Path(
            os.getenv("SEARCH_EVAL_ROUTING", str(root / ".gateway/routing/attempts.json"))
        )
        if not plan_only and existing_state.exists() and existing_route.exists():
            saved = json.loads(existing_state.read_text())
            for row in saved["attempts"]:
                if row["status"] in TERMINAL | {"unknown"}:
                    finish_route(existing_route, row)
        preflight, tasks = checks(config, root)
        if not plan_only and not preflight["live_ready"]:
            atomic_json(root / "validation" / f"doctor-{config.run_id}.json", preflight)
            raise EvalError("Live preflight failed:\n" + "\n".join(preflight["blockers"]))
        state = prepare_state(config, tasks, run_dir, root)
        if plan_only:
            return {"planned": len(state["attempts"]), "live_calls": 0, "run_dir": str(run_dir)}
        if any(a["status"] == "unknown" for a in state["attempts"]):
            raise EvalError("Reconcile unknown attempts before any new execution")
        if any(
            a["status"] not in TERMINAL | {"pending", "running", "read_pending"}
            for a in state["attempts"]
        ):
            raise EvalError("Unknown saved attempt status")
        kind = suite_contract(config.suite)["task_kind"]
        if kind == "developer_retrieval":
            stop_on_startup_failure(state, run_dir)
        route = routing_store(config, root)
        by_task = {task["id"]: task for task in tasks}
        by_arm = {arm.id: arm for arm in config.arms}

        def save():
            atomic_json(run_dir / "state.json", state)

        for attempt in state["attempts"]:
            if attempt["status"] in {"completed", "failed", "skipped", "timed_out"}:
                finish_route(route, attempt)
                continue
            if attempt["status"] == "unknown":
                raise EvalError(f"Reconcile unknown attempt {attempt['id']} before continuing")
            allowance = verify_allowance(config, root, len(state["attempts"]))
            task, arm = by_task[attempt["task_id"]], by_arm[attempt["configuration"]]
            from .github import GitHubReadError, confirmed_trigger

            recovering_review = kind == "pr_review" and confirmed_trigger(attempt)
            if kind == "pr_review" and not recovering_review:
                verify_gateway_health(config, arm, root=root)
            attempt["allowance_valid_until"] = attempt.get(
                "allowance_valid_until", allowance["valid_until"]
            )
            if kind == "developer_retrieval" and "expires_at" not in attempt:
                horizon = (
                    config.limits.episode_timeout_seconds
                    if kind == "developer_retrieval"
                    else config.limits.review_timeout_seconds
                )
                limit = min(
                    datetime.fromisoformat(allowance["valid_until"]),
                    datetime.now(timezone.utc) + timedelta(seconds=horizon),
                )
                attempt["expires_at"] = limit.isoformat()
            if (
                not recovering_review
                and "expires_at" in attempt
                and datetime.fromisoformat(attempt["expires_at"]) <= datetime.now(timezone.utc)
            ):
                attempt.update({"status": "unknown", "reason": "Original attempt deadline expired"})
                save()
                finish_route(route, attempt)
                raise EvalError("Original attempt deadline expired; reconcile before continuing")
            artifact = run_dir / attempt["artifact_dir"]
            artifact.mkdir(parents=True, exist_ok=True)
            attempt.update({"status": "running", "started_at": attempt.get("started_at", now())})
            save()
            try:
                if kind == "developer_retrieval":
                    result = execute_devdex(config, task, arm, attempt, artifact, route, save, root)
                else:
                    from .github import execute

                    result = execute(
                        config,
                        task,
                        arm,
                        attempt,
                        artifact,
                        save,
                        lambda a: register_attempt(route, config, a, task, arm, root=root),
                    )
                attempt.update(result)
                if attempt["status"] == "completed":
                    attempt.pop("reason", None)
                attempt["updated_at"] = now()
                save()
                finish_route(route, attempt)
                if kind == "developer_retrieval":
                    stop_on_startup_failure(state, run_dir)
                if attempt["status"] == "unknown":
                    raise EvalError(attempt["reason"])
            except Exception as exc:
                if (
                    isinstance(exc, GitHubReadError)
                    and kind == "pr_review"
                    and confirmed_trigger(attempt)
                ):
                    attempt.update(
                        status="read_pending",
                        reason="Transient GitHub read failed; resume reads only",
                        updated_at=now(),
                    )
                    save()
                    # Existing authorized review may still use its route until the original expiry.
                    raise
                if attempt["status"] not in TERMINAL:
                    attempt.update(
                        {"status": "unknown", "reason": type(exc).__name__, "updated_at": now()}
                    )
                save()
                finish_route(route, attempt)
                raise
        return {"run_dir": str(run_dir), "statuses": [a["status"] for a in state["attempts"]]}


def reconcile_reviews(run_dir: Path, *, root=ROOT):
    """Explicit read-only recovery even after inference allowance expires.

    Only confirmed trigger receipts qualify. Unknown/unconfirmed writes are left
    unchanged; this path cannot issue a review or extend its original deadline.
    """
    from .core import verify_run_code
    from .github import (
        GitHub,
        GitHubReadError,
        collect_review,
        confirmed_trigger,
        token_from_environment,
    )

    with lock(root / ".gateway/runner.lock"):
        state = json.loads((run_dir / "state.json").read_text())
        config = Experiment.model_validate(state["config"])
        frozen_manifest(run_dir, state)
        verify_run_code(state, root)
        if suite_contract(config.suite)["task_kind"] != "pr_review":
            raise EvalError("Read-only review reconciliation requires a PR review suite")
        api = GitHub(token_from_environment())
        route = Path(os.getenv("SEARCH_EVAL_ROUTING", str(root / ".gateway/routing/attempts.json")))
        recovered = []
        for attempt in state["attempts"]:
            if attempt["status"] not in {"running", "read_pending"} or not confirmed_trigger(
                attempt
            ):
                continue
            artifact = run_dir / attempt["artifact_dir"]
            artifact.mkdir(parents=True, exist_ok=True)
            try:
                result = collect_review(api, config, attempt, artifact, final_only=True)
            except GitHubReadError:
                result = {
                    "status": "read_pending",
                    "reason": "Transient GitHub read failed; resume reads only",
                }
            attempt.update(result, updated_at=now())
            if attempt["status"] == "completed":
                attempt.pop("reason", None)
            atomic_json(run_dir / "state.json", state)
            if attempt["status"] in TERMINAL and route.exists():
                finish_route(route, attempt)
            recovered.append({"attempt_id": attempt["id"], "status": attempt["status"]})
        return {"read_only": True, "review_triggers": 0, "attempts": recovered}
