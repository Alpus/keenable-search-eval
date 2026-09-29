"""Transport ledger must pair out-of-order responses with their own requests."""

import json

import httpx
import pytest

from search_eval import grading
from search_eval.core import Experiment


@pytest.mark.asyncio
async def test_concurrent_judge_responses_keep_request_identity(tmp_path, monkeypatch):
    from search_eval.suites import martian

    folder = tmp_path / "attempts/a"
    folder.mkdir(parents=True)
    (folder / "github.json").write_text("{}")
    (tmp_path / "manifest.json").write_text(
        json.dumps({"tasks": [{"id": "t", "role": "development", "gold": []}]})
    )
    monkeypatch.setattr(grading, "ROOT", tmp_path)
    monkeypatch.setattr(
        grading, "frozen_manifest", lambda *a: json.loads((tmp_path / "manifest.json").read_text())
    )
    monkeypatch.setattr(martian, "configure_single_attempt_clients", lambda *a: None, raising=False)
    monkeypatch.setattr(
        grading,
        "verify_allowance",
        lambda *a: {"max_judge_usd": 10, "existing_model_credit_usd": 10},
    )
    monkeypatch.setenv("MARTIAN_BASE_URL", "https://unused-environment.example/v1")
    monkeypatch.setenv("MARTIAN_API_KEY", "fixture-secret")
    monkeypatch.setattr(martian, "gold_records", lambda: {})
    monkeypatch.setattr(martian, "project_github_pages", lambda raw: {"comments": []})
    monkeypatch.setattr(
        martian, "pipeline_clients", lambda client, settings: (client, client, client)
    )
    config = Experiment.model_validate(
        dict(
            run_id="test",
            suite="martian",
            manifest="manifest.json",
            baseline="a",
            arms=[dict(id="a")],
            profiles={},
            limits={"max_attempts": 1},
            suite_options={
                "judge": {
                    "protocol_variant_accepted": True,
                    "base_url": "https://judge.example/v1",
                },
                "max_judge_calls_per_attempt": 2,
                "max_judge_usd": 10,
            },
        )
    )

    async def pipeline(projected, gold, one, *rest, **kwargs):
        assert str(one.base_url) == "https://judge.example/v1/"
        assert one.max_retries == 0
        assert one.timeout == 120
        hooks = one._client.event_hooks
        requests = [
            httpx.Request(
                "POST",
                "https://judge.example/v1",
                json={"id": i, "model": "claude-opus-4-5-20251101"},
            )
            for i in range(2)
        ]
        for request in requests:
            await hooks["request"][0](request)
        for i in [1, 0]:
            await hooks["response"][0](httpx.Response(200, json={"answer": i}, request=requests[i]))
        return {"fixture": True}

    monkeypatch.setattr(martian, "run_pipeline", pipeline)
    await grading._grade(
        {"attempts": [dict(id="a", task_id="t", status="completed", artifact_dir="attempts/a")]},
        config,
        tmp_path,
    )
    entries = json.loads((folder / "judge-http.json").read_text())
    assert [(e["body"]["id"], e["response"]["answer"]) for e in entries] == [(0, 0), (1, 1)]
    assert "fixture-secret" not in (folder / "judge-http.json").read_text()


