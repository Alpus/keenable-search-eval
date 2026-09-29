"""Offline presentation of suite-native series; no inference or API clients."""

from __future__ import annotations

import csv
import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from statistics import mean, median, stdev

from .core import EvalError, atomic_json, suite_module


def build_report(run_dir: Path):
    state = json.loads((run_dir / "state.json").read_text())
    config = state["config"]
    attempts = [{**a, "artifact_dir": str(run_dir / a["artifact_dir"])} for a in state["attempts"]]
    from .core import ROOT, frozen_manifest, verify_run_code

    frozen_manifest(run_dir, state)
    verify_run_code(state, ROOT)
    grading_config = {
        **config,
        "manifest": str(run_dir / "manifest.json"),
        "manifest_path": str(run_dir / "manifest.json"),
        "configurations": config["arms"],
    }
    result = suite_module(config["suite"]).grade(attempts, run_dir / "grading", grading_config)
    return render(run_dir, config, attempts, result)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def capture_traces(run_dir, config, attempts, trace_dir):
    """Copy only this schedule's sanitized gateway records, never routing secrets."""
    arms = {a["id"]: a for a in config.get("arms", [])}
    output = []
    for attempt in attempts:
        aid = attempt["id"]
        name = sha256(aid.encode()) + ".jsonl"
        source = trace_dir / name
        target = run_dir / "evidence" / "gateway" / name
        # A moved report can be regenerated from its already captured evidence.
        source = source if source.is_file() else target
        rows, reservations, finished = [], {}, {}
        if source.is_file():
            payload = source.read_bytes()
            try:
                rows = [json.loads(line) for line in payload.splitlines() if line.strip()]
                for row in rows:
                    if (
                        row["attempt_id"] != aid
                        or row.get("configuration") != attempt["configuration"]
                        or row["operation"] not in {"search", "fetch"}
                        or row["event"] not in {"reserved", "finished"}
                    ):
                        raise ValueError("trace identity or event mismatch")
                    bucket = reservations if row["event"] == "reserved" else finished
                    if row["call_id"] in bucket:
                        raise ValueError("duplicate trace event")
                    bucket[row["call_id"]] = row
                if not set(finished) <= set(reservations):
                    raise ValueError("finish without reservation")
                for call_id, row in finished.items():
                    if row["operation"] != reservations[call_id]["operation"]:
                        raise ValueError("operation changed within call")
            except (KeyError, ValueError, TypeError) as exc:
                raise EvalError(f"Invalid trace for attempt {aid}: {exc}") from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
        rejection_name = sha256(aid.encode()) + ".rejections.jsonl"
        rejection_target = run_dir / "evidence" / "gateway" / rejection_name
        rejection_source = trace_dir / rejection_name
        rejection_source = rejection_source if rejection_source.is_file() else rejection_target
        rejections = []
        if rejection_source.is_file():
            rejection_bytes = rejection_source.read_bytes()
            try:
                rejections = [
                    json.loads(line) for line in rejection_bytes.splitlines() if line.strip()
                ]
                if any(
                    r.get("event") != "rejected"
                    or r.get("attempt_id") != aid
                    or r.get("configuration") != attempt["configuration"]
                    for r in rejections
                ):
                    raise ValueError("rejection attribution mismatch")
            except (ValueError, TypeError) as exc:
                raise EvalError(f"Invalid rejection trace for {aid}: {exc}") from exc
            rejection_target.parent.mkdir(parents=True, exist_ok=True)
            rejection_target.write_bytes(rejection_bytes)
        arm = arms.get(attempt["configuration"], {})
        output.append(
            {
                "attempt_id": aid,
                "task_id": attempt["task_id"],
                "configuration": attempt["configuration"],
                "repeat": attempt["repeat"],
                "role": attempt["role"],
                "trace_available": source.is_file(),
                "trace_path": str(target.relative_to(run_dir)) if source.is_file() else None,
                "trace_sha256": sha256(payload) if source.is_file() else None,
                "observed_search_calls": sum(
                    r["operation"] == "search" for r in reservations.values()
                )
                if source.is_file()
                else None,
                "observed_fetch_calls": sum(
                    r["operation"] == "fetch" for r in reservations.values()
                )
                if source.is_file()
                else None,
                "finished_calls": len(finished) if source.is_file() else None,
                "failed_calls": sum(r.get("ok") is False for r in finished.values())
                if source.is_file()
                else None,
                "unfinished_calls": len(reservations) - len(finished) if source.is_file() else None,
                "rejected_calls": len(rejections) if rejection_source.is_file() else None,
                "rejection_errors": [r.get("error") for r in rejections],
                "rejection_path": str(rejection_target.relative_to(run_dir))
                if rejection_source.is_file()
                else None,
                "rejection_sha256": sha256(rejection_bytes) if rejection_source.is_file() else None,
                "successful_search_durations_seconds": [
                    r["duration_seconds"]
                    for r in finished.values()
                    if r["operation"] == "search"
                    and r.get("ok") is True
                    and isinstance(r.get("duration_seconds"), (int, float))
                    and r["duration_seconds"] >= 0
                ],
                "failed_search_calls": sum(
                    r["operation"] == "search" and r.get("ok") is False for r in finished.values()
                ),
                "native_search_enabled": arm.get("native_search"),
                "native_search_calls": None,
                "native_observability": "unknown; CodeRabbit does not expose native calls"
                if config["suite"] == "martian"
                else "not applicable",
            }
        )
    atomic_json(run_dir / "tool-calls.json", output)
    return output


