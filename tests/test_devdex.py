from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from search_eval import devdex_worker as worker
from search_eval.suites import devdex


def record(task_id=None, rank=1, error=None):
    row = next(r for r in devdex.source_rows() if task_id is None or r["id"] == task_id)
    repo, path = row["canonical_sources"]["repo_files"][0].split(":", 1)
    url = f"https://github.com/{repo}/blob/HEAD/{path}"
    sources = [f"https://unrelated.example/{i}" for i in range(rank - 1)] + [url]
    return {
        "qid": row["id"],
        "sources": sources,
        "committed": True,
        "error": error,
        "search_log": [{"latency": 0.5, "results": [{"rank": 0, "url": url}]}],
        "searches": 1,
        "reads": 0,
        "latency": 1,
        "cost": 0,
    }


def test_manifest_and_visible_input():
    manifest = devdex.read_manifest()
    tasks = devdex.load_tasks()
    assert len(tasks) == 35
    assert len([t for t in tasks if t["role"] == "development"]) == 5
    assert len([t for t in tasks if t["role"] == "scored"]) == 30
    assert not {t["id"] for t in tasks} & {x["id"] for x in manifest["exclusions"]}
    assert all(set(task["input"]) == {"query"} for task in tasks)
    assert len(devdex.source_rows()) == 201


@pytest.mark.parametrize("rank,recall,mrr", [(1, 1, 1), (3, 1, 1 / 3), (10, 1, 0.1), (11, 0, 0)])
def test_native_citation_depth(rank, recall, mrr):
    scored = devdex.score_native([record(rank=rank)])
    assert scored["recall"] == recall
    assert scored["amrr"] == mrr


def test_native_canonicalization_and_pool_separation():
    row = record()
    row["sources"] = [
        row["sources"][0].replace("/HEAD/", "/other-ref/").replace("PrefectHQ", "prefecthq")
        + "#section"
    ]
    assert devdex.score_native([row])["recall"] == 1
    row["search_log"] = []
    scored = devdex.score_native([row])
    assert scored["recall"] == 1
    assert scored["groundedness"] == 0
    assert scored["from_memory"] == 1


def test_dead_boundary_is_strictly_above_ten_percent():
    rows = [record() for _ in range(10)]
    rows[0].update(error="timeout", sources=[], committed=False, search_log=[])
    assert not devdex.score_native(rows)["invalid"]
    rows[1].update(error="no_successful_search", sources=[], committed=False, search_log=[])
    scored = devdex.score_native(rows)
    assert scored["invalid"] and scored["n"] == 10 and scored["dead"] == 2


def test_url_scorer_parity():
    report, suite = devdex.native_modules()
    rows = [record(rank=1), record(rank=4), record(rank=11)]
    meta = suite._docs_meta()

    def is_gold(url, row):
        return bool(
            suite._docs_rank(
                {"search_log": [{"results": [{"rank": 0, "url": url}]}]}, meta[row["qid"]]
            )
        )

    expected = report.score_records(
        rows, "docs", is_gold, lambda row, _: bool(suite._docs_rank(row, meta[row["qid"]]))
    )
    assert devdex.score_native(rows) == expected


def test_known_urls_have_manually_derived_native_scores():
    qid = "doc::docs/v3/concepts/rate-limits.mdx#4"
    canonical = "https://github.com/PrefectHQ/prefect/blob/HEAD/docs/v3/concepts/rate-limits.mdx"
    rendered = "https://prefect.io/docs/v3/concepts/rate-limits.html#request-size"
    unrelated = "https://unrelated.example/docs"
    rows = [
        {"qid": qid, "sources": [canonical], "committed": True},
        {"qid": qid, "sources": [unrelated, rendered], "committed": True},
        {"qid": qid, "sources": [unrelated] * 10 + [canonical], "committed": True},
    ]
    for row in rows:
        row["search_log"] = [{"results": [{"rank": 0, "url": canonical}]}]
    actual = devdex.score_native(rows)
    # Two of three cite a canonical page within ten. Their ranks are 1 and 2.
    assert actual["recall"] == pytest.approx(2 / 3)
    assert actual["amrr"] == pytest.approx((1 + 1 / 2) / 3)
    assert actual["precision"] == pytest.approx(1 / 3)
    assert actual["eng_recall"] == 1
    assert actual["n"] == 3 and actual["dead"] == 0