@pytest.fixture
def grading_case(tmp_path, monkeypatch):
    from search_eval.suites import martian

    manifest = {"tasks": [{"id": "t", "role": "development", "gold": []}]}
    attempts = []
    for aid in ("a", "b"):
        folder = tmp_path / "attempts" / aid
        folder.mkdir(parents=True)
        (folder / "github.json").write_text(json.dumps({"attempt": aid}))
        attempts.append(
            dict(id=aid, task_id="t", status="completed", artifact_dir=f"attempts/{aid}")
        )
    monkeypatch.setattr(grading, "ROOT", tmp_path)
    monkeypatch.setattr(grading, "frozen_manifest", lambda *a: manifest)
    monkeypatch.setattr(
        grading,
        "verify_allowance",
        lambda *a: {"max_judge_usd": 10, "existing_model_credit_usd": 10},
    )
    monkeypatch.setenv("MARTIAN_API_KEY", "fixture-secret")
    monkeypatch.setattr(martian, "gold_records", lambda: {})
    monkeypatch.setattr(martian, "project_github_pages", lambda raw: {"comments": [raw["attempt"]]})
    monkeypatch.setattr(
        martian, "pipeline_clients", lambda client, settings: (client, client, client)
    )
    monkeypatch.setattr(martian, "configure_single_attempt_clients", lambda *a: None)

    async def replay(saved, projected, *args, **kwargs):
        assert saved == {"status": "complete", "attempt": projected[0]}

    monkeypatch.setattr(martian, "replay_pipeline", replay)
    config = Experiment.model_validate(
        dict(
            run_id="test",
            suite="martian",
            manifest="manifest.json",
            baseline="a",
            arms=[dict(id="a")],
            profiles={},
            limits={"max_attempts": 2},
            suite_options={
                "judge": {
                    "model": "claude-opus-4-5-20251101",
                    "protocol_variant_accepted": True,
                    "base_url": "https://judge.example/v1",
                },
                "max_judge_calls_per_attempt": 2,
                "max_judge_usd": 10,
            },
        )
    )
    return tmp_path, {"attempts": attempts}, config, martian


async def recorded_call(client, *, status=200, text=None):
    hooks = client._client.event_hooks
    request = httpx.Request(
        "POST", "https://judge.example/v1", json={"model": "claude-opus-4-5-20251101"}
    )
    await hooks["request"][0](request)
    response = (
        httpx.Response(status, text=text, request=request)
        if text is not None
        else httpx.Response(status, json={"model": "claude-opus-4-5-20251101"}, request=request)
    )
    await hooks["response"][0](response)


@pytest.mark.asyncio
async def test_first_failure_is_terminal_and_later_attempt_succeeds(grading_case, monkeypatch):
    import hashlib

    root, state, config, martian = grading_case
    calls = []

    async def pipeline(projected, gold, client, *args, **kwargs):
        aid = projected[0]
        calls.append(aid)
        await recorded_call(
            client,
            status=502 if aid == "a" else 200,
            text="upstream unavailable" if aid == "a" else None,
        )
        return {"status": "complete", "attempt": aid}

    monkeypatch.setattr(martian, "run_pipeline", pipeline)
    result = await grading._grade(state, config, root)
    assert result["graded_attempts"] == ["b"] and not result["complete"]
    assert len(result["failed_attempts"]) == 1
    ledger = root / "attempts/a/judge-http.json"
    entries = json.loads(ledger.read_text())
    assert entries[0]["status"] == 502
    assert entries[0]["response_text"] == "upstream unavailable"
    assert entries[0]["response_json_error"] is True
    marker = root / "attempts/a/martian-grading-failure.json"
    failure = json.loads(marker.read_text())
    assert failure["outcome"] == "failed" and failure["retry_allowed"] is False
    assert failure["ledger_sha256"] == hashlib.sha256(ledger.read_bytes()).hexdigest()
    saved = {p: p.read_bytes() for p in root.rglob("*.json")}
    again = await grading._grade(state, config, root)
    assert again["graded_attempts"] == ["b"] and not again["complete"]
    assert calls == ["a", "b"]
    assert all(p.read_bytes() == content for p, content in saved.items())