def search_latency(traces):
    """Pool individual successful calls, never medians of attempt medians."""
    groups = defaultdict(list)
    for row in traces:
        groups[(row["configuration"], row["repeat"], row["role"])].append(row)
    output = []
    for (arm, repeat, role), rows in sorted(groups.items()):
        values = [v for r in rows for v in r["successful_search_durations_seconds"]]
        output.append(
            {
                "configuration": arm,
                "repeat": repeat,
                "role": role,
                "successful_timed_calls": len(values),
                "duration_seconds": values,
                "median_seconds": median(values) if values else None,
                "failed_search_calls": sum(r["failed_search_calls"] for r in rows),
                "traces_available": sum(r["trace_available"] for r in rows),
                "scheduled_attempts": len(rows),
            }
        )
    return output


def paired_deltas(config, attempts, result):
    """Pair the same task, role and repeat, never union findings across repeats."""
    baseline = config["baseline"]
    index = {}
    task_scores = {(r["attempt_id"], r.get("scope")): r for r in result.get("per_task", [])}
    declared = {(r["metric"], r["scope"]) for r in result["report_series"]}
    for a in attempts:
        key = (a["task_id"], a["role"], a["repeat"], a["configuration"])
        if key in index:
            raise EvalError("Duplicate attempt in paired report")
        index[key] = a
    rows = []
    for key, a in index.items():
        task, role, repeat, arm = key
        if arm == baseline:
            continue
        b = index.get((task, role, repeat, baseline))
        common = {
            "task_id": task,
            "role": role,
            "repeat": repeat,
            "configuration": arm,
            "baseline": baseline,
            "attempt_id": a["id"],
            "baseline_attempt_id": b["id"] if b else None,
        }
        both = b is not None and a["status"] == b["status"] == "completed"
        metrics = [
            (
                "completed",
                "operations",
                int(a["status"] == "completed"),
                int(b["status"] == "completed") if b else None,
            )
        ]
        metrics.append(
            (
                "duration_seconds",
                "operations",
                a.get("duration_seconds") if both else None,
                b.get("duration_seconds") if both else None,
            )
        )
        for metric, scope in sorted(declared):
            av = task_scores.get((a["id"], scope), task_scores.get((a["id"], None), {}))
            bv = (
                task_scores.get((b["id"], scope), task_scores.get((b["id"], None), {})) if b else {}
            )
            if metric in av or metric in bv:
                metrics.append(
                    (
                        metric,
                        scope,
                        av.get(metric) if both else None,
                        bv.get(metric) if both else None,
                    )
                )
        for metric, scope, value, base in metrics:
            rows.append(
                {
                    **common,
                    "metric": metric,
                    "scope": scope,
                    "value": value,
                    "baseline_value": base,
                    "delta": value - base if value is not None and base is not None else None,
                    "paired_completed": both,
                }
            )
    return rows


def summarize_repeats(config, attempts, series):
    if config["repeats"] <= 1:
        return []
    groups = defaultdict(list)
    cohorts = defaultdict(list)
    for a in attempts:
        cohorts[(a["configuration"], a["role"], a["repeat"])].append(a["task_id"])
    for row in series:
        groups[(row["configuration"], row["role"], row["scope"], row["metric"])].append(row)
    output = []
    for (arm, role, scope, metric), rows in sorted(groups.items()):
        task_sets = [sorted(cohorts[(arm, role, r["repeat"])]) for r in rows]
        same = bool(task_sets) and all(
            t == task_sets[0] and len(t) == len(set(t)) for t in task_sets
        )
        complete = (
            same
            and {r["repeat"] for r in rows} == set(range(1, config["repeats"] + 1))
            and len(rows) == config["repeats"]
            and all(r["complete"] and r["value"] is not None for r in rows)
        )
        values = [r["value"] for r in rows] if complete else []
        output.append(
            {
                "configuration": arm,
                "role": role,
                "scope": scope,
                "metric": metric,
                "repeats_expected": config["repeats"],
                "repeats_present": len(rows),
                "same_task_cohort": same,
                "complete": complete,
                "mean": mean(values) if values else None,
                "min": min(values) if values else None,
                "max": max(values) if values else None,
                "sample_stddev": stdev(values) if len(values) > 1 else None,
            }
        )
    return output


