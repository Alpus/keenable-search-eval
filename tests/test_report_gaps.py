"""Regression checks for reusable T08 evidence, blindness and chart layout."""

import json

import pytest
from test_report import attempt, audit_attempt, series

from search_eval.report import blind_audit_sample, model_summary, render


def test_model_summary_uses_returned_identity_and_keeps_hosted_model_unknown(tmp_path):
    folder = tmp_path / "attempt"
    folder.mkdir()
    (folder / "judge-http.json").write_text(
        json.dumps(
            [
                {"status": 200, "response": {"model": "returned-model"}},
                {"status": 500, "response": {"model": "failed-model"}},
            ]
        )
    )
    rows = model_summary(
        tmp_path,
        {"suite": "martian", "suite_options": {"judge": {"model": "requested-model"}}},
        [attempt(artifact_dir=str(folder))],
    )
    assert rows[0]["requested"] is None and rows[0]["observed"] == []
    assert rows[1]["requested"] == "requested-model"
    assert rows[1]["observed"] == ["returned-model"]


def test_devdex_requested_model_is_not_inferred_as_observed(tmp_path):
    folder = tmp_path / "attempt"
    folder.mkdir()
    (folder / "record.json").write_text(
        json.dumps({"model": "requested-model", "actual_models": []})
    )
    rows = model_summary(tmp_path, {"suite": "devdex_docs"}, [attempt(artifact_dir=str(folder))])
    assert rows[0]["requested"] == "requested-model"
    assert rows[0]["observed"] == []


@pytest.mark.parametrize("role", ["development", "control"])
def test_live_check_labels_cannot_be_mistaken_for_benchmark_results(tmp_path, role):
    rows = [attempt(role=role)]
    render(
        tmp_path,
        {"suite": "martian", "run_id": "live-check", "baseline": "search", "repeats": 1},
        rows,
        {"complete": True, "report_series": [series(role=role)]},
        root=tmp_path,
    )
    text = (tmp_path / "report.md").read_text()
    assert "excluded from benchmark results" in text
    assert "Controlled public-subset pilot" not in text


def test_blind_audit_resolves_frozen_run_level_visible_task(tmp_path):
    row = audit_attempt(tmp_path, "keenable", 1)
    folder = tmp_path / "keenable"
    (folder / "visible-task.json").unlink()
    run = tmp_path / "run"
    run.mkdir()
    expected = {"repo": "original/project", "base_sha": "abc", "head_sha": "def"}
    (run / "visible-tasks.json").write_text(
        json.dumps([{"id": row["task_id"], "kind": "pr_review", "input": expected}])
    )
    blind_audit_sample(run, {"seed": 42, "fixture": True}, [row])
    blind = json.loads((run / "blind-review.json").read_text())
    assert blind["items"][0]["code"] == expected


def test_chart_layout_keeps_dense_configuration_labels_separate(tmp_path, monkeypatch):
    from matplotlib.figure import Figure

    overlaps = []
    inspected = []
    original = Figure.savefig

    def inspect(figure, path, *args, **kwargs):
        figure.canvas.draw()
        renderer = figure.canvas.get_renderer()
        for axis in figure.axes:
            # Either horizontal or vertical layout may solve this generically.
            for labels in (axis.get_xticklabels(), axis.get_yticklabels()):
                labels = [label for label in labels if " / " in label.get_text()]
                inspected.extend(label.get_text() for label in labels)
                boxes = [label.get_window_extent(renderer) for label in labels]
                for i, first in enumerate(boxes):
                    overlaps.extend(first.overlaps(second) for second in boxes[i + 1 :])
        return original(figure, path, *args, **kwargs)

    monkeypatch.setattr(Figure, "savefig", inspect)
    arms = ["native", "none", "keenable", "exa", "native_keenable"]
    rows = [attempt(f"{arm}-{repeat}", arm=arm, repeat=repeat) for arm in arms for repeat in (1, 2)]
    scores = [
        series(arm=arm, repeat=repeat, scope="core", metric="F2")
        for arm in arms
        for repeat in (1, 2)
    ]
    render(
        tmp_path,
        {
            "suite": "martian",
            "run_id": "dense-fixture",
            "baseline": "native",
            "repeats": 2,
            "fixture": True,
        },
        rows,
        {"complete": True, "report_series": scores},
        root=tmp_path,
    )
    assert any("native_keenable" in label for label in inspected)
    assert not any(overlaps), "Configuration labels need a layout that scales with density"


