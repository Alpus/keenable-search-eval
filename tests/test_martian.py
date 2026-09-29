"""Offline checks. Fixtures do not represent new CodeRabbit measurements."""

import asyncio
import json

import pytest

from search_eval.suites import martian as m


def page(items, n=1):
    return {"page": n, "per_page": 30, "items": items}


def raw(items=()):
    return {
        "review_comments": [page(list(items))],
        "reviews": [page([])],
        "issue_comments": [page([])],
    }


def test_gold_profiles_preserved():
    gold = [g for row in m.gold_records().values() for g in row["comments"]]
    assert len(m.gold_records()) == 50
    assert len(gold) == 173
    assert sum(g["category"] in m.upstream("score").PROFILE_CATEGORIES["core"] for g in gold) == 158
    assert (
        sum(g["category"] in m.upstream("score").PROFILE_CATEGORIES["strict"] for g in gold) == 139
    )


def test_reference_matches_independent_published_dashboard():
    reference = m.reference_parity()
    dashboard = json.loads((m.UPSTREAM / "analysis/benchmark_dashboard.json").read_text())
    prs = dashboard["models"]["anthropic_claude-opus-4-5-20251101"]["prs"]
    for profile, tools in reference["scores"].items():
        # Dashboard was independently materialized upstream; compare every included tool.
        for tool, metric in tools.items():
            entries = [p["tool_metrics"][tool][profile] for p in prs if tool in p["tool_metrics"]]
            if not entries:  # dashboard additionally hides copilot and greptile-v5
                assert tool in {"copilot", "greptile-v5"}
                continue
            for count in ("tp", "fp", "fn"):
                assert metric[count] == sum(e[count] for e in entries)
            assert metric["prs"] == len(entries)


def test_first_page_projection_matches_original_records(monkeypatch):
    bot = {"login": "coderabbitai[bot]", "type": "Bot"}
    entries = [
        {
            "user": bot,
            "path": "x.py",
            "line": None,
            "original_line": i,
            "body": f"issue {i}",
            "created_at": "2026-01-01",
        }
        for i in range(31)
    ]
    entries[0]["user"] = {"login": "human"}
    fixture = raw(entries[:30])
    fixture["review_comments"].append(page(entries[30:], 2))
    fixture["reviews"] = [
        page([{"user": bot, "body": "Summary issue", "submitted_at": "2026-01-02"}])
    ]
    fixture["issue_comments"] = [
        page([{"user": bot, "body": "Acknowledgement", "created_at": "2026-01-03"}])
    ]

    def gh(args):
        endpoint = args[1]
        key = (
            "reviews"
            if endpoint.endswith("/reviews")
            else ("review_comments" if "/pulls/" in endpoint else "issue_comments")
        )
        return fixture[key][0]["items"]

    monkeypatch.setattr(m.upstream("collect"), "gh", gh)
    expected = m.upstream("collect").fetch_review_comments("org", "repo", 1, "coderabbit")
    projected = m.project_github_pages(fixture)
    assert projected["comments"] == expected
    assert projected["text"] == m.upstream("extract").get_all_comment_text(expected)
    assert projected["omitted_records"]["review_comments"] == 1
    assert len(expected) == 31
    with pytest.raises(ValueError):
        m.project_github_pages({**fixture, "reviews": []})


class FakeExtractor:
    model = "fixture"

    async def extract_from_comment(self, text):
        return {"issues": ["duplicate a", "duplicate b", "style finding", "unmatched"]}


class FakeDedup:
    model = "fixture"

    async def dedup_candidates(self, candidates, prompt):
        assert prompt == m.upstream("dedup").DEDUP_PROMPT
        return [[0, 1], [2], [3]]


class FakeJudge:
    model = "fixture"
    structured_output = False

    async def match_comment(self, golden, candidate):
        return {
            "match": (golden, candidate)
            in {("bug gold", "duplicate a"), ("style gold", "style finding")},
            "confidence": 1.0,
            "reasoning": "synthetic fixture",
        }


SETTINGS = {"model": "fixture", "temperature": 0.0, "structured_output": False}
GOLD = [{"comment": "bug gold", "category": "bug"}, {"comment": "style gold", "category": "style"}]