def audit_bundle(run_dir, config, attempts, result, root):
    files = {}
    effective_path = run_dir / "effective-inputs.json"
    effective = json.loads(effective_path.read_text()) if effective_path.is_file() else {}
    gateway_target = run_dir / "evidence/gateway-config.json"
    if effective.get("gateway_settings") is not None:
        atomic_json(gateway_target, effective["gateway_settings"])
    paths = [
        ("state.json", run_dir / "state.json"),
        ("config.json", run_dir / "config.json"),
        ("effective-inputs.json", run_dir / "effective-inputs.json"),
        ("visible-tasks.json", run_dir / "visible-tasks.json"),
        (
            "evidence/gateway-config.json",
            gateway_target
            if gateway_target.is_file()
            else root / ".gateway/traces/gateway-config.json",
        ),
    ]
    if config.get("manifest"):
        if (run_dir / "state.json").is_file():
            from .core import frozen_manifest

            frozen_manifest(run_dir, json.loads((run_dir / "state.json").read_text()))
            paths.append(("evidence/manifest.json", run_dir / "manifest.json"))
        elif config.get("fixture"):
            paths.append(("evidence/manifest.json", root / config["manifest"]))
    if config["suite"] == "devdex_docs":
        agent = effective.get("agent", {})
        protocol = run_dir / "evidence/devdex-protocol.json"
        if not protocol.is_file():
            protocol = root / agent.get("protocol_file", "data/devdex-docs-protocol.json")
        if agent.get("protocol_sha256") and (
            not protocol.is_file() or sha256(protocol.read_bytes()) != agent["protocol_sha256"]
        ):
            raise EvalError("Report protocol differs from frozen agent inputs")
        paths += [
            ("evidence/devdex-protocol.json", protocol),
            ("evidence/devdex-source-access.json", root / "validation/devdex-source-access.json"),
        ]
    for name, source in paths:
        target = run_dir / name
        source = target if target.is_file() else source
        if source.is_file():
            payload = source.read_bytes()
            target.parent.mkdir(parents=True, exist_ok=True)
            if source != target:
                target.write_bytes(payload)
            files[name] = sha256(payload)
    models = []
    for attempt in attempts:
        path = Path(attempt["artifact_dir"]) / "record.json"
        if path.is_file():
            record = json.loads(path.read_text())
            models.append(
                {
                    "attempt_id": attempt["id"],
                    "record_sha256": sha256(path.read_bytes()),
                    **{
                        k: record.get(k)
                        for k in (
                            "model",
                            "actual_models",
                            "settings",
                            "request_snapshot_sha256",
                            "request_verified",
                        )
                    },
                }
            )
    state = (
        json.loads((run_dir / "state.json").read_text())
        if (run_dir / "state.json").is_file()
        else {}
    )
    audit = {
        "run_fingerprint": state.get("fingerprint"),
        "file_sha256": files,
        "source_commit": result.get("source_commit"),
        "pipeline": result.get("pipeline"),
        "arms": config.get("arms", []),
        "profiles": {
            name: {k: p.get(k) for k in ("provider", "endpoint", "connection", "tier", "mode")}
            for name, p in config.get("profiles", {}).items()
        },
        "suite_options": config.get("suite_options", {}),
        "observed_models": models,
        "limitations": result.get("limitations", []),
        "note": "Requested model and observed model are distinct. Missing values are unverified. Source access is a dated audit, not current uptime.",
    }
    atomic_json(run_dir / "audit.json", audit)
    return audit


def input_summary(run_dir):
    """Describe frozen inputs without resolving today's config or service defaults."""
    path = run_dir / "effective-inputs.json"
    if not path.is_file():
        return []
    saved = json.loads(path.read_text())
    config = saved.get("resolved_config", {})
    scorer = saved.get("scorer") or {}
    schedule = saved.get("schedule", {})
    arms = config.get("arms", [])
    providers = saved.get("providers", {})
    return [
        "",
        f"Dataset: `{config.get('manifest', 'unrecorded')}`; SHA256: `{saved.get('dataset_sha256', 'unrecorded')}`.",
        f"Scorer: `{scorer.get('name', 'unrecorded')}` at `{scorer.get('revision', 'unrecorded')}`. File hashes are in resolved inputs.",
        f"Schedule seed: `{schedule.get('seed', 'unrecorded')}`; repeats: `{schedule.get('repeats', 'unrecorded')}`. The schedule seed does not control model sampling.",
        "Requested search configurations: " + "; ".join(
            f"`{arm['id']}` (native={arm['native_search']}, MCP={','.join(arm['mcp']) or 'none'})"
            for arm in arms
        ) + ".",
        "Requested provider modes: " + "; ".join(
            f"`{name}`: `{profile['mode']}` / `{profile['tier']}`"
            for name, profile in providers.items()
        ) + ".",
        "Limits: `" + json.dumps(saved.get("limits", {}), sort_keys=True) + "`.",
        "Requested settings do not prove service application. Observed model IDs appear below; tool calls and responses remain in the evidence bundle. Unreported internal defaults remain unknown.",
        "",
    ]


