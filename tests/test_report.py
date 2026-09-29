"""Offline report evidence remains scoped, paired, and explicitly qualified."""

import hashlib
import json
from pathlib import Path

import pytest

from search_eval.core import EvalError
from search_eval.report import capture_traces, paired_deltas, render, summarize_repeats


def attempt(aid="a", arm="search", task="task", role="scored", repeat=1, **kwargs):
    return {
        "id": aid,
        "task_id": task,
        "configuration": arm,
        "role": role,
        "repeat": repeat,
        "status": "completed",
        "duration_seconds": 5,
        "artifact_dir": "/missing",
        **kwargs,
    }


def series(arm="search", role="scored", repeat=1, value=0.5, **kwargs):
    return {
        "configuration": arm,
        "role": role,
        "repeat": repeat,
        "value": value,
        "metric": "recall@10",
        "scope": "docs",
        "unit": "fraction",
        "direction": "higher",
        "denominator": 1,
        "primary": True,
        "complete": True,
        **kwargs,
    }


def trace(path, aid="a", configuration="search", orphan=False):
    reserved = {
        "attempt_id": aid,
        "configuration": configuration,
        "operation": "search",
        "call_id": "one",
        "event": "reserved",
    }
    rows = ([] if orphan else [reserved]) + [{**reserved, "event": "finished", "ok": False}]
    rows.append({**reserved, "call_id": "two", "operation": "fetch"})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def test_exact_trace_copy_counts_and_portable_replay(tmp_path):
    source, bundle = tmp_path / "traces", tmp_path / "bundle"
    source.mkdir()
    filename = hashlib.sha256(b"a").hexdigest() + ".jsonl"
    trace(source / filename)
    (source / "unrelated.jsonl").write_text("sensitive unrelated run")
    config = {"suite": "martian", "arms": [{"id": "search", "native_search": True}]}
    rows = capture_traces(bundle, config, [attempt(), attempt("missing")], source)
    assert rows[0]["observed_search_calls"] == rows[0]["observed_fetch_calls"] == 1
    assert rows[0]["failed_calls"] == rows[0]["unfinished_calls"] == 1
    assert rows[0]["native_search_calls"] is None
    assert rows[1]["observed_search_calls"] is None
    assert len(list((bundle / "evidence/gateway").iterdir())) == 1
    assert rows[0]["trace_sha256"] == hashlib.sha256((source / filename).read_bytes()).hexdigest()
    assert capture_traces(bundle, config, [attempt()], tmp_path / "absent")[0] == rows[0]


@pytest.mark.parametrize(
    "options", [{"aid": "another-run"}, {"orphan": True}, {"configuration": "other-arm"}]
)
def test_trace_identity_and_lifecycle_rejected(tmp_path, options):
    source = tmp_path / "traces"
    source.mkdir()
    trace(source / (hashlib.sha256(b"a").hexdigest() + ".jsonl"), **options)
    with pytest.raises(EvalError, match="Invalid trace"):
        capture_traces(tmp_path / "bundle", {"suite": "martian"}, [attempt()], source)


def test_pairs_never_cross_roles_repeats_or_tasks():
    attempts = [
        attempt("a", duration_seconds=8),
        attempt("b", arm="base", duration_seconds=3),
        attempt("c", role="control"),
        attempt("d", repeat=2),
        attempt("e", task="different"),
    ]
    result = {
        "report_series": [series()],
        "per_task": [{"attempt_id": "a", "recall@10": 1}, {"attempt_id": "b", "recall@10": 0}],
    }
    rows = paired_deltas({"baseline": "base"}, attempts, result)
    exact = [r for r in rows if r["attempt_id"] == "a"]
    assert {r["metric"]: r["delta"] for r in exact} == {
        "completed": 0,
        "duration_seconds": 5,
        "recall@10": 1,
    }
    assert all(r["delta"] is None for r in rows if r["attempt_id"] != "a")