def test_report_joins_suite_task_annotation_to_each_attempt(tmp_path):
    """Proposed generic annotation contract; tags originate from the frozen suite."""
    rows = [attempt("native", arm="native"), attempt("search", arm="keenable")]
    config = {
        "suite": "martian",
        "run_id": "annotations-fixture",
        "baseline": "native",
        "repeats": 1,
        "fixture": True,
        "arms": [{"id": "native", "native_search": True}, {"id": "keenable", "mcp": ["keenable"]}],
    }
    result = {
        "complete": True,
        "report_series": [series(arm="native"), series(arm="keenable")],
        "task_annotations": [
            {
                "task_id": "task",
                "documentation_relevance": "external_api_contract",
                "evidence_url": "https://docs.python.org/3/library/typing.html",
            }
        ],
    }
    render(tmp_path, config, rows, result, root=tmp_path)
    evidence = json.loads((tmp_path / "per-task-evidence.json").read_text())
    assert len(evidence) == 2
    assert {row["attempt_id"] for row in evidence} == {"native", "search"}
    assert all(row["documentation_relevance"] == "external_api_contract" for row in evidence)
    assert all(row["observed_search_calls"] is None for row in evidence)
    assert all(row["native_search_calls"] is None for row in evidence)
    assert "per-task-evidence.json" in (tmp_path / "report.md").read_text()


def test_blind_audit_redacts_provider_and_connection_only_in_blind_copy(tmp_path):
    row = audit_attempt(tmp_path, "keenable", 1)
    source = tmp_path / "keenable/martian-grading.json"
    saved = json.loads(source.read_text())
    raw = "Keenable and eval-search-a found this bug. Exa confirmed it; native behavior remains."
    saved["evaluation"]["false_positives"][0]["candidate"] = raw
    source.write_text(json.dumps(saved))
    original = source.read_bytes()
    run = tmp_path / "run"
    config = {
        "profiles": {
            "a": {"provider": "keenable", "connection": "eval-search-a"},
            "b": {"provider": "exa", "connection": "eval-search-b"},
        }
    }
    blind_audit_sample(run, config, [row])
    blind = json.loads((run / "blind-review.json").read_text())
    text = blind["items"][0]["finding"]
    assert text.count("[search service]") == 3
    assert "native behavior remains" in text
    assert source.read_bytes() == original
    assert json.loads((run / "audit-unblinding.json").read_text())["items"][0]["raw_finding"] == raw


def test_blind_audit_missing_context_is_explicit(tmp_path):
    row = audit_attempt(tmp_path, "keenable", 1)
    (tmp_path / "keenable/visible-task.json").unlink()
    run = tmp_path / "run"
    blind_audit_sample(run, {}, [row])
    item = json.loads((run / "blind-review.json").read_text())["items"][0]
    assert item["code"]["status"] == "unavailable"
    assert item["code"]["reason"]


def test_frozen_visible_copy_cannot_override_identity(tmp_path):
    import pytest
    from test_core import config, tasks

    from search_eval.core import EvalError, prepare_state
    from search_eval.report import frozen_visible_tasks

    (tmp_path / "data.json").write_text("{}")
    run = tmp_path / "run"
    prepare_state(config(), tasks(), run, tmp_path)
    assert list(frozen_visible_tasks(run)) == ["0", "1"]
    (run / "visible-tasks.json").write_text("[]")
    with pytest.raises(EvalError, match="differs from frozen"):
        frozen_visible_tasks(run)


def test_task_evidence_joins_counts_and_baseline_without_role_leak():
    from search_eval.report import task_evidence

    rows = [
        attempt("native", arm="native"),
        attempt("search", arm="keenable"),
        attempt("control", arm="keenable", role="control"),
    ]
    traces = [{"attempt_id": "search", "observed_search_calls": 2, "observed_fetch_calls": 1}]
    pairs = [{"attempt_id": "search", "metric": "duration_seconds", "delta": 3}]
    values = task_evidence({"baseline": "native"}, rows, {}, traces, pairs)
    assert values[1]["observed_search_calls"] == 2 and values[1]["observed_fetch_calls"] == 1
    assert values[1]["baseline_attempt_id"] == "native" and values[1]["paired_deltas"] == pairs
    assert values[2]["baseline_attempt_id"] is None
    assert all(row["documentation_relevance"] == "UNKNOWN" for row in values)


def test_martian_annotations_come_only_from_supplied_manifest(tmp_path):
    from search_eval.suites import martian

    path = tmp_path / "manifest.json"
    source = {"file": "pre-run-screen.md", "sha256": "frozen", "line": 17}
    path.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "known",
                        "role": "scored",
                        "documentation_relevance": "EXTERNAL_PLAUSIBLE",
                        "documentation_relevance_source": source,
                    },
                    {"id": "unclassified", "role": "scored"},
                ]
            }
        )
    )
    result = martian.grade([], tmp_path / "grading", {"manifest": str(path)})
    annotations = {row["task_id"]: row for row in result["task_annotations"]}
    assert annotations["known"]["documentation_relevance"] == "EXTERNAL_PLAUSIBLE"
    assert annotations["known"]["source"] == source
    assert annotations["unclassified"]["documentation_relevance"] == "UNKNOWN"