def test_pipeline_replay_duplicates_excluded_matches_and_errors():
    saved = asyncio.run(
        m.run_pipeline([], GOLD, FakeExtractor(), FakeDedup(), FakeJudge(), SETTINGS)
    )
    result = asyncio.run(m.replay_pipeline(saved, [], GOLD, SETTINGS))
    assert result["tp"] == 2 and result["fp"] == 1
    score = m.upstream("score").score_tools({"url": {"experiment": result}}, {}, "core", 2)[
        "experiment"
    ]
    assert score["tp"] == 1 and score["fp"] == 1  # style match is neutral; duplicate not FP
    assert score["fbeta"] == pytest.approx(5 / 6)
    saved["pairs"].pop()
    with pytest.raises(ValueError, match="Missing or extra"):
        asyncio.run(m.replay_pipeline(saved, [], GOLD, SETTINGS))


def test_empty_completed_is_all_fn_and_needs_no_pairs():
    class Empty(FakeExtractor):
        async def extract_from_comment(self, text):
            return {"issues": []}

    saved = asyncio.run(m.run_pipeline([], GOLD, Empty(), FakeDedup(), FakeJudge(), SETTINGS))
    result = asyncio.run(m.replay_pipeline(saved, [], GOLD, SETTINGS))
    assert result["fn"] == 2 and result["total_candidates"] == 0 and not result["skipped"]
    with pytest.raises(ValueError, match="Stale"):
        asyncio.run(m.replay_pipeline(saved, [{"body": "changed input"}], GOLD, SETTINGS))


def test_failed_dedup_never_becomes_singletons():
    class Broken(FakeDedup):
        async def dedup_candidates(self, candidates, prompt):
            return None

    with pytest.raises(ValueError, match="Dedup failed"):
        asyncio.run(m.run_pipeline([], GOLD, FakeExtractor(), Broken(), FakeJudge(), SETTINGS))


def test_manifest_gold_canary_and_blocked_snapshot(tmp_path):
    path = tmp_path / "manifest.json"
    row = {
        "id": "a",
        "role": "scored",
        "snapshot_status": "verified",
        "gold": "GOLD_CANARY",
        "input": {
            k: k
            for k in ("repo", "base_sha", "head_sha", "base_tree", "head_tree", "title", "body")
        },
    }
    row["input"]["gold"] = "GOLD_CANARY"
    path.write_text(json.dumps({"tasks": [row]}))
    assert "GOLD_CANARY" not in json.dumps(m.load_tasks(path))
    row["snapshot_status"] = "unresolved"
    path.write_text(json.dumps({"tasks": [row]}))
    with pytest.raises(ValueError, match="Unverified"):
        m.load_tasks(path)


def test_grade_failure_and_repeat_namespaces(tmp_path):
    url, source = next(iter(m.gold_records().items()))
    task = {"id": "a", "role": "scored", "golden_url": url}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tasks": [task], "judge": SETTINGS}))

    class Empty(FakeExtractor):
        async def extract_from_comment(self, text):
            return {"issues": []}

    saved = asyncio.run(
        m.run_pipeline([], source["comments"], Empty(), FakeDedup(), FakeJudge(), SETTINGS)
    )
    art = tmp_path / "attempt"
    art.mkdir()
    (art / "github.json").write_text(json.dumps(raw()))
    (art / "martian-grading.json").write_text(json.dumps(saved))
    attempts = [
        {
            "id": str(n),
            "task_id": "a",
            "role": "scored",
            "configuration": "qodo",
            "repeat": n,
            "status": "completed" if n == 1 else "failed",
            "artifact_dir": str(art),
        }
        for n in (1, 2)
    ]
    result = m.grade(attempts, tmp_path / "results", {"manifest": str(manifest)})
    assert not result["complete"]
    a, b = [r for r in result["metrics"] if r["profile"] == "core"]
    assert a["metrics"]["prs"] == 1 and a["metrics"]["fn"] > 0
    assert b["metrics"]["prs"] == 0 and b["metrics"]["fn"] == 0
    assert len(result["artifacts"]) == 2
    task_core = next(r for r in result["per_task"] if r["scope"] == "core")
    assert task_core["attempt_id"] == "1"
    assert task_core["fn"] == a["metrics"]["fn"]
    assert task_core["F2"] == a["metrics"]["fbeta"]
    assert {r["attempt_id"] for r in result["per_task"]} == {"1"}
    assert all(r["value"] is None for r in result["report_series"])
    assert all(r["score_use"] == "diagnostic_only" for r in result["metrics"])