@pytest.mark.asyncio
async def test_ambiguous_inflight_failure_is_not_retried(grading_case, monkeypatch):
    root, state, config, martian = grading_case
    calls = []

    async def pipeline(projected, gold, client, *args, **kwargs):
        aid = projected[0]
        calls.append(aid)
        if aid == "a":
            request = httpx.Request(
                "POST", "https://judge.example/v1", json={"model": "claude-opus-4-5-20251101"}
            )
            await client._client.event_hooks["request"][0](request)
            raise httpx.ReadTimeout("Response not observed", request=request)
        await recorded_call(client)
        return {"status": "complete", "attempt": aid}

    monkeypatch.setattr(martian, "run_pipeline", pipeline)
    result = await grading._grade(state, config, root)
    assert result["failed_attempts"][0]["outcome"] == "unknown"
    assert result["graded_attempts"] == ["b"]
    assert json.loads((root / "attempts/a/judge-http.json").read_text())[0]["status"] == "reserved"
    await grading._grade(state, config, root)
    assert calls == ["a", "b"]


@pytest.mark.asyncio
async def test_existing_incomplete_ledger_is_quarantined_without_stalling_later_tasks(
    grading_case, monkeypatch
):
    root, state, config, martian = grading_case
    ledger = root / "attempts/a/judge-http.json"
    original = '[{"status":"reserved","body":{"model":"claude-opus-4-5-20251101"}}]'
    ledger.write_text(original)
    calls = []

    async def pipeline(projected, gold, client, *args, **kwargs):
        calls.append(projected[0])
        await recorded_call(client)
        return {"status": "complete", "attempt": projected[0]}

    monkeypatch.setattr(martian, "run_pipeline", pipeline)
    result = await grading._grade(state, config, root)
    assert result["failed_attempts"][0]["phase"] == "prior_incomplete_grading"
    assert result["failed_attempts"][0]["outcome"] == "unknown"
    assert calls == ["b"] and ledger.read_text() == original


@pytest.mark.asyncio
async def test_run_allowance_denial_stops_unstarted_attempts_without_charged_requests(
    grading_case, monkeypatch
):
    from search_eval.core import EvalError

    root, state, config, martian = grading_case
    calls = []

    def denied(*args):
        raise EvalError("Existing run allowance exhausted")

    async def pipeline(projected, gold, client, *args, **kwargs):
        calls.append(projected[0])
        await recorded_call(client)
        raise AssertionError("Request must be denied before transport")

    monkeypatch.setattr(grading, "verify_allowance", denied)
    monkeypatch.setattr(martian, "run_pipeline", pipeline)
    result = await grading._grade(state, config, root)
    assert not result["complete"] and result["blocked_reason"]
    assert result["failed_attempts"] == [] and calls == ["a"]
    assert not list(root.rglob("judge-http.json"))
    assert not list(root.rglob("martian-grading-failure.json"))


@pytest.mark.asyncio
async def test_doctor_and_live_grading_reject_truthy_nonboolean_protocol_approval(grading_case):
    from search_eval.core import EvalError

    root, state, config, _ = grading_case
    config.suite_options["judge"]["protocol_variant_accepted"] = "yes"
    problems = grading.judge_problems(config, {})
    assert any("Historical judge" in problem for problem in problems)
    with pytest.raises(EvalError, match="Historical judge"):
        await grading._grade(state, config, root)
    assert not list(root.rglob("judge-http.json"))


@pytest.mark.asyncio
async def test_changed_terminal_failure_evidence_blocks_replay(grading_case, monkeypatch):
    from search_eval.core import EvalError

    root, state, config, martian = grading_case
    ledger = root / "attempts/a/judge-http.json"
    ledger.write_text('[{"status":"reserved"}]')

    async def pipeline(projected, *args, **kwargs):
        return {"status": "complete", "attempt": projected[0]}

    monkeypatch.setattr(martian, "run_pipeline", pipeline)
    await grading._grade(state, config, root)
    ledger.write_text("[]")
    with pytest.raises(EvalError, match="failure evidence changed"):
        await grading._grade(state, config, root)