def model_summary(run_dir, config, attempts):
    """Separate requested identities from identities returned in saved responses."""
    path = run_dir / "effective-inputs.json"
    effective = json.loads(path.read_text()) if path.is_file() else {}
    rows = []
    if config["suite"] == "martian":
        rows.append(
            {
                "stage": "CodeRabbit reviewer",
                "requested": None,
                "observed": [],
                "note": "Service-managed; model identity is not exposed by this integration",
            }
        )
        judge = effective.get("judge") or config.get("suite_options", {}).get("judge", {})
        observed = set()
        for attempt in attempts:
            ledger = Path(attempt["artifact_dir"]) / "judge-http.json"
            if ledger.is_file():
                for call in json.loads(ledger.read_text()):
                    model = call.get("response", {}).get("model")
                    if call.get("status") == 200 and model:
                        observed.add(model)
        rows.append(
            {
                "stage": "Martian extraction, deduplication and judge",
                "requested": judge.get("model"),
                "observed": sorted(observed),
                "note": "Only successful saved HTTP responses establish observed identity",
            }
        )
    elif config["suite"] == "devdex_docs":
        requested, observed = set(), set()
        if effective.get("agent", {}).get("requested_model"):
            requested.add(effective["agent"]["requested_model"])
        for attempt in attempts:
            record_path = Path(attempt["artifact_dir"]) / "record.json"
            if record_path.is_file():
                record = json.loads(record_path.read_text())
                model = record.get("requested_model", record.get("model"))
                if model:
                    requested.add(model)
                observed.update(record.get("actual_models") or [])
        rows.append(
            {
                "stage": "DevDex agent",
                "requested": ", ".join(sorted(requested)) or None,
                "observed": sorted(observed),
                "note": "Observed identities come from SDK messages",
            }
        )
    return rows


def frozen_visible_tasks(run_dir):
    """Resolve agent-visible inputs from the saved identity, never the current manifest."""
    state_path, visible_path = run_dir / "state.json", run_dir / "visible-tasks.json"
    rows = json.loads(visible_path.read_text()) if visible_path.is_file() else []
    if state_path.is_file():
        from .core import frozen_manifest

        state = json.loads(state_path.read_text())
        frozen_manifest(run_dir, state)
        expected = state["input_identity"]["tasks"]
        if visible_path.is_file() and rows != expected:
            raise EvalError("Visible task copy differs from frozen input identity")
        rows = expected
    if not isinstance(rows, list) or any(not isinstance(r, dict) or not r.get("id") for r in rows):
        raise EvalError("Invalid frozen visible task mapping")
    if len({r["id"] for r in rows}) != len(rows):
        raise EvalError("Duplicate frozen visible task identity")
    return {r["id"]: r for r in rows}


def task_evidence(config, attempts, result, traces, pairs):
    """Join suite-owned descriptive annotations without inventing task scores."""
    annotations = {}
    for row in result.get("task_annotations", []):
        if row["task_id"] in annotations:
            raise EvalError("Duplicate task annotation")
        annotations[row["task_id"]] = row
    by_trace = {row["attempt_id"]: row for row in traces}
    by_pair = defaultdict(list)
    for row in pairs:
        by_pair[row["attempt_id"]].append(row)
    baseline = {
        (a["task_id"], a["role"], a["repeat"]): a
        for a in attempts
        if a["configuration"] == config["baseline"]
    }
    output = []
    for attempt in attempts:
        annotation = annotations.get(attempt["task_id"], {})
        trace = by_trace.get(attempt["id"], {})
        base = baseline.get((attempt["task_id"], attempt["role"], attempt["repeat"]))
        output.append(
            {
                "attempt_id": attempt["id"],
                "task_id": attempt["task_id"],
                "configuration": attempt["configuration"],
                "role": attempt["role"],
                "repeat": attempt["repeat"],
                "status": attempt["status"],
                "documentation_relevance": annotation.get("documentation_relevance", "UNKNOWN"),
                "annotation": {k: v for k, v in annotation.items() if k != "task_id"},
                "baseline_attempt_id": base["id"] if base else None,
                "paired_deltas": by_pair[attempt["id"]],
                **{
                    k: trace.get(k)
                    for k in (
                        "observed_search_calls",
                        "observed_fetch_calls",
                        "native_search_calls",
                        "failed_calls",
                        "unfinished_calls",
                        "rejected_calls",
                    )
                },
            }
        )
    return output