def test_frozen_reconstructed_pilot_and_control_patch_evidence():
    import hashlib

    path = m.ROOT / "data/martian-pilot.json"
    manifest = json.loads(path.read_text())
    tasks = m.load_tasks(path)
    assert len(tasks) == 14
    assert {
        role: sum(t["role"] == role for t in tasks) for role in ("scored", "control", "development")
    } == {"scored": 10, "control": 2, "development": 2}
    originals = m.gold_records()
    for task in manifest["tasks"]:
        visible = next(t for t in tasks if t["id"] == task["id"])
        assert "gold" not in visible and "golden_url" not in visible
        assert "provenance" not in visible["input"]
        if task["role"] == "development":
            continue
        evidence = json.loads((m.ROOT / task["provenance"]["selected_file_patch"]).read_text())
        assert evidence["reviewed_head_confirmed"] and evidence["round_trip_patch_verified"]
        assert (
            hashlib.sha256(evidence["local_git_diff"].encode()).hexdigest()
            == evidence["local_git_diff_sha256"]
        )
        if task["role"] == "control":
            patch = task["input"]["head_overrides"][0]
            assert evidence["head_content"].count(patch["old_text"]) == 1
            fixed = evidence["head_content"].replace(patch["old_text"], patch["new_text"])
            assert hashlib.sha256(fixed.encode()).hexdigest() == task["fixed_file_sha256"]
            assert task["fixed_target"] not in task["gold"]
            source = next(t for t in manifest["tasks"] if t["id"] == task["source_task_id"])
            assert len(task["gold"]) + 1 == len(originals[source["golden_url"]]["comments"])


def test_authored_clean_completion_is_not_a_fabricated_score(tmp_path):
    class Empty(FakeExtractor):
        async def extract_from_comment(self, text):
            return {"issues": []}

    saved = asyncio.run(m.run_pipeline([], [], Empty(), FakeDedup(), FakeJudge(), SETTINGS))
    assert saved["evaluation"] == {"skipped": True, "reason": "No golden comments"}
    task = {"id": "clean", "role": "development", "golden_url": "fixture://clean", "gold": []}
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tasks": [task], "judge": SETTINGS}))
    (tmp_path / "github.json").write_text(json.dumps(raw()))
    (tmp_path / "martian-grading.json").write_text(json.dumps(saved))
    result = m.grade(
        [
            {
                "id": "x",
                "task_id": "clean",
                "role": "development",
                "configuration": "native",
                "repeat": 1,
                "status": "completed",
                "artifact_dir": str(tmp_path),
            }
        ],
        tmp_path / "out",
        {"manifest": str(manifest), "role": "development"},
    )
    assert result["complete"]
    assert result["diagnostics"][0]["candidate_count"] == 0
    assert not any(r["primary"] for r in result["report_series"])
    assert all(r["metrics"]["prs"] == 0 for r in result["metrics"])


def test_development_requests_contract_with_stub():
    import sys
    from types import SimpleNamespace
    from unittest.mock import patch

    manifest = json.loads((m.ROOT / "data/martian-pilot.json").read_text())
    task = next(t for t in manifest["tasks"] if t["id"] == "dev-requests-timeout")
    calls = []
    response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"ok": True})
    fake = SimpleNamespace(get=lambda *args, **kwargs: calls.append(kwargs) or response)
    with patch.dict(sys.modules, {"requests": fake}):
        for side in ("base_files", "head_files"):
            namespace = {}
            exec(task["input"][side]["status.py"], namespace)
            assert namespace["fetch_status"]("https://partner.example.invalid/status") == {
                "ok": True
            }
    assert calls[0]["timeout"] == (3.05, 10)
    assert "timeout" not in calls[1]


