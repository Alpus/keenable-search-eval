"""DevDex Docs inputs and its unchanged native URL scorer.

Only this grading module reads labels. The worker receives query text alone.
"""

from __future__ import annotations

import hashlib
import importlib
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
VENDOR = ROOT / "vendor" / "devdex"
SOURCE = VENDOR / "devdex" / "gt" / "docs_public.jsonl"
DEFAULT_MANIFEST = ROOT / "data" / "devdex-docs-manifest.json"
COMMIT = "24e60473887d33960bf155a9e73affcd07d288a3"
TASK_KIND = "developer_retrieval"
REQUIRED_ENV = ("ANTHROPIC_API_KEY",)
ALLOWANCE_SERVICES = ("devdex_model",)
LIVE_ALLOWED = True
METRICS = {"primary": "recall@10", "secondary": "MRR@10", "direction": "higher"}
SEED = "keenable-devdex-docs-2026-09-29-v1"


def scorer_identity() -> dict:
    files = ("devdex/scorer/report_metrics.py", "devdex/scorer/suite.py")
    return {
        "name": "devdex-docs-original",
        "revision": COMMIT,
        "files": {p: hashlib.sha256((VENDOR / p).read_bytes()).hexdigest() for p in files},
    }


def digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def source_rows() -> list[dict]:
    return [json.loads(line) for line in SOURCE.read_text().splitlines() if line.strip()]


def read_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    manifest = json.loads(Path(path).read_text())
    if manifest["source_commit"] != COMMIT:
        raise ValueError("DevDex source commit differs from the frozen protocol")
    if manifest["source_sha256"] != hashlib.sha256(SOURCE.read_bytes()).hexdigest():
        raise ValueError("DevDex source dataset hash changed")
    unsigned = {k: v for k, v in manifest.items() if k != "manifest_sha256"}
    if digest(unsigned) != manifest["manifest_sha256"]:
        raise ValueError("DevDex manifest hash changed")
    rows = {row["id"]: row for row in source_rows()}
    ids = [task["id"] for task in manifest["tasks"]]
    if len(set(ids)) != len(ids):
        raise ValueError("Duplicate DevDex task IDs")
    if set(ids) & {row["id"] for row in manifest["exclusions"]}:
        raise ValueError("Previously inspected DevDex examples entered the split")
    if Counter(task["role"] for task in manifest["tasks"]) != {"development": 5, "evaluation": 30}:
        raise ValueError("DevDex requires five development and thirty evaluation tasks")
    for task in manifest["tasks"]:
        if task["id"] not in rows or digest(rows[task["id"]]) != task["source_row_sha256"]:
            raise ValueError("DevDex task or label hash changed")
    return manifest


def load_tasks(manifest_path: Path = DEFAULT_MANIFEST) -> list[dict]:
    manifest = read_manifest(manifest_path)
    rows = {row["id"]: row for row in source_rows()}
    return [
        {
            "id": item["id"],
            "role": "scored" if item["role"] == "evaluation" else item["role"],
            "kind": TASK_KIND,
            "input": {"query": rows[item["id"]]["query"]},
        }
        for item in manifest["tasks"]
    ]


def native_modules():
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    return (
        importlib.import_module("devdex.scorer.report_metrics"),
        importlib.import_module("devdex.scorer.suite"),
    )


def score_native(records: list[dict]) -> dict | None:
    """Call upstream arithmetic and canonical matching; never discover run files."""
    report, suite = native_modules()
    meta = suite._docs_meta()
    if not meta:
        raise ValueError("DevDex gold metadata is absent")
    report.check_docs_coverage(records, meta, "exact selected records")

    def is_gold(url, record):
        probe = {"search_log": [{"results": [{"rank": 0, "url": url}]}]}
        return bool(suite._docs_rank(probe, meta.get(record["qid"])))

    def pool_test(record, engine):
        if record.get("search_log"):
            return bool(suite._docs_rank(record, meta.get(record["qid"])))
        return any(is_gold(url, record) for url in engine)

    return report.score_records(records, "docs", is_gold, pool_test)


def _missing_record(attempt: dict, reason: str) -> dict:
    return {
        "qid": attempt["task_id"],
        "sources": [],
        "search_log": [],
        "committed": False,
        "error": f"no_tool_calls: {reason}",
        "latency": None,
        "attempt_id": attempt["id"],
    }


