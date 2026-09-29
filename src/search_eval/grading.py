"""Explicitly authorized original Martian model grading with raw transport evidence."""

import asyncio
import hashlib
import json
import os
from decimal import Decimal
from pathlib import Path

import httpx
from openai import AsyncOpenAI

from .core import (
    ROOT,
    EvalError,
    Experiment,
    atomic_json,
    frozen_manifest,
    lock,
    verify_allowance,
    verify_run_code,
)


def grade_live(run_dir: Path):
    state = json.loads((run_dir / "state.json").read_text())
    config = Experiment.model_validate(state["config"])
    verify_run_code(state, ROOT)
    if config.suite != "martian":
        raise EvalError("DevDex grading is deterministic; use report, not grade")
    with lock(ROOT / ".gateway/runner.lock"):
        verify_allowance(config, ROOT, len(state["attempts"]))
        return asyncio.run(_grade(state, config, run_dir))


async def _grade(state, config, run_dir):
    from .suites import martian

    manifest = frozen_manifest(run_dir, state)
    settings = martian.judge_settings(config.suite_options, manifest)
    problems = judge_problems(config, manifest)
    if problems:
        raise EvalError("\n".join(problems))
    from .effective_inputs import resolve_judge

    resolved_judge = resolve_judge(settings)
    base_url = resolved_judge["base_url"]
    frozen_judge = state.get("input_identity", {}).get("effective_inputs", {}).get("judge")
    if frozen_judge is not None and resolved_judge != frozen_judge:
        raise EvalError("Judge transport/settings differ from frozen effective inputs")
    key = os.getenv("MARTIAN_API_KEY", "")
    if not key:
        raise EvalError("Set verified judge.base_url and MARTIAN_API_KEY")
    tasks = {t["id"]: t for t in manifest["tasks"]}
    originals = martian.gold_records()
    max_calls = config.suite_options.get("max_judge_calls_per_attempt")
    graded, failed = [], []
    blocked = None
    budget = None
    transport_lock = asyncio.Lock()

    class SerializedClient(httpx.AsyncClient):
        async def send(self, *args, **kwargs):
            async with transport_lock:
                return await super().send(*args, **kwargs)

    for attempt in state["attempts"]:
        if attempt["status"] != "completed":
            continue
        folder = run_dir / attempt["artifact_dir"]
        target = folder / "martian-grading.json"
        ledger = folder / "judge-http.json"
        failure_path = folder / "martian-grading-failure.json"
        if failure_path.exists():
            failure = json.loads(failure_path.read_text())
            if (
                failure.get("status") != "failed"
                or failure.get("retry_allowed") is not False
                or failure.get("attempt_id") != attempt["id"]
                or failure.get("ledger_sha256") != _file_hash(ledger)
                or failure.get("cache_sha256") != _file_hash(target)
            ):
                raise EvalError("Terminal grading failure evidence changed; reconcile manually")
            failed.append({"attempt_id": attempt["id"], **failure})
            continue
        entries = []
        phase = "read_saved_inputs"
        admission_error = None
        try:
            entries = json.loads(ledger.read_text()) if ledger.exists() else []
            if not isinstance(entries, list):
                raise ValueError("Grading ledger must be a list")
            raw = json.loads((folder / "github.json").read_text())
            projected = martian.project_github_pages(raw)["comments"]
            task = tasks[attempt["task_id"]]
            gold, fixed_target = martian.evaluator_targets(task, originals)
            if target.exists():
                phase = "replay_saved_cache"
                await martian.replay_pipeline(
                    json.loads(target.read_text()),
                    projected,
                    gold,
                    settings,
                    fixed_target=fixed_target,
                )
                graded.append(attempt["id"])
                continue
            if entries:
                phase = "prior_incomplete_grading"
                raise EvalError("Prior charged-call evidence cannot be automatically replayed")

            async def on_request(request):
                nonlocal admission_error, budget
                if len(entries) >= max_calls:
                    raise EvalError("Judge call cap exhausted")
                try:
                    allowance = verify_allowance(config, ROOT, len(state["attempts"]))
                    if budget is None:
                        budget = _JudgeBudget(config, allowance, state, run_dir)
                    budget.verify_allowance(allowance)
                    budget.admit(json.loads(request.content))
                except (EvalError, ValueError, OSError) as exc:
                    # An allowance failure blocks the batch, including later untouched tasks.
                    admission_error = f"Run judge admission blocked: {exc}"
                    raise
                request.extensions["search_eval_ledger_index"] = len(entries)
                entries.append(
                    {
                        "method": request.method,
                        "url": str(request.url),
                        "body": json.loads(request.content),
                        "status": "reserved",
                        "reserved_cost_usd": float(_JudgeBudget.RESERVE),
                    }
                )
                atomic_json(ledger, entries)

            async def on_response(response):
                await response.aread()
                # Concurrent matching responses retain their original request identity.
                index = response.request.extensions["search_eval_ledger_index"]
                entries[index]["status"] = response.status_code
                try:
                    body = response.json()
                except ValueError:
                    entries[index].update(
                        {"response_text": response.text, "response_json_error": True}
                    )
                    atomic_json(ledger, entries)
                    raise EvalError("Judge returned a non-JSON HTTP response") from None
                entries[index]["response"] = body
                charge = _JudgeBudget.response_cost(entries[index])
                if charge is not None:
                    entries[index]["reported_cost_usd"] = float(charge)
                    budget.settle(charge)
                atomic_json(ledger, entries)
                if (
                    response.status_code == 200
                    and settings.get("model")
                    and (not isinstance(body, dict) or body.get("model") != settings["model"])
                ):
                    raise EvalError("Judge returned an unexpected or missing model identity")

            phase = "original_pipeline"
            async with SerializedClient(
                event_hooks={"request": [on_request], "response": [on_response]}
            ) as http:
                client = AsyncOpenAI(
                    base_url=base_url,
                    api_key=key,
                    http_client=http,
                    max_retries=resolved_judge["transport"]["sdk_max_retries"],
                    timeout=resolved_judge["transport"]["timeout_seconds"],
                )
                clients = martian.pipeline_clients(client, settings)

                def before_judging(required):
                    if len(entries) + required > max_calls:
                        raise EvalError("Complete judge matrix exceeds the remaining call budget")

                saved = await martian.run_pipeline(
                    projected,
                    gold,
                    *clients,
                    settings=settings,
                    fixed_target=fixed_target,
                    before_judging=before_judging,
                )
                if admission_error:
                    raise EvalError(admission_error)
                atomic_json(target, saved)
                graded.append(attempt["id"])
        except Exception as exc:
            # Never overwrite partial transport evidence or retry an uncertain charged call.
            # Cancellation/process death leaves the ledger for the next run to quarantine.
            if not admission_error or entries:
                ambiguous = (
                    (ledger.exists() and phase == "read_saved_inputs")
                    or not isinstance(entries, list)
                    or any(
                        not isinstance(e, dict) or e.get("status") == "reserved" for e in entries
                    )
                )
                failure = {
                    "status": "failed",
                    "attempt_id": attempt["id"],
                    "outcome": "unknown" if ambiguous else "failed",
                    "phase": phase,
                    "reason": f"{phase}: {type(exc).__name__}; no automatic retry",
                    "retry_allowed": False,
                    "ledger_sha256": _file_hash(ledger),
                    "cache_sha256": _file_hash(target),
                    "settings": settings,
                }
                atomic_json(failure_path, failure)
                failed.append(failure)
            if admission_error:
                blocked = admission_error
                break
    return {
        "graded_attempts": graded,
        "failed_attempts": failed,
        "complete": not failed and blocked is None and len(graded) == len(state["attempts"]),
        "blocked_reason": blocked,
        "report_command": f"search-eval report --run {config.run_id}",
    }