def test_all50_metadata_screen_reconciles_seeded_selection():
    import hashlib
    from collections import defaultdict

    screen = json.loads((m.ROOT / "validation/martian-metadata-screen.json").read_text())
    inventory = json.loads((m.ROOT / "data/martian-inventory.json").read_text())
    assert len(screen["records"]) == 50
    assert len({r["id"] for r in screen["records"]}) == 50
    groups = defaultdict(list)
    for row in screen["records"]:
        evidence = json.loads((m.ROOT / row["metadata_evidence"]).read_text())
        graph = {c["sha"]: [p["sha"] for p in c["parents"]] for c in evidence["commits"]}
        pending, seen = [row["head_sha"]], set()
        while pending:
            commit = pending.pop()
            if commit not in seen:
                seen.add(commit)
                pending.extend(graph.get(commit, []))
        assert row["base_ancestor"] == (row["base_sha"] in seen)
        if row["metadata_eligible"]:
            assert row["base_ancestor"] and row["head_reviewed"]
            assert row["base_tree"] and row["head_tree"]
        groups[row["group"]].append(next(t for t in inventory["tasks"] if t["id"] == row["id"]))
    selected = set()
    for rows in groups.values():
        eligible = [
            r for r in rows if r["metadata_eligible"] and r["gold_audit_status"] != "excluded"
        ]
        eligible.sort(
            key=lambda r: hashlib.sha256(
                ("martian-pilot-2026-09-29:" + r["golden_url"]).encode()
            ).hexdigest()
        )
        selected.update(r["id"] for r in eligible[:2])
    assert selected == set(screen["selection_reconciliation"]["ids"])
    assert len(selected) == 10


@pytest.mark.parametrize("target_matches", [True, False])
def test_fixed_target_diagnostic_is_separate_from_original_score(tmp_path, target_matches):
    target = {"comment": "fixed target", "category": "bug", "severity": "High"}
    gold = [{"comment": "remaining issue", "category": "bug", "severity": "Medium"}]

    class Extractor(FakeExtractor):
        async def extract_from_comment(self, text):
            return {"issues": ["observed finding"]}

    class Judge(FakeJudge):
        async def match_comment(self, golden, candidate):
            return {
                "match": golden == target["comment"] and target_matches,
                "confidence": 0.9,
                "reasoning": "fixture decision",
            }

    plain = asyncio.run(m.run_pipeline([], gold, Extractor(), FakeDedup(), Judge(), SETTINGS))
    saved = asyncio.run(
        m.run_pipeline([], gold, Extractor(), FakeDedup(), Judge(), SETTINGS, fixed_target=target)
    )
    assert saved["evaluation"] == plain["evaluation"]
    assert saved["pairs"] == plain["pairs"]
    assert all(p["golden"] != target["comment"] for p in saved["pairs"])
    assert saved["fixed_target"]["diagnostic"] == {
        "candidate_count": 1,
        "target_matched": target_matches,
        "matched_candidate_count": int(target_matches),
    }
    replayed = asyncio.run(m.replay_pipeline(saved, [], gold, SETTINGS, fixed_target=target))
    assert replayed == plain["evaluation"]
    task = {
        "id": "fixed",
        "role": "control",
        "golden_url": "fixture://control",
        "gold": gold,
        "fixed_target": target,
    }
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tasks": [task], "judge": SETTINGS}))
    (tmp_path / "github.json").write_text(json.dumps(raw()))
    (tmp_path / "martian-grading.json").write_text(json.dumps(saved))
    result = m.grade(
        [
            {
                "id": "c",
                "task_id": "fixed",
                "role": "control",
                "configuration": "native",
                "repeat": 1,
                "status": "completed",
                "artifact_dir": str(tmp_path),
            }
        ],
        tmp_path / "out",
        {"manifest": str(manifest)},
    )
    assert result["complete"]
    assert result["diagnostics"][0]["target_matched"] == target_matches
    assert all(r["metrics"]["tp"] == 0 and r["metrics"]["fn"] == 1 for r in result["metrics"])
    assert not any(r["primary"] for r in result["report_series"])
    with pytest.raises(ValueError, match="fixed-target fingerprint"):
        asyncio.run(
            m.replay_pipeline(
                saved, [], gold, SETTINGS, fixed_target={**target, "comment": "changed target"}
            )
        )
    saved["fixed_target"]["pairs"] = []
    with pytest.raises(ValueError, match="Missing or extra pairwise"):
        asyncio.run(m.replay_pipeline(saved, [], gold, SETTINGS, fixed_target=target))