@pytest.mark.asyncio
async def test_ambiguous_cost_reservation_blocks_later_calls_and_survives_resume(
    grading_case, monkeypatch
):
    root, state, config, martian = grading_case
    config.suite_options["max_judge_usd"] = 2.60
    monkeypatch.setattr(
        grading,
        "verify_allowance",
        lambda *a: {"max_judge_usd": 2.60, "existing_model_credit_usd": 3},
    )
    charged = []

    async def pipeline(projected, gold, client, *args, **kwargs):
        request = httpx.Request(
            "POST", "https://judge.example/v1", json={"model": "claude-opus-4-5-20251101"}
        )
        await client._client.event_hooks["request"][0](request)
        charged.append(projected[0])
        raise httpx.ReadTimeout("Ambiguous provider response", request=request)

    monkeypatch.setattr(martian, "run_pipeline", pipeline)
    first = await grading._grade(state, config, root)
    assert not first["complete"] and "$2.60 reservation" in first["blocked_reason"]
    assert charged == ["a"]
    assert first["failed_attempts"][0]["outcome"] == "unknown"
    assert not (root / "attempts/b/judge-http.json").exists()
    assert not (root / "attempts/b/martian-grading-failure.json").exists()
    before = {p: p.read_bytes() for p in root.rglob("*.json")}
    again = await grading._grade(state, config, root)
    assert again["blocked_reason"] and charged == ["a"]
    assert all(p.read_bytes() == raw for p, raw in before.items())


@pytest.mark.asyncio
async def test_serial_transport_releases_reservation_to_actual_usage(grading_case, monkeypatch):
    import asyncio

    root, state, config, martian = grading_case
    config.suite_options["max_judge_usd"] = 2.62
    monkeypatch.setattr(
        grading,
        "verify_allowance",
        lambda *a: {"max_judge_usd": 2.62, "existing_model_credit_usd": 3},
    )
    active = peak = calls = 0

    async def transport(request):
        nonlocal active, peak, calls
        active += 1
        peak = max(active, peak)
        calls += 1
        await asyncio.sleep(0)
        active -= 1
        return httpx.Response(
            200,
            json={
                "model": "claude-opus-4-5-20251101",
                "usage": {"prompt_tokens": 1000, "completion_tokens": 10},
            },
        )

    async def pipeline(projected, gold, client, *args, **kwargs):
        client._client._mounts.clear()
        client._client._transport = httpx.MockTransport(transport)
        await asyncio.gather(
            *[
                client._client.post(
                    "https://judge.example/v1", json={"model": "claude-opus-4-5-20251101"}
                )
                for _ in range(2)
            ]
        )
        return {"status": "complete", "attempt": projected[0]}

    monkeypatch.setattr(martian, "run_pipeline", pipeline)
    result = await grading._grade(state, config, root)
    assert result["complete"] and result["graded_attempts"] == ["a", "b"]
    assert peak == 1 and calls == 4
    for path in root.rglob("judge-http.json"):
        entries = json.loads(path.read_text())
        assert all(
            e["reserved_cost_usd"] == 2.60 and e["reported_cost_usd"] == 0.00525 for e in entries
        )
        assert all("max_tokens" not in e["body"] for e in entries)


def test_usd_gate_rejects_unverified_cap_or_unsupported_model(grading_case):
    from search_eval.core import EvalError

    root, state, config, _ = grading_case
    with pytest.raises(EvalError, match="differs"):
        grading._JudgeBudget(
            config, {"max_judge_usd": 9, "existing_model_credit_usd": 10}, state, root
        )
    with pytest.raises(EvalError, match="exceeds"):
        grading._JudgeBudget(
            config, {"max_judge_usd": 10, "existing_model_credit_usd": 9}, state, root
        )
    budget = grading._JudgeBudget(
        config, {"max_judge_usd": 10, "existing_model_credit_usd": 10}, state, root
    )
    with pytest.raises(EvalError, match="Opus 4.5 only"):
        budget.admit({"model": "different-model"})
    assert budget.spent == 0


