"""Audit a saved DevDex run offline; never modify the run or call external services."""

from __future__ import annotations

import argparse
import collections
import fnmatch
import hashlib
import importlib
import json
import sys
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

ROOT = Path(__file__).resolve().parents[1]
TERMINAL = {"completed", "failed", "skipped", "timed_out"}


def digest(value):
    if not isinstance(value, bytes):
        value = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(value).hexdigest()


def read(path):
    return json.loads(path.read_text())


def require(condition, message):
    if not condition:
        raise ValueError(message)


def canonical_url(url):
    p = urlsplit(url)
    return urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", p.query, ""))


def runtime_hash():
    files = list((ROOT / "src").rglob("*.py")) + [ROOT / "uv.lock"]
    files += [
        p
        for p in (ROOT / "vendor").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ]
    return digest({str(p.relative_to(ROOT)): digest(p.read_bytes()) for p in files})


def audit(run):
    state = read(run / "state.json")
    config, identity = state["config"], state["input_identity"]
    require(config["suite"] == "devdex_docs", "Not a DevDex Docs run")
    require(digest(identity) == state["fingerprint"], "Run fingerprint mismatch")
    require(identity["config"] == config, "Frozen configuration mismatch")
    require(
        digest((run / "manifest.json").read_bytes()) == identity["manifest"],
        "Frozen manifest changed",
    )
    manifest = read(run / "manifest.json")
    provenance = read(run / "audit.json")
    for name, expected in provenance["file_sha256"].items():
        path = (run / name).resolve()
        require(path.is_relative_to(run), "Audit file escapes run")
        require(digest(path.read_bytes()) == expected, f"Saved audit hash differs: {name}")
    protocol = read(run / "evidence/devdex-protocol.json")
    vendor = ROOT / "vendor/devdex"
    for name, expected in protocol["source_files_sha256"].items():
        require(
            digest((vendor / name).read_bytes()) == expected,
            f"Pinned vendor source changed: {name}",
        )
    required_scorers = {"devdex/scorer/report_metrics.py", "devdex/scorer/suite.py"}
    require(
        required_scorers <= protocol["source_files_sha256"].keys(), "No recorded scorer identity"
    )
    data = vendor / "devdex/gt/docs_public.jsonl"
    require(digest(data.read_bytes()) == manifest["source_sha256"], "Pinned gold dataset changed")
    gold = {row["id"]: row for row in map(json.loads, data.read_text().splitlines())}
    roles = {
        t["id"]: "scored" if t["role"] == "evaluation" else t["role"] for t in manifest["tasks"]
    }
    for task in manifest["tasks"]:
        require(digest(gold[task["id"]]) == task["source_row_sha256"], "Selected gold row changed")
    expected = {
        (task, config["role"], arm["id"], repeat)
        for task, role in roles.items()
        if role == config["role"]
        for arm in config["arms"]
        for repeat in range(1, config["repeats"] + 1)
    }
    attempts = state["attempts"]
    keys = [(a["task_id"], a["role"], a["configuration"], a["repeat"]) for a in attempts]
    require(
        len(keys) == len(set(keys)) and set(keys) == expected,
        "Schedule missing/extra/duplicate cells",
    )
    require(
        all(a["status"] in TERMINAL for a in attempts),
        "Run has unresolved attempts; do not audit as final",
    )
    # Import only verified original arithmetic, never the model harness or current adapter.
    sys.path.insert(0, str(vendor))
    native = importlib.import_module("devdex.scorer.report_metrics")
    suite = importlib.import_module("devdex.scorer.suite")
    metadata = suite._docs_meta()

    def is_gold(url, record):
        return bool(
            suite._docs_rank(
                {"search_log": [{"results": [{"rank": 0, "url": url}]}]}, metadata[record["qid"]]
            )
        )

    def pool_test(record, engine):
        if record.get("search_log"):
            return bool(suite._docs_rank(record, metadata[record["qid"]]))
        return any(is_gold(url, record) for url in engine)

    groups, snapshots = collections.defaultdict(list), collections.defaultdict(set)
    transcript_calls = collections.Counter()
    counts, costs, missing, citation_gaps, episode_evidence = (
        collections.Counter(),
        collections.defaultdict(list),
        [],
        [],
        [],
    )
    tool_evidence = {r["attempt_id"]: r for r in read(run / "tool-calls.json")}
    for a in attempts:
        aid = a["id"]
        require(
            aid == digest([config["run_id"], a["task_id"], a["configuration"], a["repeat"]])[:24],
            "Attempt ID derivation mismatch",
        )
        path = (run / a["artifact_dir"]).resolve()
        require(path.is_relative_to(run), "Attempt artifact directory escapes run")
        if not (path / "record.json").is_file():
            require(a["status"] != "completed", "Completed attempt lacks a record")
            record = {
                "qid": a["task_id"],
                "sources": [],
                "search_log": [],
                "committed": False,
                "error": f"no_tool_calls: scheduled status={a['status']}; record absent",
                "latency": None,
                "attempt_id": aid,
            }
            missing.append(aid)
        else:
            record = read(path / "record.json")
            for field, value in {
                "attempt_id": aid,
                "qid": a["task_id"],
                "configuration": a["configuration"],
                "repeat": a["repeat"],
            }.items():
                require(record.get(field) == value, f"Record {field} mismatch: {aid}")
            terminal = read(path / "terminal.json")
            require(
                terminal["attempt_id"] == aid and terminal["status"] == a["status"],
                f"Terminal identity/outcome mismatch: {aid}",
            )
            require(
                terminal["record_sha256"] == digest((path / "record.json").read_bytes()),
                f"Terminal record hash mismatch: {aid}",
            )
            visible, worker, request = (
                read(path / "visible-task.json"),
                read(path / "worker-input.jsonl"),
                read(path / "request-snapshot.json"),
            )
            query = gold[a["task_id"]]["query"]
            require(
                visible["input"] == {"query": query},
                f"Model visible input differs or contains extra fields: {aid}",
            )
            require(
                set(worker)
                == {"id", "query", "intent", "container", "expected_answer", "acceptable_sources"},
                f"Worker input has unexpected fields: {aid}",
            )
            require(
                worker["id"] == a["task_id"]
                and worker["query"] == query
                and worker["expected_answer"] == ""
                and worker["acceptable_sources"] == []
                and worker["container"] == {"ref": ""},
                f"Gold/identity leakage in worker input: {aid}",
            )
            require(
                set(request)
                == {
                    "model",
                    "system_prompt",
                    "prompt",
                    "allowlisted_model_fields",
                    "settings",
                    "search_tool",
                    "fetch_tool",
                    "submit_tool",
                    "vendor_skills",
                },
                f"Unexpected request snapshot fields: {aid}",
            )
            require(
                record["model"] == protocol["model"] and record["settings"] == protocol["settings"],
                f"Recorded model/settings drift: {aid}",
            )
            for event in record.get("_transcript", []):
                if isinstance(event, dict) and event.get("t") == "call":
                    transcript_calls[event["name"]] += 1
            require(
                request["prompt"] == "Task:\n" + query
                and request["allowlisted_model_fields"] == ["input.query"],
                f"Actual request snapshot includes extra task context: {aid}",
            )
            require(
                request["system_prompt"] == protocol["system_prompt"]
                and request["settings"] == protocol["settings"]
                and request["model"] == protocol["model"],
                f"Prompt/model/settings drift: {aid}",
            )
            require(
                digest(request) == record["request_snapshot_sha256"],
                f"Request hash mismatch: {aid}",
            )
            require(record.get("expected") == "", f"Gold answer present in worker record: {aid}")
            require(
                not set(record["actual_models"]) - {protocol["model"]},
                f"Unexpected actual model: {aid}",
            )
            if a["status"] == "completed":
                require(
                    record["actual_models"] == [protocol["model"]]
                    and record["request_verified"]
                    and not record.get("error"),
                    f"Completed episode lacks verified model execution: {aid}",
                )
            snapshots[(a["task_id"], a["repeat"])].add(record["request_snapshot_sha256"])
            costs[a["configuration"]].append(record.get("cost") or 0)
        groups[(a["role"], a["configuration"], a["repeat"])].append(record)
        trace_info = tool_evidence[aid]
        trace = run / "evidence/gateway" / (digest(aid.encode()) + ".jsonl")
        reservations, finished = {}, {}
        if trace.is_file():
            require(
                digest(trace.read_bytes()) == trace_info["trace_sha256"],
                f"Trace hash mismatch: {aid}",
            )
            for event in map(json.loads, trace.read_text().splitlines()):
                require(
                    event["attempt_id"] == aid and event["configuration"] == a["configuration"],
                    f"Cross-attempt trace: {aid}",
                )
                bucket = reservations if event["event"] == "reserved" else finished
                require(
                    event["event"] in {"reserved", "finished"} and event["call_id"] not in bucket,
                    f"Invalid/duplicate trace event: {aid}",
                )
                bucket[event["call_id"]] = event
            require(
                set(finished) <= reservations.keys(), f"Finished call without reservation: {aid}"
            )
        successful, retrieved = [], set()
        for event in finished.values():
            require(
                event["operation"] == reservations[event["call_id"]]["operation"],
                f"Trace operation changed: {aid}",
            )
            counts[
                (a["configuration"], event["operation"], "success" if event["ok"] else "failed")
            ] += 1
            if not event["ok"]:
                continue
            if event["operation"] == "search":
                profile = config["profiles"][
                    config["arms"][
                        next(
                            i
                            for i, arm in enumerate(config["arms"])
                            if arm["id"] == a["configuration"]
                        )
                    ]["mcp"][0]
                ]
                require(
                    event["provider"] == profile["provider"]
                    and event["tier"] == profile["tier"]
                    and event["mode"] == profile["mode"],
                    f"Provider tier/mode mismatch: {aid}",
                )
                require(
                    isinstance(event.get("raw", {}).get("results"), list),
                    f"Raw provider response absent: {aid}",
                )
                output = event["output"]["results"]
                require(len(output) <= 10, f"Search depth exceeded: {aid}")
                for result in output:
                    require(
                        not any(
                            fnmatch.fnmatchcase(result["url"].lower(), p.lower())
                            for p in manifest["answer_patterns"]
                        ),
                        f"Blocked answer URL reached model: {aid}",
                    )
                    candidates = [
                        r
                        for r in event["raw"]["results"]
                        if isinstance(r, dict)
                        and isinstance(r.get("url"), str)
                        and canonical_url(r["url"]) == result["url"]
                    ]

                    def matches(raw):
                        excerpt = raw.get(
                            "snippet" if profile["provider"] == "keenable" else "highlights", ""
                        )
                        if isinstance(excerpt, list):
                            excerpt = "\n".join(x for x in excerpt if isinstance(x, str))
                        if not isinstance(excerpt, str):
                            excerpt = ""
                        return (
                            result["title"] == str(raw.get("title") or "")[:1000]
                            and result["excerpt"] == excerpt[:1000]
                        )

                    require(
                        any(matches(raw) for raw in candidates),
                        f"Normalized title/excerpt/URL absent from raw response: {aid}",
                    )
                    retrieved.add(result["url"])
                successful.append((event["input"], tuple(r["url"] for r in output)))
            elif event["operation"] == "fetch":
                retrieved.update([event["input"], event["output"]["url"]])
        if aid not in missing:
            logged = [
                (s["q"], tuple(r["url"] for r in s["results"]))
                for s in record["search_log"]
                if not s.get("error")
            ]
            require(
                collections.Counter(successful) == collections.Counter(logged),
                f"Agent search log differs from gateway output: {aid}",
            )
            if a["status"] == "completed":
                require(
                    set(reservations) == set(finished),
                    f"Completed episode has unfinished calls: {aid}",
                )
                require(
                    record["searches"]
                    == sum(e["operation"] == "search" for e in reservations.values()),
                    f"Search count mismatch: {aid}",
                )
                require(
                    record["reads"]
                    == sum(e["operation"] == "fetch" for e in reservations.values()),
                    f"Fetch count mismatch: {aid}",
                )
            for url in record.get("sources", []):
                if url not in retrieved:
                    citation_gaps.append(
                        {
                            "attempt_id": aid,
                            "url": url,
                            "note": "Not a literal retrieved URL; native scorer decides credit without replacement.",
                        }
                    )
        episode_evidence.append(
            {
                "attempt_id": aid,
                "task_id": a["task_id"],
                "configuration": a["configuration"],
                "repeat": a["repeat"],
                "status": a["status"],
                "reserved_calls": len(reservations),
                "finished_calls": len(finished),
            }
        )
    require(
        all(len(values) == 1 for values in snapshots.values()), "Paired request snapshots differ"
    )
    saved = read(run / "grading/devdex-report.json")
    require(len(saved["cells"]) == len(groups), "Saved report cell coverage differs")
    cells = []
    for cell in saved["cells"]:
        key = (cell["role"], cell["configuration"], cell["repeat"])
        records = groups[key]
        native.check_docs_coverage(records, metadata, "independent audit exact records")
        measured = native.score_records(records, "docs", is_gold, pool_test)
        require(measured == cell["native_metrics"], f"Original scorer replay differs: {key}")
        expected_headline = {
            "recall@10": None if measured["invalid"] else measured["recall"],
            "MRR@10": None if measured["invalid"] else measured["amrr"],
        }
        require(
            cell["headline"] == expected_headline and cell["scheduled"] == len(records),
            f"Headline/denominator differs: {key}",
        )
        cells.append(
            {
                "role": key[0],
                "configuration": key[1],
                "repeat": key[2],
                "scheduled": len(records),
                "native_metrics": measured,
                "headline": expected_headline,
            }
        )
    expected_series = {
        (cell["role"], cell["configuration"], cell["repeat"], metric): (value, cell["scheduled"])
        for cell in cells
        for metric, value in cell["headline"].items()
    }
    observed_series = {
        (row["role"], row["configuration"], row["repeat"], row["metric"]): (
            row["value"],
            row["denominator"],
        )
        for row in saved["report_series"]
    }
    require(
        observed_series == expected_series and len(saved["report_series"]) == len(expected_series),
        "Published report series differs from native replay",
    )
    current = runtime_hash()
    return {
        "passed": True,
        "run_id": config["run_id"],
        "run_fingerprint": state["fingerprint"],
        "runtime": {
            "recorded_sha256": identity["code"],
            "current_sha256": current,
            "matches": current == identity["code"],
            "note": "A mismatch is disclosed, never rewritten. Replay uses original scorer files verified against captured protocol hashes, not current adapter logic.",
        },
        "scheduled": len(attempts),
        "outcomes": dict(collections.Counter(a["status"] for a in attempts)),
        "missing_records": missing,
        "cells": cells,
        "episodes": episode_evidence,
        "tool_calls": [
            {"configuration": k[0], "operation": k[1], "outcome": k[2], "count": v}
            for k, v in sorted(counts.items())
        ],
        "citation_gaps": citation_gaps,
        "transcript_tool_calls": dict(transcript_calls),
        "model_cost": {
            arm: {
                "sdk_result_reported_total_usd": sum(values),
                "record_count": len(values),
                "mean_usd": sum(values) / len(values),
            }
            for arm, values in sorted(costs.items())
        },
        "cost_note": "Sum each record.cost once. Pinned upstream assigns it from ResultMessage.total_cost_usd. observed_usage is overlapping telemetry and is never summed. SDK estimates are not invoices or search-provider charges.",
        "limitations": [
            "Native canonical URL metrics are not semantic answer grading.",
            "No task/candidate replacement or alternative-label rescoring occurred.",
            "Zero fetch calls do not establish reader transport behavior.",
            "All checks are offline. No live service was contacted.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    run = args.run_dir.resolve()
    output = (args.output or ROOT / "validation" / f"{run.name}-audit.json").resolve()
    if output.is_relative_to(run):
        parser.error("Audit output must stay outside the immutable run")
    try:
        result = audit(run)
    except (OSError, ValueError, KeyError, TypeError, ImportError) as exc:
        result = {
            "passed": False,
            "run_dir": str(run),
            "error": str(exc),
            "note": "Audit failed closed. No result or run file was changed.",
        }
    output.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, indent=2) + "\n"
    if output.exists() and output.read_text() != payload:
        print(
            json.dumps(
                {
                    "passed": False,
                    "error": "Existing audit differs; select a new --output path. Existing evidence was preserved.",
                }
            )
        )
        return 2
    if not output.exists():
        output.write_text(payload)
    print(
        json.dumps({"passed": result["passed"], "audit": str(output), "error": result.get("error")})
    )
    return 0 if result["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