def _file_hash(path: Path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def judge_problems(config, manifest, allowance=None):
    """One readiness policy for doctor and live grading; collect all actionable errors."""
    from .effective_inputs import judge_endpoint, resolve_judge
    from .suites.martian import judge_settings

    settings = judge_settings(config.suite_options, manifest)
    problems = []
    if (
        settings.get("historical_structured_output_verified") is not True
        and settings.get("protocol_variant_accepted") is not True
    ):
        problems.append(
            "Historical judge settings require verification or accepted protocol variant"
        )
    calls = config.suite_options.get("max_judge_calls_per_attempt")
    if type(calls) is not int or calls < 1:
        problems.append("Freeze positive integer max_judge_calls_per_attempt")
    try:
        judge_endpoint(resolve_judge(settings).get("base_url"))
    except ValueError as exc:
        problems.append(str(exc))
    if allowance is not None:
        try:
            validate_judge_budget(config, allowance)
        except (ValueError, EvalError, TypeError) as exc:
            problems.append(str(exc))
    return problems


def validate_judge_budget(config, allowance):
    """Validate run funding without constructing live grading state or reading ledgers."""
    cap = config.suite_options.get("max_judge_usd")
    if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
        raise EvalError("Freeze positive max_judge_usd before new judge calls")
    cap = Decimal(str(cap))
    _verify_prepaid_cap(cap, allowance)
    return cap


def _verify_prepaid_cap(cap, allowance):
    if (
        allowance.get("max_judge_usd") is None
        or allowance.get("existing_model_credit_usd") is None
    ):
        raise EvalError("Verified allowance lacks the run's USD cap or prepaid balance")
    if Decimal(str(allowance["max_judge_usd"])) != cap or cap > Decimal(
        str(allowance["existing_model_credit_usd"])
    ):
        raise EvalError("Run USD cap exceeds or differs from verified prepaid allowance")


class _JudgeBudget:
    """Bound standard Opus 4.5 calls using existing request ledgers only."""

    MODEL = "claude-opus-4-5-20251101"
    RESERVE = Decimal("2.60")  # 200K input at $5/M + 64K output at $25/M.

    def __init__(self, config, allowance, state, run_dir):
        self.cap = validate_judge_budget(config, allowance)
        self.spent = Decimal(0)
        for attempt in state["attempts"]:
            path = run_dir / attempt["artifact_dir"] / "judge-http.json"
            for entry in json.loads(path.read_text()) if path.exists() else []:
                cost = self.response_cost(entry)
                self.spent += self.RESERVE if cost is None else cost

    def verify_allowance(self, allowance):
        _verify_prepaid_cap(self.cap, allowance)

    def admit(self, body):
        if body.get("model") != self.MODEL:
            raise EvalError("USD bound supports standard dated Opus 4.5 only")
        if self.spent + self.RESERVE > self.cap:
            raise EvalError("Run judge USD cap cannot cover the next $2.60 reservation")
        self.spent += self.RESERVE

    def settle(self, cost):
        self.spent += cost - self.RESERVE

    @classmethod
    def response_cost(cls, entry):
        body = entry.get("response")
        if (
            entry.get("status") != 200
            or not isinstance(body, dict)
            or body.get("model") != cls.MODEL
        ):
            return None
        usage = body.get("usage", {})
        incoming, outgoing = usage.get("prompt_tokens"), usage.get("completion_tokens")
        if type(incoming) is not int or type(outgoing) is not int:
            return None
        if not (0 <= incoming <= 200_000 and 0 <= outgoing <= 64_000):
            return None
        return (Decimal(incoming) * 5 + Decimal(outgoing) * 25) / 1_000_000