@pytest.mark.asyncio
async def test_real_pairwise_pipeline_waits_before_starting_request_timeout(tmp_path, monkeypatch):
    """Queueing twenty pairs must not consume any pair's upstream call deadline."""
    import asyncio

    from search_eval.suites import martian

    folder = tmp_path / "attempts/a"
    folder.mkdir(parents=True)
    (folder / "github.json").write_text("{}")
    gold = [{"comment": f"Bug {i}", "severity": "P1", "category": "bug"} for i in range(10)]
    manifest = {
        "tasks": [
            {
                "id": "t",
                "role": "control",
                "gold": gold,
                "fixed_target": {"comment": "Fixed bug", "severity": "P1"},
            }
        ]
    }
    monkeypatch.setattr(grading, "ROOT", tmp_path)
    monkeypatch.setattr(grading, "frozen_manifest", lambda *args: manifest)
    monkeypatch.setattr(
        grading,
        "verify_allowance",
        lambda *args: {
            "max_judge_usd": 10,
            "existing_model_credit_usd": 10,
        },
    )
    monkeypatch.setenv("MARTIAN_API_KEY", "fixture-secret")
    monkeypatch.setattr(martian, "gold_records", lambda: {})
    monkeypatch.setattr(
        martian,
        "project_github_pages",
        lambda raw: {
            "comments": [{"body": "Two substantive bugs in this test review."}],
        },
    )
    monkeypatch.setattr(martian.upstream("judge"), "LLM_CALL_TIMEOUT", 0.3)
    active = peak = calls = 0

    async def transport(request):
        nonlocal active, peak, calls
        active += 1
        peak = max(peak, active)
        calls += 1
        number = calls
        try:
            await asyncio.sleep(0.03)
            answer = (
                {"issues": ["First substantive bug", "Second substantive bug"]}
                if number == 1
                else {"groups": [[0], [1]]}
                if number == 2
                else {"match": False, "confidence": 1.0, "reasoning": "Distinct bugs"}
            )
            return httpx.Response(
                200,
                json={
                    "id": f"call-{number}",
                    "object": "chat.completion",
                    "created": 1,
                    "model": "claude-opus-4-5-20251101",
                    "choices": [
                        {
                            "index": 0,
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": json.dumps(answer),
                            },
                        }
                    ],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 5},
                },
            )
        finally:
            active -= 1

    class MockClient(httpx.AsyncClient):
        def __init__(self, **kwargs):
            super().__init__(transport=httpx.MockTransport(transport), **kwargs)

    monkeypatch.setattr(grading.httpx, "AsyncClient", MockClient)
    config = Experiment.model_validate(
        {
            "run_id": "test",
            "suite": "martian",
            "manifest": "manifest.json",
            "baseline": "a",
            "arms": [{"id": "a"}],
            "profiles": {},
            "limits": {"max_attempts": 1},
            "suite_options": {
                "max_judge_calls_per_attempt": 30,
                "max_judge_usd": 10,
                "judge": {
                    "model": "claude-opus-4-5-20251101",
                    "temperature": 0.0,
                    "structured_output": False,
                    "protocol_variant_accepted": True,
                    "base_url": "https://judge.example/v1",
                },
            },
        }
    )
    result = await grading._grade(
        {
            "attempts": [
                {
                    "id": "a",
                    "task_id": "t",
                    "status": "completed",
                    "artifact_dir": "attempts/a",
                }
            ]
        },
        config,
        tmp_path,
    )
    assert result["complete"] and result["failed_attempts"] == []
    saved = json.loads((folder / "martian-grading.json").read_text())
    assert saved["evaluation"]["errors_count"] == 0 and len(saved["pairs"]) == 20
    assert len(saved["fixed_target"]["pairs"]) == 2
    assert saved["dedup_groups"] == [[0], [1]]
    assert peak == 1 and calls == 24
    assert all(e["status"] == 200 for e in json.loads((folder / "judge-http.json").read_text()))