def test_empty_review_does_not_match_fixed_target_or_require_extra_pairs():
    class Empty(FakeExtractor):
        async def extract_from_comment(self, text):
            return {"issues": []}

    target = {"comment": "fixed target", "category": "bug"}
    saved = asyncio.run(
        m.run_pipeline([], GOLD, Empty(), FakeDedup(), FakeJudge(), SETTINGS, fixed_target=target)
    )
    assert saved["fixed_target"]["pairs"] == []
    assert saved["fixed_target"]["diagnostic"] == {
        "candidate_count": 0,
        "target_matched": False,
        "matched_candidate_count": 0,
    }
    assert asyncio.run(m.replay_pipeline(saved, [], GOLD, SETTINGS, fixed_target=target))["fn"] == 2
    saved["fixed_target"]["diagnostic"]["target_matched"] = True
    with pytest.raises(ValueError, match="differs from original pairwise replay"):
        asyncio.run(m.replay_pipeline(saved, [], GOLD, SETTINGS, fixed_target=target))


@pytest.mark.parametrize("vary_decision", [False, True])
def test_replay_preserves_duplicate_text_pair_occurrences(vary_decision):
    class Repeated(FakeExtractor):
        async def extract_from_comment(self, text):
            return {"issues": ["same candidate", "same candidate"]}

    class Dedup(FakeDedup):
        async def dedup_candidates(self, candidates, prompt):
            return [[0, 1]]

    class VariableJudge(FakeJudge):
        def __init__(self):
            self.calls = 0

        async def match_comment(self, golden, candidate):
            index = self.calls
            self.calls += 1
            # Complete in reverse order to exercise recording before awaiting.
            await asyncio.sleep(0.001 if index % 2 == 0 else 0)
            return {
                "match": not (vary_decision and index % 2 == 1),
                "confidence": 0.9,
                "reasoning": f"Reason {index}",
            }

    gold = [{"comment": "remaining gold", "category": "bug"}]
    target = {"comment": "fixed target", "category": "bug"}
    saved = asyncio.run(
        m.run_pipeline(
            [], gold, Repeated(), Dedup(), VariableJudge(), SETTINGS, fixed_target=target
        )
    )
    assert len(saved["pairs"]) == 2 and len(saved["fixed_target"]["pairs"]) == 2
    assert saved["pairs"][0]["response"]["reasoning"] == "Reason 0"
    replay = asyncio.run(m.replay_pipeline(saved, [], gold, SETTINGS, fixed_target=target))
    assert replay == saved["evaluation"]
    assert replay["total_candidates"] == 2  # original count, never collapsed to one
    saved["pairs"].pop()
    with pytest.raises(ValueError, match="Missing or extra pairwise decisions"):
        asyncio.run(m.replay_pipeline(saved, [], gold, SETTINGS, fixed_target=target))


def test_equal_size_but_different_incomplete_cohorts_are_not_reported_as_scores(tmp_path):
    class Empty(FakeExtractor):
        async def extract_from_comment(self, text):
            return {"issues": []}

    tasks, attempts = [], []
    for index, (url, source) in enumerate(list(m.gold_records().items())[:2]):
        task_id = f"task{index}"
        tasks.append({"id": task_id, "role": "scored", "golden_url": url})
        art = tmp_path / task_id
        art.mkdir()
        saved = asyncio.run(
            m.run_pipeline([], source["comments"], Empty(), FakeDedup(), FakeJudge(), SETTINGS)
        )
        (art / "github.json").write_text(json.dumps(raw()))
        (art / "martian-grading.json").write_text(json.dumps(saved))
        attempts.append(
            {
                "id": task_id,
                "task_id": task_id,
                "role": "scored",
                "configuration": ["native", "exa"][index],
                "repeat": 1,
                "status": "completed",
                "artifact_dir": str(art),
            }
        )
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"tasks": tasks, "judge": SETTINGS}))
    result = m.grade(
        attempts, tmp_path / "out", {"manifest": str(manifest), "configurations": ["native", "exa"]}
    )
    assert not result["complete"]
    assert all(row["value"] is None and not row["complete"] for row in result["report_series"])
    assert all(row["score_use"] == "diagnostic_only" for row in result["metrics"])
    assert all(row["scope"] == "partial_diagnostic" for row in result["metrics"])
    assert all(row["metrics"]["prs"] == 1 for row in result["metrics"])