def blind_audit_sample(run_dir, config, attempts, limit=20):
    """Sample unmatched findings for human diagnosis without changing native scores."""
    limit = min(20, max(0, limit))
    seed = config.get("seed", 1729)
    pools = defaultdict(list)
    unavailable = []
    tasks = frozen_visible_tasks(run_dir)
    # Strip only configured provider/connection identifiers, not ordinary arm words.
    names = {
        str(p[k])
        for p in config.get("profiles", {}).values()
        for k in ("provider", "connection")
        if p.get(k)
    }
    pattern = (
        re.compile(
            r"(?<!\w)(?:"
            + "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
            + r")(?!\w)",
            re.IGNORECASE,
        )
        if names
        else None
    )

    def rank(value):
        return sha256(json.dumps([seed, value], sort_keys=True).encode())

    for attempt in attempts:
        path = Path(attempt["artifact_dir"]) / "martian-grading.json"
        if attempt["status"] != "completed" or not path.is_file():
            unavailable.append(attempt["id"])
            continue
        saved = json.loads(path.read_text())
        if saved.get("status") != "complete":
            unavailable.append(attempt["id"])
            continue
        visible = Path(attempt["artifact_dir"]) / "visible-task.json"
        task = tasks.get(attempt["task_id"])
        if (
            task is None
            and not tasks
            and not (run_dir / "state.json").is_file()
            and visible.is_file()
        ):
            task = json.loads(visible.read_text())
            if task.get("id") not in (None, attempt["task_id"]):
                raise EvalError("Per-attempt visible task identity mismatch")
        task = task or {}
        code = {
            k: task.get("input", {}).get(k)
            for k in ("repo", "base_sha", "head_sha", "base_files", "head_files", "head_overrides")
            if task.get("input", {}).get(k) is not None
        }
        if not code:
            code = {"status": "unavailable", "reason": "No saved visible input for this task"}
        for index, finding in enumerate(saved.get("evaluation", {}).get("false_positives", [])):
            text = finding.get("candidate") if isinstance(finding, dict) else finding
            if not isinstance(text, str) or not text.strip():
                raise EvalError("Malformed saved Martian false-positive candidate")
            pools[attempt["configuration"]].append(
                {
                    "task_id": attempt["task_id"],
                    "code": code,
                    "finding": text,
                    "configuration": attempt["configuration"],
                    "attempt_id": attempt["id"],
                    "repeat": attempt["repeat"],
                    "role": attempt["role"],
                    "source_index": index,
                    "source_sha256": sha256(path.read_bytes()),
                    "selection_key": rank([attempt["id"], index, text]),
                }
            )
    for pool in pools.values():
        pool.sort(key=lambda row: row["selection_key"])
    arms = sorted(pools, key=rank)
    selected, offset = [], 0
    while len(selected) < limit:
        found = False
        for arm in arms:
            if offset < len(pools[arm]):
                selected.append(pools[arm][offset])
                found = True
                if len(selected) == limit:
                    break
        if not found:
            break
        offset += 1
    # Hide the arm rotation from the reviewer, as well as all arm metadata.
    selected.sort(key=lambda row: rank(["blind-order", row["selection_key"]]))
    items, mapping = [], []
    for index, row in enumerate(selected, 1):
        item_id = f"finding-{index:03d}"
        items.append(
            {
                "item_id": item_id,
                "task_id": row["task_id"],
                "code": row["code"],
                "finding": pattern.sub("[search service]", row["finding"])
                if pattern
                else row["finding"],
                "manual_verdict": None,
                "review_notes": "",
            }
        )
        mapping.append(
            {
                "item_id": item_id,
                **{
                    k: row[k]
                    for k in (
                        "configuration",
                        "attempt_id",
                        "repeat",
                        "role",
                        "source_index",
                        "source_sha256",
                    )
                },
                "source_field": "evaluation.false_positives",
                "raw_finding": row["finding"],
            }
        )
    existing = run_dir / "blind-review.json"
    if existing.is_file():
        previous = json.loads(existing.read_text()).get("items", [])
        has_work = any(
            r.get("manual_verdict") is not None or r.get("review_notes") for r in previous
        )

        def identity(row):
            return {k: row.get(k) for k in ("item_id", "task_id", "code", "finding")}

        if has_work and [identity(r) for r in previous] != [identity(r) for r in items]:
            raise EvalError(
                "Blind sample changed after manual review; preserve existing review before rebuilding"
            )
        if has_work:
            for item, old in zip(items, previous, strict=True):
                item.update(
                    manual_verdict=old.get("manual_verdict"),
                    review_notes=old.get("review_notes", ""),
                )
    blind = {
        "purpose": "Diagnostic sample of unmatched findings, not a precision estimate. Manual verdicts never change native scores.",
        "instructions": "Review each finding against the identified code. Leave unknown cases unresolved. Do not open audit-unblinding.json before recording verdicts.",
        "evidence_type": "fixture" if config.get("fixture") else "saved_native_evaluation",
        "items": items,
    }
    unblinding = {
        "seed": seed,
        "limit": limit,
        "selection": "Seeded stable-hash order within each configuration, balanced round robin, then blinded order.",
        "eligible_by_configuration": {arm: len(pools[arm]) for arm in sorted(pools)},
        "unavailable_attempts": unavailable,
        "items": mapping,
    }
    atomic_json(existing, blind)
    atomic_json(run_dir / "audit-unblinding.json", unblinding)
    return len(items)