def test_audit_uses_frozen_protocol_and_gateway_after_workspace_changes(tmp_path):
    from search_eval.core import EvalError
    from search_eval.report import audit_bundle, sha256

    run = tmp_path / "run"
    run.mkdir()
    protocol = tmp_path / "custom.json"
    protocol.write_text('{"model":"pinned"}')
    effective = {
        "agent": {"protocol_file": "custom.json", "protocol_sha256": sha256(protocol.read_bytes())},
        "gateway_settings": {"mode": "original"},
    }
    (run / "effective-inputs.json").write_text(json.dumps(effective))
    config = {"suite": "devdex_docs"}
    audit_bundle(run, config, [], {}, tmp_path)
    protocol.write_text('{"model":"changed"}')
    gateway = tmp_path / ".gateway/traces/gateway-config.json"
    gateway.parent.mkdir(parents=True)
    gateway.write_text('{"mode":"new"}')
    audit_bundle(run, config, [], {}, tmp_path)
    assert json.loads((run / "evidence/devdex-protocol.json").read_text())["model"] == "pinned"
    assert json.loads((run / "evidence/gateway-config.json").read_text())["mode"] == "original"
    (run / "evidence/devdex-protocol.json").unlink()
    with pytest.raises(EvalError, match="protocol differs"):
        audit_bundle(run, config, [], {}, tmp_path)


def test_profile_specific_native_pairs_do_not_overwrite_each_other():
    from search_eval.report import paired_deltas

    rows = [attempt("a"), attempt("b", arm="base")]
    result = {
        "report_series": [series(scope=p, metric="F2") for p in ("core", "strict")],
        "per_task": [
            {"attempt_id": "a", "scope": "core", "F2": 0.2},
            {"attempt_id": "b", "scope": "core", "F2": 0.1},
            {"attempt_id": "a", "scope": "strict", "F2": 0.8},
            {"attempt_id": "b", "scope": "strict", "F2": 0.4},
        ],
    }
    pairs = paired_deltas({"baseline": "base"}, rows, result)
    assert {p["scope"]: p["delta"] for p in pairs if p["metric"] == "F2"} == {
        "core": 0.1,
        "strict": 0.4,
    }


def test_search_latency_pools_successful_calls_and_excludes_failures(tmp_path):
    import hashlib

    from search_eval.report import capture_traces, search_latency

    source = tmp_path / "traces"
    source.mkdir()
    for aid, samples in (("a", [1, 2, 3]), ("b", [100])):
        lines = []
        for n, value in enumerate(samples + [1000]):
            row = {
                "attempt_id": aid,
                "configuration": "search",
                "call_id": str(n),
                "operation": "search",
                "event": "reserved",
            }
            lines += [
                row,
                {**row, "event": "finished", "ok": n < len(samples), "duration_seconds": value},
            ]
        (source / (hashlib.sha256(aid.encode()).hexdigest() + ".jsonl")).write_text(
            "".join(json.dumps(r) + "\n" for r in lines)
        )
    traces = capture_traces(
        tmp_path / "run", {"suite": "martian"}, [attempt("a"), attempt("b")], source
    )
    row = search_latency(traces)[0]
    assert row["successful_timed_calls"] == 4
    assert row["median_seconds"] == 2.5
    assert row["failed_search_calls"] == 2


def test_unresolved_attempt_can_render_an_explicitly_incomplete_report(tmp_path, monkeypatch):
    from test_core import config, tasks

    from search_eval import core, report

    cfg = config()
    (tmp_path / "data.json").write_text('{"tasks": []}')
    run = tmp_path / "run"
    state = core.prepare_state(cfg, tasks(), run, tmp_path)
    state["attempts"][0]["status"] = "unknown"
    core.atomic_json(run / "state.json", state)

    class Suite:
        @staticmethod
        def grade(attempts, output_dir, grading_config):
            assert attempts[0]["status"] == "unknown"
            return {"complete": False, "report_series": [series(value=None, complete=False)]}

    monkeypatch.setattr(core, "ROOT", tmp_path)
    monkeypatch.setattr(report, "suite_module", lambda name: Suite())
    report.build_report(run)
    text = (run / "report.md").read_text()
    assert "INCOMPLETE" in text and "unavailable" in text
    assert json.loads((run / "state.json").read_text())["attempts"][0]["status"] == "unknown"