def grade(attempts: list[dict], output_dir: Path, config: dict) -> dict:
    """Score the supplied schedule, including missing outputs in the denominator.

    Native scores are kept as audit evidence when dead runs exceed 10%, but public
    headline values are null. Historical files not named by the schedule are ignored.
    """
    manifest_path = Path(config.get("manifest_path", DEFAULT_MANIFEST))
    tasks = {task["id"]: task for task in load_tasks(manifest_path)}
    groups: dict[tuple, list] = defaultdict(list)
    seen = set()
    paths = set()
    artifacts = []
    for attempt in attempts:
        if attempt["status"] not in {"completed", "failed", "skipped", "timed_out"}:
            raise ValueError("DevDex grading requires every scheduled attempt to be terminal")
        key = (attempt["role"], attempt["configuration"], attempt["repeat"], attempt["task_id"])
        if key in seen:
            raise ValueError("Duplicate scheduled DevDex episode")
        seen.add(key)
        if attempt["task_id"] not in tasks or tasks[attempt["task_id"]]["role"] != attempt["role"]:
            raise ValueError("DevDex attempt is outside the frozen split")
        record_path = Path(attempt["artifact_dir"]).resolve() / "record.json"
        if record_path in paths:
            raise ValueError("DevDex attempts share an artifact directory")
        paths.add(record_path)
        if record_path.exists():
            record = json.loads(record_path.read_text())
            expected = {
                "attempt_id": attempt["id"],
                "qid": attempt["task_id"],
                "configuration": attempt["configuration"],
                "repeat": attempt["repeat"],
            }
            if any(record.get(field) != value for field, value in expected.items()):
                raise ValueError("DevDex record does not belong to this exact episode")
            artifacts.append(str(record_path))
        else:
            record = _missing_record(
                attempt, f"scheduled status={attempt['status']}; record absent"
            )
        groups[key[:3]].append((attempt, record))

    cells = []
    per_task = []
    for (role, configuration, repeat), values in sorted(groups.items()):
        records = [record for _, record in values]
        native = score_native(records)
        invalid = bool(native and native["invalid"])
        complete = sum(
            attempt["status"] == "completed" and not record.get("error")
            for attempt, record in values
        )
        expected_count = sum(task["role"] == role for task in tasks.values())
        suppressed = invalid or len(values) != expected_count
        cell = {
            "role": role,
            "configuration": configuration,
            "repeat": repeat,
            "scheduled": len(values),
            "expected_in_manifest": expected_count,
            "schedule_complete": len(values) == expected_count,
            "completed": complete,
            "missing_outputs": sum(
                not (Path(a["artifact_dir"]) / "record.json").exists() for a, _ in values
            ),
            "native_metrics": native,
            "headline": {
                "recall@10": None if suppressed else native["recall"],
                "MRR@10": None if suppressed else native["amrr"],
            },
            "suppressed": suppressed,
            "suppression_reason": (
                "dead_runs > 10%"
                if invalid
                else "Incomplete frozen schedule"
                if suppressed
                else None
            ),
        }
        cells.append(cell)
        for attempt, record in values:
            one = score_native([record])
            per_task.append(
                {
                    "attempt_id": attempt["id"],
                    "task_id": record["qid"],
                    "role": role,
                    "configuration": configuration,
                    "repeat": repeat,
                    "sources": (record.get("sources") or [])[:10],
                    "recall@10": one["recall"],
                    "MRR@10": one["amrr"],
                    "error": record.get("error"),
                    "dead": one["dead"],
                }
            )
    pairs = []
    by_task = defaultdict(dict)
    for row in per_task:
        by_task[(row["role"], row["repeat"], row["task_id"])][row["configuration"]] = row
    configs = sorted({a["configuration"] for a in attempts})
    if len(configs) == 2:
        for role, repeat in sorted({(a["role"], a["repeat"]) for a in attempts}):
            counts = Counter()
            for (r, rep, _), values in by_task.items():
                if (r, rep) != (role, repeat) or set(values) != set(configs):
                    continue
                left, right = (values[c] for c in configs)
                delta = left["recall@10"] - right["recall@10"]
                counts["left_wins" if delta > 0 else "right_wins" if delta < 0 else "ties"] += 1
            pairs.append(
                {
                    "role": role,
                    "repeat": repeat,
                    "left": configs[0],
                    "right": configs[1],
                    "paired_tasks": sum(counts.values()),
                    **counts,
                }
            )
    series = [
        {
            "configuration": cell["configuration"],
            "repeat": cell["repeat"],
            "role": cell["role"],
            "metric": metric,
            "value": value,
            "unit": "fraction",
            "direction": "higher",
            "denominator": cell["scheduled"],
            "scope": "docs",
            "primary": metric == "recall@10",
            "complete": cell["schedule_complete"],
        }
        for cell in cells
        for metric, value in cell["headline"].items()
    ]
    result = {
        "suite": "devdex_docs",
        "source_commit": COMMIT,
        "evidence_type": config.get("evidence_type", "live"),
        "metric_metadata": METRICS,
        "scheduled": len(attempts),
        "complete": bool(cells) and all(cell["schedule_complete"] for cell in cells),
        "report_series": series,
        "cells": cells,
        "per_task": per_task,
        "paired_descriptive": pairs,
        "artifacts": artifacts,
        "limitations": [
            "Public 30-task single-repeat pilot; descriptive, not statistical superiority.",
            "Canonical URL retrieval, not answer correctness or CodeRabbit review quality.",
        ],
    }
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "devdex-report.json").write_text(json.dumps(result, indent=2) + "\n")
    return result