def evidence_markdown(config, traces, pairs, repeats, audit, result):
    lines = [
        "",
        "## Search evidence",
        "",
        "Counts are observed gateway reservations, including failed or unfinished calls. Missing traces mean unknown, not zero. Rejected calls are recorded separately when attributable. Unauthenticated/unattributed events remain gateway-local and are not assigned to a run. Native CodeRabbit tool calls remain unknown.",
        "",
        "| Configuration | Repeat | Role | Traces / Attempts | Search | Fetch | Failed | Unfinished | Rejected | Native search |",
        "|---|---:|---|---:|---:|---:|---:|---:|---:|---|",
    ]
    groups = defaultdict(list)
    for row in traces:
        groups[(row["configuration"], row["repeat"], row["role"])].append(row)
    for (arm, repeat, role), rows in sorted(groups.items()):
        available = sum(r["trace_available"] for r in rows)

        def count(key):
            values = [r[key] for r in rows if r[key] is not None]
            if not values:
                return "unknown"
            return str(sum(values)) + (" (partial)" if len(values) < len(rows) else "")

        native = "unknown" if config["suite"] == "martian" else "not applicable"
        lines.append(
            f"| {arm} | {repeat} | {role} | {available}/{len(rows)} | {count('observed_search_calls')} | {count('observed_fetch_calls')} | {count('failed_calls')} | {count('unfinished_calls')} | {count('rejected_calls')} | {native} |"
        )
    lines += [
        "",
        "Exact attempts, trace paths and hashes: [tool-calls.json](tool-calls.json).",
        "",
        "## Paired comparisons",
        "",
        "[Per-task evidence](per-task-evidence.json) joins frozen suite annotations, gateway counts and baseline comparisons. UNKNOWN means no frozen relevance annotation. These tags do not prove search necessity or causal benefit.",
        "",
        "[Per-task deltas](paired-deltas.json) subtract the baseline on the same task, role and repeat. Missing pairs stay unavailable. Duration includes only pairs where both attempts completed. Positive duration means slower. No findings are pooled across repeats.",
    ]
    native_quality = [r for r in pairs if r["metric"] == "F2" and r["scope"] == "core"]
    if native_quality:
        lines += [
            "",
            "Core F2 per-task differences are descriptive diagnostics. They do not replace the frozen full-cohort headline policy.",
            "",
            "| Task | Configuration | Repeat | Role | Core F2 | Baseline Core F2 | Difference |",
            "|---|---|---:|---|---:|---:|---:|",
        ]
        for r in native_quality:
            values = [
                "unavailable" if r[k] is None else f"{r[k]:.4f}"
                for k in ("value", "baseline_value", "delta")
            ]
            lines.append(
                f"| {r['task_id']} | {r['configuration']} | {r['repeat']} | {r['role']} | "
                + " | ".join(values)
                + " |"
            )
    if not result.get("per_task"):
        lines.append(
            "This suite does not expose native per-task scores. Per-task comparisons cover completion and duration only; aggregate native metrics remain above."
        )
    for limitation in result.get("limitations", []):
        lines.append(str(limitation))
    if config["suite"] == "martian":
        lines += [
            "",
            "[Blind finding audit](blind-review.json): up to 20 unmatched findings, balanced across available configurations. This is a diagnostic sample, not a precision estimate. Manual verdicts start unresolved and never change native scores. Keep [unblinding metadata](audit-unblinding.json) separate until review is complete.",
        ]
    native_pairs = result.get("paired_descriptive", [])
    if native_pairs:
        lines += [
            "",
            "Native descriptive recall@10 pairs, including the scorer's failed-task handling:",
            "",
            "| Role | Repeat | Left | Right | Paired tasks | Left wins | Right wins | Ties |",
            "|---|---:|---|---|---:|---:|---:|---:|",
        ]
        for row in native_pairs:
            lines.append(
                f"| {row['role']} | {row['repeat']} | {row['left']} | {row['right']} | {row['paired_tasks']} | {row.get('left_wins', 0)} | {row.get('right_wins', 0)} | {row.get('ties', 0)} |"
            )
    if repeats:
        lines += [
            "",
            "## Repeat summaries",
            "",
            "Each row summarizes native per-repeat scores on the identical scheduled task cohort. Incomplete or suppressed repeats suppress the summary. These are descriptive ranges, not confidence intervals.",
            "",
            "| Configuration | Role | Scope | Metric | Mean | Min | Max | Sample SD | Complete |",
            "|---|---|---|---|---:|---:|---:|---:|---|",
        ]
        for row in repeats:
            vals = [
                "unavailable" if row[k] is None else f"{row[k]:.4f}"
                for k in ("mean", "min", "max", "sample_stddev")
            ]
            lines.append(
                f"| {row['configuration']} | {row['role']} | {row['scope']} | {row['metric']} | "
                + " | ".join(vals)
                + f" | {row['complete']} |"
            )
    lines += [
        "",
        "## Audit appendix",
        "",
        f"Run fingerprint: `{audit.get('run_fingerprint') or 'unverified'}`.",
        "[Audit metadata](audit.json) contains exact file hashes, arm settings, source revision, requested and observed models when recorded. Captured manifests and source-access audits are under `evidence/`; source availability is a dated observation.",
        "The bundle contains only this schedule's gateway traces. Routing credentials are not copied.",
    ]
    return lines


