"""Pinned Martian transformations and scoring. Gold never enters visible task records.

No API clients are instantiated here. run_pipeline accepts explicitly configured
upstream CandidateExtractor, DedupLLM and LLMJudge instances from an authorized caller.
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import inspect
import json
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

TASK_KIND = "pr_review"
REQUIRED_ENV = ("MARTIAN_API_KEY",)
ALLOWANCE_SERVICES = ("martian_judge",)
LIVE_ALLOWED = True

ROOT = Path(__file__).resolve().parents[3]
UPSTREAM = ROOT / "vendor/martian/offline"
REVISION = "e616e849755441da38f18bf3adba2c9583b03803"
_MODULES: dict[str, Any] = {}


def upstream(name: str):
    paths = {
        "score": "analysis/score_profiles.py",
        "collect": "code_review_benchmark/step1_download_prs.py",
        "extract": "code_review_benchmark/step2_extract_comments.py",
        "dedup": "code_review_benchmark/step2_5_dedup_candidates.py",
        "judge": "code_review_benchmark/step3_judge_comments.py",
    }
    if name not in _MODULES:
        spec = importlib.util.spec_from_file_location(f"martian_{name}", UPSTREAM / paths[name])
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        _MODULES[name] = module
    return _MODULES[name]


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
    ).hexdigest()


def gold_records() -> dict[str, dict]:
    return {
        row["url"]: row
        for path in sorted((UPSTREAM / "golden_comments").glob("*.json"))
        for row in json.loads(path.read_text())
    }


def pipeline_identity() -> dict:
    files = [
        "analysis/score_profiles.py",
        "code_review_benchmark/step1_download_prs.py",
        "code_review_benchmark/step2_extract_comments.py",
        "code_review_benchmark/step2_5_dedup_candidates.py",
        "code_review_benchmark/step3_judge_comments.py",
    ]
    return {
        "revision": REVISION,
        "files": {p: hashlib.sha256((UPSTREAM / p).read_bytes()).hexdigest() for p in files},
    }


def scorer_identity() -> dict:
    return {"name": "martian-original", **pipeline_identity()}


def load_tasks(manifest_path: Path) -> list[dict]:
    manifest = json.loads(manifest_path.read_text())
    tasks = []
    for row in manifest.get("tasks", []):
        if row.get("snapshot_status") not in {"verified", "reconstructed", "authored"}:
            raise ValueError(f"Unverified Martian snapshot: {row.get('id')}")
        visible = row["input"]
        required = (
            {"base_files", "head_files", "title", "body"}
            if row.get("snapshot_status") == "authored"
            else {"repo", "base_sha", "head_sha", "base_tree", "head_tree", "title", "body"}
        )
        if "head_overrides" in visible and row["role"] == "control":
            required.add("head_overrides")
        if not required <= visible.keys():
            raise ValueError("Verified Martian snapshot lacks execution fields")
        # Explicit allowlist: never forward gold, source-review metadata or arbitrary keys.
        tasks.append(
            {
                "id": row["id"],
                "role": row["role"],
                "kind": "pr_review",
                "input": {k: visible[k] for k in sorted(required)},
            }
        )
    if len({t["id"] for t in tasks}) != len(tasks):
        raise ValueError("Duplicate Martian task IDs")
    return tasks


def project_github_pages(raw: dict) -> dict:
    """Reproduce upstream default first30 records per endpoint BEFORE bot filtering.

    Collector must use per_page30 and retain page boundaries/server ordering. Fail
    closed on missing endpoints or incompatible pagination instead of silently
    treating failed collection as a completed zero-comment review.
    """
    output = []
    omissions = {}
    for endpoint in ("review_comments", "reviews", "issue_comments"):
        pages = raw.get(endpoint)
        if not isinstance(pages, list) or not pages:
            raise ValueError(f"Missing successful GitHub page: {endpoint}")
        if [p.get("page") for p in pages] != list(range(1, len(pages) + 1)):
            raise ValueError(f"Noncontiguous page order: {endpoint}")
        if any(
            p.get("per_page") != 30
            or not isinstance(p.get("items"), list)
            or p.get("error")
            or len(p["items"]) > 30
            for p in pages
        ):
            raise ValueError(f"Invalid default-page projection: {endpoint}")
        omissions[endpoint] = sum(len(p["items"]) for p in pages[1:])
        for c in pages[0]["items"]:
            if not upstream("collect")._is_bot(c.get("user")):
                continue
            if endpoint == "reviews" and not c.get("body"):
                continue
            inline = endpoint == "review_comments"
            output.append(
                {
                    "path": c.get("path") if inline else None,
                    "line": (c.get("line") or c.get("original_line")) if inline else None,
                    "body": c.get("body"),
                    "created_at": c.get("submitted_at")
                    if endpoint == "reviews"
                    else c.get("created_at"),
                }
            )
    return {
        "comments": output,
        "text": upstream("extract").get_all_comment_text(output),
        "omitted_records": omissions,
    }


def grading_fingerprint(comments: list, gold: list, settings: dict) -> str:
    return digest(
        {"comments": comments, "gold": gold, "settings": settings, "pipeline": pipeline_identity()}
    )


def _fixed_fingerprint(comments, target, candidates, groups, settings):
    return digest(
        {
            "grading": grading_fingerprint(comments, [target], settings),
            "candidates": candidates,
            "dedup_groups": groups,
        }
    )


def _pair_lookup(pairs, expected):
    # Equal candidate text still represents distinct upstream judge calls. Preserve
    # every response occurrence instead of collapsing it into a text-keyed result.
    lookup = defaultdict(deque)
    for pair in pairs:
        key = (pair["golden"], pair["candidate"])
        response = pair["response"]
        if (
            response.get("error")
            or type(response.get("match")) is not bool
            or type(response.get("confidence")) not in (int, float)
        ):
            raise ValueError("Failed/malformed pairwise judge response")
        lookup[key].append(response)
    if Counter({key: len(values) for key, values in lookup.items()}) != Counter(expected):
        raise ValueError("Missing or extra pairwise decisions")
    return lookup


def _fixed_diagnostic(evaluation, pairs, candidates):
    # The flag comes from original matching semantics, including confidence handling.
    matched_candidates = {
        p["candidate"] for p in pairs if p["response"]["match"] and p["response"]["confidence"] > 0
    }
    return {
        "candidate_count": len(candidates),
        "target_matched": bool(evaluation["tp"]),
        "matched_candidate_count": len(matched_candidates),
    }


def judge_settings(suite_options, manifest):
    """Use the same settings precedence for live grading and offline replay."""
    return suite_options.get("judge", manifest.get("judge", {}))


def evaluator_targets(task, originals):
    """Resolve suite-owned gold and optional fixed target without changing either."""
    gold = originals[task["golden_url"]]["comments"] if task["role"] == "scored" else task["gold"]
    fixed_target = task.get("fixed_target") if task["role"] == "control" else None
    return gold, fixed_target


async def run_pipeline(
    comments: list,
    gold: list,
    extractor,
    deduper,
    judge,
    settings: dict,
    fixed_target: dict | None = None,
    before_judging=None,
) -> dict:
    """Use original extraction/dedup/matcher with no missing-stage fallbacks.

    Client methods: extract_from_comment(text), dedup_candidates(texts, prompt),
    match_comment(golden, candidate). Objects must expose model. Caller owns access,
    allowance, transport recording and persistence. The returned pairwise responses
    are original parsed responses; raw HTTP traces must be recorded by the caller's
    client transport. This function does not claim historical model availability.
    before_judging(count) is an optional sync/async admission gate. It runs after
    extraction/dedup and may raise to block the entire planned matcher matrix.
    """
    if not settings.get("model") or any(
        c.model != settings["model"] for c in (extractor, deduper, judge)
    ):
        raise ValueError("Explicit identical extraction/dedup/judge model required")
    if settings.get("temperature") != 0.0:
        raise ValueError("Original Martian temperature must be zero")
    if fixed_target is not None and (
        not isinstance(fixed_target, dict) or not fixed_target.get("comment")
    ):
        raise ValueError("Invalid fixed target")
    if bool(getattr(judge, "structured_output", False)) != settings.get("structured_output"):
        raise ValueError("Judge structured-output settings mismatch")
    result = await extractor.extract_from_comment(
        upstream("extract").get_all_comment_text(comments)
    )
    if result.get("error") or not isinstance(result.get("issues"), list):
        raise ValueError("Extraction failed; no raw-comment fallback")
    candidates = result["issues"]
    if any(not isinstance(c, str) for c in candidates):
        raise ValueError("Malformed extraction candidates")
    candidates = [c for c in candidates if c]  # upstream get_candidates drops empty text
    groups = (
        [[i] for i in range(len(candidates))]
        if len(candidates) < 2
        else await deduper.dedup_candidates(candidates, upstream("dedup").DEDUP_PROMPT)
    )
    if (
        groups is None
        or upstream("dedup")._parse_groups_response(json.dumps({"groups": groups}), len(candidates))
        is None
    ):
        raise ValueError("Dedup failed; no singleton fallback")
    if before_judging is not None:
        # Includes supplemental fixed-target calls and duplicate text occurrences.
        admission = before_judging((len(gold) + (fixed_target is not None)) * len(candidates))
        if inspect.isawaitable(admission):
            await admission
    pairs = []
    # Wait before upstream starts its per-call timeout. The budget transport is serial.
    judge_lock = asyncio.Lock()

    class RecordingJudge:
        def __init__(self, records):
            self.records = records

        async def match_comment(self, golden, candidate):
            record = {"golden": golden, "candidate": candidate}
            self.records.append(record)
            async with judge_lock:
                response = await judge.match_comment(golden, candidate)
            record["response"] = response
            return response

    evaluation = await upstream("judge").evaluate_review(
        RecordingJudge(pairs), gold, candidates, groups
    )
    if evaluation.get("errors_count") or (evaluation.get("skipped") and gold):
        raise ValueError("Pairwise judging incomplete")
    saved = {
        "status": "complete",
        "settings": settings,
        "extraction": result,
        "candidates": candidates,
        "dedup_groups": groups,
        "pairs": pairs,
        "fingerprint": grading_fingerprint(comments, gold, settings),
        "evaluation": evaluation,
        "pipeline": pipeline_identity(),
    }
    if fixed_target is not None:
        if not isinstance(fixed_target, dict) or not fixed_target.get("comment"):
            raise ValueError("Invalid fixed target")
        target_pairs = []

        target_evaluation = await upstream("judge").evaluate_review(
            RecordingJudge(target_pairs), [fixed_target], candidates, groups
        )
        if target_evaluation.get("errors_count") or target_evaluation.get("skipped"):
            raise ValueError("Fixed-target judging incomplete")
        _pair_lookup(target_pairs, [(fixed_target["comment"], c) for c in candidates])
        saved["fixed_target"] = {
            "fingerprint": _fixed_fingerprint(comments, fixed_target, candidates, groups, settings),
            "pairs": target_pairs,
            "diagnostic": _fixed_diagnostic(target_evaluation, target_pairs, candidates),
        }
    return saved


async def replay_pipeline(
    saved: dict, comments: list, gold: list, settings: dict, fixed_target: dict | None = None
) -> dict:
    if saved.get("status") != "complete" or saved.get("settings") != settings:
        raise ValueError("Incomplete grading or changed judge settings")
    if saved.get("fingerprint") != grading_fingerprint(comments, gold, settings):
        raise ValueError("Stale grading fingerprint")
    extracted = saved.get("extraction", {})
    candidates = saved.get("candidates")
    if (
        extracted.get("error")
        or not isinstance(candidates, list)
        or [c for c in extracted.get("issues", []) if c] != candidates
    ):
        raise ValueError("Missing successful extraction")
    groups = saved.get("dedup_groups")
    if (
        upstream("dedup")._parse_groups_response(json.dumps({"groups": groups}), len(candidates))
        is None
    ):
        raise ValueError("Missing successful dedup")
    pairs = saved.get("pairs", [])
    expected = [(g["comment"], c) for g in gold for c in candidates]
    lookup = _pair_lookup(pairs, expected)

    class ReplayJudge:
        async def match_comment(self, golden, candidate):
            return lookup[(golden, candidate)].popleft()

    result = await upstream("judge").evaluate_review(ReplayJudge(), gold, candidates, groups)
    if result.get("errors_count") or (result.get("skipped") and gold):
        raise ValueError("Incomplete evaluation")
    if fixed_target is not None:
        supplemental = saved.get("fixed_target", {})
        if supplemental.get("fingerprint") != _fixed_fingerprint(
            comments, fixed_target, candidates, groups, settings
        ):
            raise ValueError("Missing or stale fixed-target fingerprint")
        target_pairs = supplemental.get("pairs", [])
        target_lookup = _pair_lookup(
            target_pairs, [(fixed_target["comment"], c) for c in candidates]
        )

        class TargetReplayJudge:
            async def match_comment(self, golden, candidate):
                return target_lookup[(golden, candidate)].popleft()

        target_evaluation = await upstream("judge").evaluate_review(
            TargetReplayJudge(), [fixed_target], candidates, groups
        )
        if target_evaluation.get("errors_count") or target_evaluation.get("skipped"):
            raise ValueError("Incomplete fixed-target evaluation")
        if supplemental.get("diagnostic") != _fixed_diagnostic(
            target_evaluation, target_pairs, candidates
        ):
            raise ValueError("Fixed-target diagnostic differs from original pairwise replay")
    return result


def grade(attempts: list[dict], output_dir: Path, config: dict) -> dict:
    """Offline grading only; save native scores per role/configuration/repeat."""
    manifest_path = Path(
        config.get("manifest") or config.get("suite_options", {}).get("manifest", "")
    )
    manifest = json.loads(manifest_path.read_text())
    task_map = {
        t["id"]: t
        for t in manifest.get("tasks", [])
        if config.get("role") is None or t["role"] == config["role"]
    }
    settings = judge_settings(config.get("suite_options", {}), manifest)
    original = gold_records()
    categories = {g["comment"]: g["category"] for row in original.values() for g in row["comments"]}
    categories.update(
        {g["comment"]: g["category"] for t in task_map.values() for g in t.get("gold", [])}
    )
    diagnostics, per_task = [], []
    grouped = defaultdict(list)
    for attempt in attempts:
        grouped[(attempt["role"], attempt["configuration"], attempt["repeat"])].append(attempt)
    rows, failures, omissions, artifacts = [], [], {}, []
    configured = config.get("configurations", [])
    arm_ids = {c["id"] if isinstance(c, dict) else c for c in configured}
    if arm_ids:
        expected_groups = {
            (t["role"], arm, repeat)
            for t in task_map.values()
            for arm in arm_ids
            for repeat in range(1, config.get("repeats", 1) + 1)
        }
        if set(grouped) != expected_groups:
            failures.append("Missing or unexpected configuration/repeat groups")
    for (role, arm, repeat), group in sorted(grouped.items()):
        evaluations, completed_gold = {}, []
        expected_ids = {t["id"] for t in task_map.values() if t["role"] == role}
        seen = [a["task_id"] for a in group]
        reasons = []
        if set(seen) != expected_ids or len(seen) != len(set(seen)):
            reasons.append("Missing, extra or duplicate scheduled task")
        for a in group:
            if a["status"] != "completed":
                reasons.append(f"{a['id']}: runtime {a['status']}")
                continue
            try:
                task = task_map[a["task_id"]]
                url = task["golden_url"]
                gold, fixed_target = evaluator_targets(task, original)
                if url in evaluations:
                    raise ValueError("Duplicate golden URL within group")
                raw = json.loads((Path(a["artifact_dir"]) / "github.json").read_text())
                projection = project_github_pages(raw)
                failure_path = Path(a["artifact_dir"]) / "martian-grading-failure.json"
                if failure_path.is_file():
                    failure = json.loads(failure_path.read_text())
                    raise ValueError(
                        f"Terminal grading failure: {failure.get('reason', 'unavailable')}"
                    )
                saved = json.loads((Path(a["artifact_dir"]) / "martian-grading.json").read_text())
                ev = asyncio.run(
                    replay_pipeline(
                        saved, projection["comments"], gold, settings, fixed_target=fixed_target
                    )
                )
                if fixed_target is not None:
                    diagnostics.append(
                        {
                            "task_id": task["id"],
                            "role": role,
                            "configuration": arm,
                            "repeat": repeat,
                            "scope": "fixed_target",
                            "complete": True,
                            **saved["fixed_target"]["diagnostic"],
                        }
                    )
                if not gold:
                    if role == "scored":
                        raise ValueError("Original scored tasks must contain gold")
                    diagnostics.append(
                        {
                            "task_id": task["id"],
                            "role": role,
                            "configuration": arm,
                            "repeat": repeat,
                            "candidate_count": len(saved["candidates"]),
                            "complete": True,
                            "scope": "completion_only",
                            "note": "No gold; original scorer skips this task. Findings require manual review.",
                        }
                    )
                    continue
                for profile in ("core", "strict", "all"):
                    one = upstream("score").score_tools(
                        {url: {"experiment": ev}}, categories, profile, 2.0
                    )["experiment"]
                    per_task.append(
                        {
                            "attempt_id": a["id"],
                            "task_id": task["id"],
                            "scope": profile,
                            **{
                                ("F2" if key == "fbeta" else key): value
                                for key, value in one.items()
                            },
                        }
                    )
                evaluations[url] = {"experiment": ev}
                completed_gold.extend(gold)
                omissions[a["id"]] = projection["omitted_records"]
            except (KeyError, ValueError, OSError, TypeError) as e:
                reasons.append(f"{a['id']}: {e}")
        profiles = ("core", "strict", "all") if evaluations or reasons else ()
        for profile in profiles:
            scored = upstream("score").score_tools(evaluations, categories, profile, 2.0)
            metrics = scored.get(
                "experiment",
                {
                    "tp": 0,
                    "fp": 0,
                    "fn": 0,
                    "prs": 0,
                    "precision": 0,
                    "recall": 0,
                    "f1": 0,
                    "fbeta": 0,
                },
            )
            denom = sum(
                g["category"] in upstream("score").PROFILE_CATEGORIES[profile]
                for g in completed_gold
            )
            if metrics["tp"] + metrics["fn"] != denom or metrics["prs"] != len(evaluations):
                reasons.append(f"{profile}: denominator reconciliation failed")
            rows.append(
                {
                    "role": role,
                    "configuration": arm,
                    "repeat": repeat,
                    "profile": profile,
                    "complete": not reasons,
                    "metrics": metrics,
                    "denominator": denom,
                    "scheduled_tasks": len(expected_ids),
                    "graded_tasks": len(evaluations),
                    "scope": "original_benchmark" if role == "scored" else "diagnostic",
                }
            )
        failures.extend(reasons)
        safe_id = digest([role, arm, repeat])[:16]
        path = output_dir / f"martian-evaluations-{safe_id}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(evaluations, indent=2) + "\n")
        artifacts.append(str(path))
    if not attempts:
        failures.append("No scheduled attempts; no benchmark result")
    series = []
    for row in rows:
        row["group_complete"] = row["complete"]
        if failures:
            row["complete"] = False
            row["scope"] = "partial_diagnostic"
        row["score_use"] = "comparison" if row["complete"] else "diagnostic_only"
        for key, value in row["metrics"].items():
            series.append(
                {
                    "configuration": row["configuration"],
                    "repeat": row["repeat"],
                    "role": row["role"],
                    "metric": "F2" if key == "fbeta" else key,
                    "value": value if row["complete"] else None,
                    "unit": "count" if key in {"tp", "fp", "fn", "prs"} else "fraction",
                    "direction": "lower" if key in {"fp", "fn"} else "higher",
                    "denominator": row["denominator"],
                    "scope": row["profile"],
                    "primary": key == "fbeta"
                    and row["profile"] == "core"
                    and row["role"] == "scored",
                    "complete": row["complete"] and not failures,
                }
            )
    result = {
        "suite": "martian",
        "diagnostics": diagnostics,
        "per_task": per_task,
        "limitations": [
            "Predeclared headline policy: all scheduled tasks must have valid native grading. Any missing or failed outcome suppresses every headline value. Successful per-task scores remain diagnostic only; no post-hoc complete-case replacement is made.",
            "Reconstructed pilot inputs use frozen source trees, titles from the gold metadata and a neutral PR body. These are identical across arms but differ from historical benchmark PR descriptions. Original scoring is preserved; historical leaderboard comparability is not claimed.",
        ],
        "task_annotations": [
            {
                "task_id": task["id"],
                "documentation_relevance": task.get("documentation_relevance", "UNKNOWN"),
                "source": task.get("documentation_relevance_source"),
            }
            for task in task_map.values()
        ],
        "report_series": series,
        "complete": bool(attempts) and not failures,
        "primary_metric": "core.fbeta",
        "metrics": rows,
        "failures": failures,
        "omitted_records": omissions,
        "artifacts": artifacts,
        "pipeline": pipeline_identity(),
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "martian-result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def reference_parity() -> dict:
    """Replay a real published evaluation artifact, without inference/network."""
    p = UPSTREAM / "results/anthropic_claude-opus-4-5-20251101/evaluations.json"
    evaluations = json.loads(p.read_text())
    categories = {
        g["comment"]: g["category"] for row in gold_records().values() for g in row["comments"]
    }
    return {
        "reference": str(p.relative_to(ROOT)),
        "sha256": hashlib.sha256(p.read_bytes()).hexdigest(),
        "model": "anthropic/claude-opus-4-5-20251101",
        "scores": {
            profile: upstream("score").score_tools(evaluations, categories, profile, 2.0)
            for profile in ("core", "strict", "all")
        },
    }


def pipeline_clients(client, settings: dict) -> tuple:
    """Bind an authorized recording AsyncOpenAI client without .env/default fallback.

    Caller supplies client transport, explicit endpoint, access and numeric allowance.
    This constructor performs no network call and reads no credentials.
    """
    if not settings.get("model") or settings.get("temperature") != 0.0:
        raise ValueError("Explicit judge model and original temperature0.0 required")
    objects = []
    for module, class_name in (
        ("extract", "CandidateExtractor"),
        ("dedup", "DedupLLM"),
        ("judge", "LLMJudge"),
    ):
        cls = getattr(upstream(module), class_name)
        obj = cls.__new__(cls)
        obj.client, obj.model = client, settings["model"]
        if module == "judge":
            if type(settings.get("structured_output")) is not bool:
                raise ValueError("Explicit structured_output required")
            obj.structured_output = settings["structured_output"]
        objects.append(obj)
    return configure_single_attempt_clients(tuple(objects))


def configure_single_attempt_clients(clients) -> tuple:
    """Bound the disclosed single-attempt protocol variant; no vendor file edits.

    Original prompts, parsing and matching remain in their original methods.
    Extractor/judge call_llm receive max_retries=1. Original dedup reads its module
    MAX_RETRIES, overridden to1 in this process. This differs from upstream retry
    defaults and must be disclosed in live settings. SDK/HTTP retries must be zero.
    """
    if len(clients) != 3:
        raise ValueError("Expected extractor, deduper and judge")
    for obj in clients:
        if getattr(obj.client, "max_retries", 0) != 0:
            raise ValueError("Underlying model transport retries must be disabled")

    def bounded(original):
        async def call(prompt, max_retries=1):
            if max_retries != 1:
                raise ValueError("Single-attempt protocol requires max_retries=1")
            return await original(prompt, max_retries=1)

        return call

    for obj in (clients[0], clients[2]):
        if not getattr(obj, "_single_attempt_configured", False):
            obj.call_llm = bounded(obj.call_llm)
            obj._single_attempt_configured = True
    upstream("dedup").MAX_RETRIES = 1
    return tuple(clients)


def grade_live(run_dir):
    """Suite-dispatched live grading; import locally to avoid a module cycle."""
    from search_eval.grading import grade_live as implementation

    return implementation(run_dir)