def attempt(tmp_path, task, name="one", configuration="keenable", status="completed"):
    directory = tmp_path / name
    directory.mkdir()
    item = {
        "id": name,
        "task_id": task["id"],
        "role": task["role"],
        "configuration": configuration,
        "repeat": 1,
        "status": status,
        "artifact_dir": str(directory),
    }
    row = record(task_id=task["id"])
    row.update(attempt_id=name, configuration=configuration, repeat=1)
    (directory / "record.json").write_text(json.dumps(row))
    return item


def test_exact_records_and_missing_denominator(tmp_path):
    tasks = devdex.load_tasks()
    first = attempt(tmp_path, tasks[0])
    second = attempt(tmp_path, tasks[1], "two", status="failed")
    (Path(second["artifact_dir"]) / "record.json").unlink()
    # A perfect historical result exists nearby but is not scheduled.
    attempt(tmp_path, tasks[2], "historical")
    result = devdex.grade([first, second], tmp_path / "report", {"evidence_type": "fixture"})
    cell = result["cells"][0]
    assert result["scheduled"] == cell["native_metrics"]["n"] == 2
    assert cell["native_metrics"]["recall"] == 0.5
    assert cell["missing_outputs"] == 1
    assert cell["headline"]["recall@10"] is None and cell["suppressed"]
    assert not cell["schedule_complete"]


def test_wrong_identity_and_duplicate_schedule_rejected(tmp_path):
    item = attempt(tmp_path, devdex.load_tasks()[0])
    with pytest.raises(ValueError, match="Duplicate"):
        devdex.grade([item, item], tmp_path / "report", {})
    path = Path(item["artifact_dir"]) / "record.json"
    row = json.loads(path.read_text())
    row["attempt_id"] = "old-run"
    path.write_text(json.dumps(row))
    with pytest.raises(ValueError, match="exact episode"):
        devdex.grade([item], tmp_path / "report", {})


def test_nonterminal_attempt_cannot_be_finalized_as_dead(tmp_path):
    item = attempt(tmp_path, devdex.load_tasks()[0])
    item["status"] = "running"
    with pytest.raises(ValueError, match="terminal"):
        devdex.grade([item], tmp_path / "report", {})


def test_gold_fields_never_become_model_input():
    task = devdex.load_tasks()[0]
    canary = "PRIVATE_GOLD_ANSWER_CANARY"
    task.update(expected_answer=canary, canonical_sources=canary)
    snapshot = worker.request_snapshot(task)
    assert canary not in json.dumps(snapshot)
    assert task["id"] not in json.dumps(snapshot)
    task["input"]["expected_answer"] = canary
    with pytest.raises(ValueError, match="only"):
        worker.request_snapshot(task)


def test_model_substitution_and_unverified_access_fail_before_calls():
    task = devdex.load_tasks()[0]
    cfg = {"attempt_id": "a", "task_id": task["id"], "configuration": "keenable", "repeat": 1}
    with pytest.raises(ValueError, match="unverified"):
        worker.validate_execution(task, cfg)
    with pytest.raises(ValueError, match="replacement"):
        worker.validate_execution(task, {**cfg, "model": "another-model"})


