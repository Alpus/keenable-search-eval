"""One original DevDex agent episode in an isolated subprocess.

Invocation does not retry episodes. The parent must supply a verified allowance and
an immutable per-attempt MCP authentication context. No fallback model is allowed.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib
import json
import os
import shutil
import sys
import time
from pathlib import Path

from search_eval.suites.devdex import ROOT, VENDOR, digest

MODEL = "claude-opus-4-8"
SETTINGS = {
    "K": "10",
    "CITE_K": "10",
    "MAX_TURNS": "40",
    "MAX_SEARCH": "0",
    "MAX_READ": "0",
    "TIMEOUT": "600",
    "RUNS": "1",
    "SKILL": "off",
    "AGENT_SNIPPET": "20000",
    "TRANSCRIPT_CHARS": "0",
    "PAGE_FULL": "0",
    "GROUND_CHARS": "6000",
    "MAX_MCP_OUTPUT_TOKENS": "800000",
}


def prompt_text() -> str:
    if str(VENDOR) not in sys.path:
        sys.path.insert(0, str(VENDOR))
    return importlib.import_module("devdex.harness.prompts").SYSTEM_DOCS


def visible_input(task: dict) -> dict:
    if task.get("kind") != "developer_retrieval":
        raise ValueError("DevDex worker only accepts developer_retrieval tasks")
    inp = task.get("input", {})
    if set(inp) != {"query"} or not isinstance(inp["query"], str) or not inp["query"].strip():
        raise ValueError("DevDex model input must contain only a nonempty query")
    return {"query": inp["query"]}


def request_snapshot(task: dict, agent: dict | None = None) -> dict:
    inp = visible_input(task)
    return {
        "model": agent["requested_model"] if agent else MODEL,
        "system_prompt": prompt_text(),
        "prompt": f"Task:\n{inp['query']}",
        "allowlisted_model_fields": ["input.query"],
        "settings": agent["settings"] if agent else SETTINGS,
        "search_tool": "mcp__ext__search",
        "fetch_tool": "mcp__ext__fetch",
        "submit_tool": "mcp__sub__submit_answer",
        "vendor_skills": False,
    }


def validate_execution(task: dict, config: dict) -> None:
    visible_input(task)
    from search_eval.effective_inputs import resolve_agent

    agent = config.get("agent")
    if agent:
        expected = resolve_agent(
            {
                "agent": {
                    "name": agent["name"],
                    "model": agent["requested_model"],
                    "protocol_file": agent["protocol_file"],
                    "settings": agent["settings"],
                    "sampling": agent["sampling"],
                    "retry": agent["retry"],
                }
            },
            ROOT,
        )
        if hashlib.sha256(prompt_text().encode()).hexdigest() != agent["system_prompt_sha256"]:
            raise ValueError("Actual system prompt differs from resolved protocol")
        if expected != agent:
            raise ValueError("Resolved agent protocol changed")
    protocol = json.loads(
        (ROOT / (agent["protocol_file"] if agent else "data/devdex-docs-protocol.json")).read_text()
    )
    for relative, expected in protocol["source_files_sha256"].items():
        if hashlib.sha256((VENDOR / relative).read_bytes()).hexdigest() != expected:
            raise ValueError("Pinned DevDex harness source changed")
    required = {"attempt_id", "task_id", "configuration", "repeat"}
    if not required <= config.keys() or config["task_id"] != task["id"]:
        raise ValueError("Missing or mismatched immutable DevDex attempt identity")
    if config.get("model", MODEL) != MODEL:
        raise ValueError(f"Protocol pins {MODEL}; model replacement needs a named protocol change")
    if not config.get("agent_access_verified") or not config.get("allowance_verified"):
        raise ValueError(f"Live access and allowance for historical model {MODEL} are unverified")
    if not os.environ.get("SEARCH_EVAL_MCP_URL") or not os.environ.get("SEARCH_EVAL_MCP_TOKEN"):
        raise ValueError("Missing per-attempt MCP URL/authentication context")
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise ValueError("Original DevDex agent requires ANTHROPIC_API_KEY and verified allowance")


async def run_attempt(task: dict, config: dict, output: Path) -> dict:
    validate_execution(task, config)
    if (output / "record.json").exists() or (output / "terminal.json").exists():
        raise ValueError("DevDex attempt already has terminal evidence; do not replay it")
    output.mkdir(parents=True, exist_ok=True)
    agent = config.get("agent")
    selected_model = agent["requested_model"] if agent else MODEL
    selected_settings = agent["settings"] if agent else SETTINGS
    snapshot = request_snapshot(task, agent)
    (output / "request-snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")
    # The upstream module hashes and loads GT_FILE at import. Give it only the
    # allowlisted query and identity plus empty output-only label placeholders.
    row = {
        "id": task["id"],
        "query": task["input"]["query"],
        "intent": "find_docs",
        "container": {"ref": ""},
        "expected_answer": "",
        "acceptable_sources": [],
    }
    task_file = output / "worker-input.jsonl"
    task_file.write_text(json.dumps(row) + "\n")
    os.environ.update(selected_settings)
    os.environ.update(
        {
            "MODEL": selected_model,
            "GT_FILE": str(task_file.resolve()),
            "OUT_DIR": str(output.resolve()),
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "DEVDEX_EXT_MCP_URL": os.environ["SEARCH_EVAL_MCP_URL"],
            "DEVDEX_EXT_AUTH": "Bearer " + os.environ["SEARCH_EVAL_MCP_TOKEN"],
            "DEVDEX_EXT_SEARCH_TOOL": "search",
            "DEVDEX_EXT_FETCH_TOOL": "fetch",
        }
    )
    os.environ.pop("BENCH_DIR", None)
    sys.path.insert(0, str(VENDOR))
    sys.path.insert(0, str(VENDOR / "devdex" / "harness"))
    sys.argv = ["runner_sdk.py", "docs", "external", "1", "1", "isolated"]
    runner = importlib.import_module("runner_sdk")
    original_query = runner.query
    actual_models = set()
    usage = []
    request_seen = False

    async def checked_query(*, prompt, options):
        nonlocal request_seen
        if prompt != snapshot["prompt"] or options.system_prompt != snapshot["system_prompt"]:
            raise ValueError("Actual model request differs from the frozen snapshot")
        if options.model != selected_model:
            raise ValueError("Original harness selected an unexpected model")
        request_seen = True
        async for message in original_query(prompt=prompt, options=options):
            actual = getattr(message, "model", None)
            if actual:
                actual_models.add(actual)
                if actual != selected_model:
                    raise ValueError(
                        f"returned an error result: actual model {actual!r} differs from pinned {selected_model}"
                    )
            observed = getattr(message, "usage", None)
            if observed is not None:
                usage.append(observed)
            yield message

    runner.query = checked_query
    start = time.monotonic()
    try:
        record = await runner.run_one(row)
    finally:
        shutil.rmtree(runner.JAIL, ignore_errors=True)
    record.update(
        {
            "attempt_id": config["attempt_id"],
            "configuration": config["configuration"],
            "repeat": config["repeat"],
            "latency": time.monotonic() - start,
            "actual_models": sorted(actual_models),
            "observed_usage": usage,
            "request_snapshot_sha256": digest(snapshot),
            "request_verified": request_seen,
            "settings": selected_settings,
            "requested_model": selected_model,
            "effective_agent": agent,
            "harness_adaptations": [
                "Normalized shared search/fetch MCP surface",
                "One original run_one per process",
                "Query-only model input; output-only gold fields empty",
                "Exact attempt artifact scope",
            ],
        }
    )
    if not actual_models and not record.get("error"):
        record["error"] = "returned an error result: actual model identity unavailable"
    # Native exceptions may echo transport/auth details; credentials must not enter artifacts.
    serialized = json.dumps(record, indent=2)
    for key in ("SEARCH_EVAL_MCP_TOKEN", "ANTHROPIC_API_KEY"):
        if os.environ.get(key):
            serialized = serialized.replace(os.environ[key], "[REDACTED]")
    (output / "record.json").write_text(serialized + "\n")
    terminal = {
        "attempt_id": config["attempt_id"],
        "status": "failed" if record.get("error") else "completed",
        "record_sha256": hashlib.sha256((serialized + "\n").encode()).hexdigest(),
    }
    (output / "terminal.json").write_text(json.dumps(terminal, indent=2) + "\n")
    return terminal


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        result = asyncio.run(
            run_attempt(
                json.loads(args.task.read_text()), json.loads(args.config.read_text()), args.output
            )
        )
        return 0 if result["status"] == "completed" else 1
    except Exception as error:  # noqa: BLE001 - worker boundary must redact transport secrets.
        detail = (
            str(error) if isinstance(error, ValueError) else "check preflight/terminal evidence"
        )
        for key in ("SEARCH_EVAL_MCP_TOKEN", "ANTHROPIC_API_KEY"):
            if os.environ.get(key):
                detail = detail.replace(os.environ[key], "[REDACTED]")
        print(f"DevDex worker stopped: {type(error).__name__}: {detail}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