def test_failed_runtime_pair_keeps_completion_but_not_duration():
    attempts = [attempt("a", status="failed"), attempt("b", arm="base")]
    rows = paired_deltas({"baseline": "base"}, attempts, {"report_series": [series()]})
    assert rows[0]["delta"] == -1
    assert rows[1]["delta"] is None


def test_repeat_summary_same_cohort_only_and_no_role_mixing():
    attempts = [
        attempt(),
        attempt("b", repeat=2),
        attempt("c", role="control"),
        attempt("d", role="control", repeat=2),
    ]
    scores = [
        series(value=0.25),
        series(repeat=2, value=0.75),
        series(role="control", value=0),
        series(role="control", repeat=2, value=0),
    ]
    rows = summarize_repeats({"repeats": 2}, attempts, scores)
    scored = next(r for r in rows if r["role"] == "scored")
    assert scored["mean"] == 0.5 and scored["min"] == 0.25 and scored["max"] == 0.75
    attempts[1]["task_id"] = "different"
    rows = summarize_repeats({"repeats": 2}, attempts, scores)
    assert next(r for r in rows if r["role"] == "scored")["mean"] is None
    assert next(r for r in rows if r["role"] == "control")["mean"] == 0


def test_suppressed_repeat_never_becomes_average():
    rows = summarize_repeats(
        {"repeats": 2},
        [attempt(), attempt("b", repeat=2)],
        [series(), series(repeat=2, value=None)],
    )
    assert rows[0]["mean"] is None and not rows[0]["complete"]


def test_fixture_report_labels_charts_and_audit(tmp_path):
    manifest = tmp_path / "input.json"
    manifest.write_text('{"source":"fixture"}')
    attempts = [attempt(), attempt("b", arm="base")]
    config = {
        "suite": "martian",
        "run_id": "fixture",
        "baseline": "base",
        "repeats": 1,
        "manifest": "input.json",
        "fixture": True,
        "arms": [{"id": "search", "native_search": True, "mcp": ["test"]}],
    }
    result = {"complete": True, "report_series": [series(), series(arm="base")]}
    render(tmp_path, config, attempts, result, root=tmp_path)
    report = (tmp_path / "report.md").read_text()
    svg = (tmp_path / "results.svg").read_text()
    assert "SYNTHETIC FIXTURE, NOT MEASURED" in report and "SYNTHETIC FIXTURE, NOT MEASURED" in svg
    assert "scored / docs" in svg
    assert "Native CodeRabbit tool calls remain unknown" in report
    audit = json.loads((tmp_path / "audit.json").read_text())
    assert (
        audit["file_sha256"]["evidence/manifest.json"]
        == hashlib.sha256(manifest.read_bytes()).hexdigest()
    )
    assert audit["run_fingerprint"] is None


def audit_attempt(tmp_path, arm, count):
    path = tmp_path / arm
    path.mkdir()
    (path / "martian-grading.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "evaluation": {
                    "false_positives": [
                        {"candidate": f"Finding {i}: possible resource leak."} for i in range(count)
                    ]
                },
            }
        )
    )
    (path / "visible-task.json").write_text(
        json.dumps({"input": {"repo": "original/project", "base_sha": "abc", "head_sha": "def"}})
    )
    return attempt(arm, arm=arm, artifact_dir=str(path))


def test_blind_audit_bounded_balanced_deterministic_and_unresolved(tmp_path):
    from collections import Counter

    from search_eval.report import blind_audit_sample

    attempts = [audit_attempt(tmp_path, name, 15) for name in ("native", "keenable", "exa", "none")]
    bundle = tmp_path / "report"
    config = {"seed": 42, "fixture": True}
    original = [(Path(a["artifact_dir"]) / "martian-grading.json").read_bytes() for a in attempts]
    assert blind_audit_sample(bundle, config, attempts) == 20
    blind = json.loads((bundle / "blind-review.json").read_text())
    mapping = json.loads((bundle / "audit-unblinding.json").read_text())
    assert Counter(r["configuration"] for r in mapping["items"]) == {
        a["configuration"]: 5 for a in attempts
    }
    assert all(r["manual_verdict"] is None and r["review_notes"] == "" for r in blind["items"])
    assert all(
        set(r) == {"item_id", "task_id", "code", "finding", "manual_verdict", "review_notes"}
        for r in blind["items"]
    )
    assert all(
        r["code"] == {"repo": "original/project", "base_sha": "abc", "head_sha": "def"}
        for r in blind["items"]
    )
    assert blind["evidence_type"] == "fixture" and "not a precision estimate" in blind["purpose"]
    before = (bundle / "blind-review.json").read_bytes()
    blind_audit_sample(bundle, config, list(reversed(attempts)))
    assert before == (bundle / "blind-review.json").read_bytes()
    assert original == [
        (Path(a["artifact_dir"]) / "martian-grading.json").read_bytes() for a in attempts
    ]