def test_worker_actual_request_snapshot_without_model_calls(tmp_path, monkeypatch):
    task = devdex.load_tasks()[0]
    task["expected_answer"] = "HIDDEN_GOLD_CANARY"
    cfg = {
        "attempt_id": "fixture",
        "task_id": task["id"],
        "configuration": "keenable",
        "repeat": 1,
        "agent_access_verified": True,
        "allowance_verified": True,
    }
    monkeypatch.setenv("SEARCH_EVAL_MCP_URL", "http://localhost:8000/mcp/devdex")
    monkeypatch.setenv("SEARCH_EVAL_MCP_TOKEN", "SECRET_CANARY_TOKEN")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "FAKE_NO_NETWORK_KEY")
    from search_eval.effective_inputs import resolve_agent

    cfg["agent"] = resolve_agent({}, worker.ROOT)
    captured = []

    async def fake_query(*, prompt, options):
        assert worker.os.environ["MODEL"] == cfg["agent"]["requested_model"]
        assert all(worker.os.environ[k] == v for k, v in cfg["agent"]["settings"].items())
        captured.append(
            {"prompt": prompt, "system_prompt": options.system_prompt, "model": options.model}
        )
        yield SimpleNamespace(model=worker.MODEL, usage={"input_tokens": 20})

    fake_runner = SimpleNamespace(query=fake_query, JAIL=str(tmp_path / "jail"))

    async def fake_run_one(row):
        options = SimpleNamespace(system_prompt=worker.prompt_text(), model=worker.MODEL)
        async for _ in fake_runner.query(prompt=f"Task:\n{row['query']}", options=options):
            pass
        return {"qid": row["id"], "sources": [], "search_log": [], "error": None}

    fake_runner.run_one = fake_run_one
    original_import = worker.importlib.import_module
    monkeypatch.setattr(
        worker.importlib,
        "import_module",
        lambda name: fake_runner if name == "runner_sdk" else original_import(name),
    )
    # Restore process-global environment/argv/path after simulating a dedicated worker.
    monkeypatch.setattr(worker.sys, "argv", list(worker.sys.argv))
    monkeypatch.setattr(worker.sys, "path", list(worker.sys.path))
    for key in [
        *worker.SETTINGS,
        "MODEL",
        "GT_FILE",
        "OUT_DIR",
        "DEVDEX_EXT_MCP_URL",
        "DEVDEX_EXT_AUTH",
        "DEVDEX_EXT_SEARCH_TOOL",
        "DEVDEX_EXT_FETCH_TOOL",
        "CLAUDE_CODE_DISABLE_AUTO_MEMORY",
    ]:
        monkeypatch.setenv(key, "fixture-before")
    terminal = asyncio.run(worker.run_attempt(task, cfg, tmp_path / "out"))
    assert terminal["status"] == "completed"
    assert len(captured) == 1
    assert "HIDDEN_GOLD_CANARY" not in json.dumps(captured)
    assert "SECRET_CANARY_TOKEN" not in json.dumps(captured)
    saved = json.loads((tmp_path / "out" / "request-snapshot.json").read_text())
    assert all(captured[0][key] == saved[key] for key in captured[0])
    assert json.loads((tmp_path / "out" / "record.json").read_text())["request_verified"]


def test_original_agent_loop_with_stubbed_sdk_transport(tmp_path):
    """Run real upstream make_options/run_one/hooks; replace only the model transport."""
    script = r"""
import asyncio, json, os, sys
from pathlib import Path
import claude_agent_sdk as sdk
from search_eval import devdex_worker as worker
from search_eval.suites.devdex import load_tasks
captured = []
async def fake_query(*, prompt, options):
    captured.append({"prompt": prompt, "system_prompt": options.system_prompt,
                     "model": options.model, "allowed_tools": options.allowed_tools})
    for i in range(2):
        event = {"tool_name": "mcp__ext__search", "tool_input": {"query": "fixture " + str(i)}}
        await options.hooks["PreToolUse"][0].hooks[0](event, str(i), None)
        await options.hooks["PostToolUse"][0].hooks[0](
            {**event, "tool_response": {"content": [{"type": "text", "text": json.dumps(
                {"results": [{"url": "https://official.example/docs", "title": "Fixture", "text": "Evidence"}]})}]}}, str(i), None)
    yield sdk.AssistantMessage(content=[sdk.TextBlock(text="Fixture answer")], model=worker.MODEL)
sdk.query = fake_query
task = load_tasks()[0]
task["expected_answer"] = "PRIVATE_GOLD_CANARY"
cfg = {"attempt_id": "original-loop-fixture", "task_id": task["id"], "configuration": "keenable",
       "repeat": 1, "agent_access_verified": True, "allowance_verified": True}
out = Path(sys.argv[1])
terminal = asyncio.run(worker.run_attempt(task, cfg, out))
assert terminal["status"] == "completed", terminal
record = json.loads((out / "record.json").read_text())
assert record["searches"] == 2 and len(record["search_log"]) == 2
assert "PRIVATE_GOLD_CANARY" not in json.dumps(captured)
assert record["expected"] == ""
assert "mcp__ext__fetch" in captured[0]["allowed_tools"]
assert record["actual_models"] == [worker.MODEL]
assert record["request_verified"]
"""
    env = {
        **os.environ,
        "SEARCH_EVAL_MCP_URL": "http://localhost:8000/mcp/devdex",
        "SEARCH_EVAL_MCP_TOKEN": "FIXTURE_TOKEN",
        "ANTHROPIC_API_KEY": "FAKE_NO_NETWORK_KEY",
    }
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "original-loop")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