@pytest.mark.parametrize("failure", ["parse", "timeout"])
def test_original_stage_clients_make_one_attempt_even_on_parse_or_timeout(failure):
    from types import SimpleNamespace

    calls = []

    async def create(**kwargs):
        calls.append(kwargs)
        if failure == "timeout":
            raise TimeoutError("fixture transport timeout")
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="not JSON"))]
        )

    client = SimpleNamespace(
        max_retries=0, chat=SimpleNamespace(completions=SimpleNamespace(create=create))
    )
    extractor, deduper, judge = m.pipeline_clients(client, SETTINGS)
    result = asyncio.run(
        extractor.extract_from_comment("A sufficiently long review comment for extraction.")
    )
    assert result.get("error") and len(calls) == 1
    result = asyncio.run(
        deduper.dedup_candidates(["first", "second"], m.upstream("dedup").DEDUP_PROMPT)
    )
    assert result is None and len(calls) == 2
    result = asyncio.run(judge.match_comment("gold", "candidate"))
    assert result.get("error") and len(calls) == 3
    with pytest.raises(ValueError, match="max_retries=1"):
        asyncio.run(judge.call_llm("prompt", max_retries=3))
    assert len(calls) == 3
    assert all(request["temperature"] == 0.0 for request in calls)


def test_pipeline_factory_rejects_hidden_sdk_retries():
    from types import SimpleNamespace

    with pytest.raises(ValueError, match="transport retries must be disabled"):
        m.pipeline_clients(SimpleNamespace(max_retries=2), SETTINGS)


def test_before_judging_gate_counts_whole_matrix_and_blocks_every_match():
    events = []

    class Dedup(FakeDedup):
        async def dedup_candidates(self, candidates, prompt):
            events.append("dedup")
            return await super().dedup_candidates(candidates, prompt)

    class Judge(FakeJudge):
        async def match_comment(self, golden, candidate):
            events.append("judge")
            return await super().match_comment(golden, candidate)

    def gate(count):
        assert events == ["dedup"]
        assert count == (len(GOLD) + 1) * 4
        raise ValueError("No remaining HTTP allowance")

    with pytest.raises(ValueError, match="No remaining HTTP allowance"):
        asyncio.run(
            m.run_pipeline(
                [],
                GOLD,
                FakeExtractor(),
                Dedup(),
                Judge(),
                SETTINGS,
                fixed_target={"comment": "fixed", "category": "bug"},
                before_judging=gate,
            )
        )
    assert events == ["dedup"]


def test_async_before_judging_gate_runs_before_matchers():
    admitted = []

    async def gate(count):
        await asyncio.sleep(0)
        admitted.append(count)

    class Judge(FakeJudge):
        async def match_comment(self, golden, candidate):
            assert admitted == [len(GOLD) * 4]
            return await super().match_comment(golden, candidate)

    asyncio.run(
        m.run_pipeline(
            [], GOLD, FakeExtractor(), FakeDedup(), Judge(), SETTINGS, before_judging=gate
        )
    )
    assert admitted == [len(GOLD) * 4]


def test_grade_live_suite_dispatch_delegates_without_network(monkeypatch, tmp_path):
    from search_eval import grading

    called = []
    monkeypatch.setattr(
        grading, "grade_live", lambda run_dir: called.append(run_dir) or {"ok": True}
    )
    assert m.grade_live(tmp_path) == {"ok": True}
    assert called == [tmp_path]