def test_blind_audit_imbalanced_pool_and_preserves_manual_verdict(tmp_path):
    from search_eval.report import blind_audit_sample

    attempts = [audit_attempt(tmp_path, "small", 1), audit_attempt(tmp_path, "large", 30)]
    bundle = tmp_path / "report"
    blind_audit_sample(bundle, {"seed": 7}, attempts)
    mapping = json.loads((bundle / "audit-unblinding.json").read_text())
    assert sum(r["configuration"] == "small" for r in mapping["items"]) == 1
    blind = json.loads((bundle / "blind-review.json").read_text())
    blind["items"][0]["manual_verdict"] = "needs_more_context"
    (bundle / "blind-review.json").write_text(json.dumps(blind))
    blind_audit_sample(bundle, {"seed": 7}, attempts)
    assert (
        json.loads((bundle / "blind-review.json").read_text())["items"][0]["manual_verdict"]
        == "needs_more_context"
    )
    with pytest.raises(EvalError, match="sample changed"):
        blind_audit_sample(bundle, {"seed": 7}, attempts, limit=3)


def test_attributed_rejections_are_portable_and_distinct_from_no_trace(tmp_path):
    source = tmp_path / "traces"
    source.mkdir()
    filename = hashlib.sha256(b"a").hexdigest() + ".rejections.jsonl"
    (source / filename).write_text(
        json.dumps(
            {
                "event": "rejected",
                "attempt_id": "a",
                "configuration": "search",
                "operation": "search",
                "error": "disallowed_attempt",
            }
        )
        + "\n"
    )
    (source / "rejections.jsonl").write_text("unattributed events must not enter a run bundle")
    rows = capture_traces(tmp_path / "bundle", {"suite": "martian"}, [attempt()], source)
    assert rows[0]["rejected_calls"] == 1
    assert rows[0]["observed_search_calls"] is None
    assert rows[0]["rejection_errors"] == ["disallowed_attempt"]
    assert (tmp_path / "bundle" / rows[0]["rejection_path"]).read_bytes() == (
        source / filename
    ).read_bytes()
    assert not (tmp_path / "bundle/evidence/gateway/rejections.jsonl").exists()


def test_report_grades_frozen_manifest_not_changed_source(tmp_path, monkeypatch):
    from test_core import config, tasks

    from search_eval import core, report

    cfg = config()
    manifest = tmp_path / "data.json"
    manifest.write_text('{"original": true}')
    run_dir = tmp_path / "run"
    state = core.prepare_state(cfg, tasks(), run_dir, tmp_path)
    for a in state["attempts"]:
        a["status"] = "completed"
    core.atomic_json(run_dir / "state.json", state)
    manifest.write_text('{"original": false}')
    seen = []

    class Suite:
        def grade(self, attempts, output_dir, grading_config):
            seen.append(json.loads(Path(grading_config["manifest"]).read_text()))
            return {"complete": True}

    monkeypatch.setattr(core, "ROOT", tmp_path)
    monkeypatch.setattr(report, "suite_module", lambda name: Suite())
    monkeypatch.setattr(report, "render", lambda *args: {"fixture": True})
    report.build_report(run_dir)
    assert seen == [{"original": True}]
    (run_dir / "manifest.json").write_text("{}")
    with pytest.raises(EvalError, match="Frozen manifest hash"):
        report.build_report(run_dir)