def render(run_dir, config, attempts, result, *, root=None, trace_dir=None):
    from .core import ROOT

    root = Path(root or ROOT)
    trace_dir = Path(trace_dir or root / ".gateway" / "traces")
    series = result.get("report_series", [])
    if not series:
        raise EvalError("Suite returned no declared report series")
    fields = [
        "configuration",
        "repeat",
        "role",
        "metric",
        "value",
        "unit",
        "direction",
        "denominator",
        "scope",
        "primary",
        "complete",
    ]
    with (run_dir / "metrics.csv").open("w") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(series)
    traces = capture_traces(run_dir, config, attempts, trace_dir)
    latency = search_latency(traces)
    atomic_json(run_dir / "search-latency.json", latency)
    pairs = paired_deltas(config, attempts, result)
    repeats = summarize_repeats(config, attempts, series)
    task_rows = task_evidence(config, attempts, result, traces, pairs)
    atomic_json(run_dir / "per-task-evidence.json", task_rows)
    audit = audit_bundle(run_dir, config, attempts, result, root)
    if config["suite"] == "martian":
        blind_audit_sample(
            run_dir,
            {
                **config,
                "fixture": config.get("fixture") or result.get("evidence_type") == "fixture",
            },
            attempts,
        )
    atomic_json(run_dir / "paired-deltas.json", pairs)
    atomic_json(run_dir / "repeat-summary.json", repeats)
    operational = []
    groups = defaultdict(list)
    for attempt in attempts:
        groups[(attempt["configuration"], attempt["repeat"], attempt["role"])].append(attempt)
    for (arm, repeat, role), values in sorted(groups.items()):
        durations = [
            a["duration_seconds"]
            for a in values
            if a["status"] == "completed" and a.get("duration_seconds") is not None
        ]
        operational.append(
            {
                "configuration": arm,
                "repeat": repeat,
                "role": role,
                "scheduled": len(values),
                "completed": sum(a["status"] == "completed" for a in values),
                "median_duration_seconds": median(durations) if durations else None,
            }
        )
    atomic_json(run_dir / "operational.json", operational)
    complete = bool(result.get("complete")) and all(a["status"] == "completed" for a in attempts)
    status = "COMPLETE" if complete else "INCOMPLETE"
    fixture = result.get("evidence_type") == "fixture" or config.get("fixture", False)
    roles = {a["role"] for a in attempts}
    if fixture:
        label = "SYNTHETIC FIXTURE, NOT MEASURED"
    elif roles == {"development"}:
        label = "Live development checks, excluded from benchmark results"
    elif roles == {"control"}:
        label = "Live controls, excluded from benchmark results"
    else:
        label = "Controlled public-subset pilot"
    lines = [
        f"# {config['suite']}: {status}",
        "",
        label,
        "",
        f"Run: `{config['run_id']}`. Baseline: `{config['baseline']}`. Repeats: {config['repeats']}.",
        "",
        "Scores below are the suite's native metrics. Operational failures remain visible.",
        "",
        "## Models and inputs",
        "",
        "[Resolved inputs](effective-inputs.json) record controlled settings and provider-managed defaults."
        if (run_dir / "effective-inputs.json").is_file()
        else "This historical run predates the consolidated input snapshot; use its saved config and request evidence.",
        "",
    ]
    lines += input_summary(run_dir)
    lines += [
        "| Stage | Requested model | Observed model | Evidence limit |",
        "|---|---|---|---|",
    ]
    for row in model_summary(run_dir, config, attempts):
        lines.append(
            f"| {row['stage']} | {row['requested'] or 'not selectable / unrecorded'} | "
            f"{', '.join(row['observed']) or 'not observed'} | {row['note']} |"
        )
    lines += [
        "",
        "## Native scores",
        "",
        "| Configuration | Repeat | Role | Scope | Metric | Value | Denominator | Complete |",
        "|---|---:|---|---|---|---:|---:|---|",
    ]
    for row in series:
        value = "unavailable" if row["value"] is None else f"{row['value']:.4f}"
        lines.append(
            f"| {row['configuration']} | {row['repeat']} | {row['role']} | {row['scope']} | {row['metric']} | {value} | {row['denominator']} | {row['complete']} |"
        )
    lines += [
        "",
        "## Coverage and timing",
        "",
        "| Configuration | Repeat | Role | Completed / Scheduled | Median seconds |",
        "|---|---:|---|---:|---:|",
    ]
    for row in operational:
        lines.append(
            f"| {row['configuration']} | {row['repeat']} | {row['role']} | {row['completed']}/{row['scheduled']} | {row['median_duration_seconds']} |"
        )
    lines += [
        "",
        "## Search latency",
        "",
        "Successful attributed search calls only, measured at the gateway. This is separate from end-to-end review time. Failed calls and missing traces stay visible; no native-search latency is inferred.",
        "",
        "| Configuration | Repeat | Role | Successful timed calls | Median seconds | Failed search calls |",
        "|---|---:|---|---:|---:|---:|",
    ]
    for row in latency:
        lines.append(
            f"| {row['configuration']} | {row['repeat']} | {row['role']} | {row['successful_timed_calls']} | {row['median_seconds']} | {row['failed_search_calls']} |"
        )
    lines += ["", "Exact per-call observations: [search-latency.json](search-latency.json)."]
    lines += evidence_markdown(config, traces, pairs, repeats, audit, result)
    lines += [
        "",
        "## Per-task evidence",
        "",
        "| Task | Configuration | Repeat | Role | Relevance | Search | Fetch | Baseline attempt |",
        "|---|---|---:|---|---|---:|---:|---|",
    ]

    def cell(value):
        return "unknown" if value is None else str(value).replace("|", "\\|").replace("\n", " ")

    for row in task_rows:
        lines.append(
            "| "
            + " | ".join(
                cell(row[k])
                for k in (
                    "task_id",
                    "configuration",
                    "repeat",
                    "role",
                    "documentation_relevance",
                    "observed_search_calls",
                    "observed_fetch_calls",
                    "baseline_attempt_id",
                )
            )
            + " |"
        )
    lines += [
        "",
        "## Interpretation",
        "",
        "Descriptive pilot, not statistical superiority or an official leaderboard result.",
        "Native CodeRabbit search calls and internal costs are not observable. Different suites are not averaged.",
        "See grading artifacts for native matching, exclusions, suppression and denominator checks.",
    ]
    (run_dir / "report.md").write_text("\n".join(lines) + "\n")
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    primary = [r for r in series if r["primary"]]
    valid = [r for r in primary if r["value"] is not None]
    timed = [r for r in operational if r["median_duration_seconds"] is not None]
    score_labels = [
        f"{r['configuration']} / {r['repeat']} / {r['role']} / {r['scope']}" for r in valid
    ]
    time_labels = [f"{r['configuration']} / {r['repeat']} / {r['role']}" for r in timed]
    longest = max((len(label) for label in score_labels + time_labels), default=0)
    height = max(4.5, 1.8 + 0.36 * max(len(valid), len(timed)))
    width = max(12, 7 + 0.18 * longest)
    fig, axes = plt.subplots(1, 2, figsize=(width, height), layout="constrained")
    axes[0].barh(score_labels, [r["value"] for r in valid], color="#3764c7")
    axes[0].invert_yaxis()
    axes[0].set_title(primary[0]["metric"] if primary else "No primary metric")
    axes[1].barh(time_labels, [r["median_duration_seconds"] for r in timed], color="#249c84")
    axes[1].invert_yaxis()
    axes[1].set_title("Completed episode/review median seconds")
    fig.suptitle(f"{config['suite']} | {status} | {len(attempts)} scheduled attempts\n{label}")
    fig.savefig(run_dir / "results.png", dpi=160)
    fig.savefig(run_dir / "results.svg")
    plt.close(fig)
    return {
        "complete": complete,
        "report": str(run_dir / "report.md"),
        "chart": str(run_dir / "results.png"),
    }
