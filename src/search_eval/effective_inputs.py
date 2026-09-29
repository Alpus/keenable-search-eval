"""Resolve experiment-controlled inputs without reading credential values."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit


def sha(data):
    return hashlib.sha256(data).hexdigest()


def resolve_agent(options, root):
    requested = options.get("agent", {})
    allowed = {"name", "model", "protocol_file", "settings", "sampling", "retry"}
    if set(requested) - allowed:
        raise ValueError("Unknown DevDex agent input")
    relative = requested.get("protocol_file", "data/devdex-docs-protocol.json")
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError("Agent protocol must be inside the workspace")
    raw = path.read_bytes()
    protocol = json.loads(raw)
    if protocol["model"] != "claude-opus-4-8":
        raise ValueError("Original DevDex protocol model must remain claude-opus-4-8")
    name = requested.get("name", "devdex-original-sdk")
    model = requested.get("model", protocol["model"])
    settings = requested.get("settings", protocol["settings"])
    sampling = requested.get("sampling", {"temperature": None, "top_p": None, "seed": None})
    retry = requested.get("retry", {"episode_retries": 0, "sdk_retries": None})
    if (
        name != "devdex-original-sdk"
        or model != protocol["model"]
        or settings != protocol["settings"]
    ):
        raise ValueError("Agent/model/settings differ from the selected pinned protocol")
    if sampling != {"temperature": None, "top_p": None, "seed": None}:
        raise ValueError("Original DevDex does not set sampling parameters")
    if retry != {"episode_retries": 0, "sdk_retries": None}:
        raise ValueError(
            "Original DevDex retry inputs require zero episode retries and SDK-managed internals"
        )
    return {
        "name": name,
        "requested_model": model,
        "model_observability": "SDK messages",
        "protocol_file": relative,
        "protocol_sha256": sha(raw),
        "source_revision": protocol["source_commit"],
        "source_files_sha256": protocol["source_files_sha256"],
        "system_prompt_sha256": sha(protocol["system_prompt"].encode()),
        "settings": settings,
        "sampling": sampling,
        "retry": retry,
        "null_parameters": "Not set by the original harness; provider/SDK default is not claimed pinned.",
    }


def judge_endpoint(value):
    if not isinstance(value, str):
        raise ValueError(
            "Set explicit judge.base_url; MARTIAN_BASE_URL no longer selects transport"
        )
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("judge.base_url must be HTTPS without credentials, query or fragment")
    return value


def resolve_judge(settings):
    if settings is None:
        return None
    allowed = {
        "model",
        "temperature",
        "structured_output",
        "historical_structured_output_verified",
        "protocol_variant_accepted",
        "protocol_variant",
        "max_retries",
        "base_url",
        "max_tokens",
        "top_p",
        "seed",
        "transport",
    }
    if set(settings) - allowed:
        raise ValueError("Unknown judge input; unsupported parameters cannot be silently ignored")
    result = dict(settings)
    if result.setdefault("max_retries", 1) != 1:
        raise ValueError("Accepted judge protocol requires max_retries=1 (one total attempt)")
    transport = result.setdefault("transport", {"timeout_seconds": 120, "sdk_max_retries": 0})
    if transport != {"timeout_seconds": 120, "sdk_max_retries": 0}:
        raise ValueError("Judge transport must preserve the accepted timeout and no SDK retries")
    result.setdefault("base_url", None)
    if result["base_url"] is not None:
        judge_endpoint(result["base_url"])
    for key in ("max_tokens", "top_p", "seed"):
        if result.get(key) is not None:
            raise ValueError(f"Judge {key} is not set by the accepted protocol")
        result[key] = None
    result["unset_sampling_note"] = (
        "Null fields are omitted from requests; provider defaults are not pinned."
    )
    return result


def resolve_inputs(config, root: Path):
    from .core import coderabbit_settings, shared_runtime_identity, suite_contract, suite_module

    root = root.resolve()
    manifest_raw = (root / config.manifest).read_bytes()
    manifest = json.loads(manifest_raw)
    kind = suite_contract(config.suite)["task_kind"]
    suite = suite_module(config.suite)
    scorer = suite.scorer_identity() if hasattr(suite, "scorer_identity") else None
    requested_scorer = config.suite_options.get("scorer")
    if requested_scorer is not None and (
        scorer is None or requested_scorer != {key: scorer[key] for key in ("name", "revision")}
    ):
        raise ValueError("Requested scorer does not match the installed pinned suite")
    if kind == "developer_retrieval":
        agent = resolve_agent(config.suite_options, root)
    else:
        agent = {
            "name": "CodeRabbit",
            "review_profile": config.review_profile,
            "requested_model": None,
            "model_observability": "Service-managed; model identity is not exposed by this integration",
            "sampling": {"temperature": None, "top_p": None, "seed": None},
            "retry": {"review_retriggers": 0, "internal_retries": None},
            "requested_settings_by_arm": {
                arm.id: coderabbit_settings(arm, config.profiles, config.review_profile)
                for arm in config.arms
            },
        }
    providers = {}
    for name, profile in config.profiles.items():
        from .providers import request_spec

        providers[name] = {
            "provider": profile.provider,
            "mode": profile.mode,
            "tier": profile.tier,
            **request_spec(profile.model_dump()),
        }
    gateway = root / ".gateway/traces/gateway-config.json"
    judge = resolve_judge(config.suite_options.get("judge", manifest.get("judge")))
    return {
        "version": 2,
        "suite": config.suite,
        "task_kind": kind,
        "resolved_config": config.model_dump(),
        "dataset_sha256": sha(manifest_raw),
        "dataset_revision": manifest.get("revision", manifest.get("source_commit")),
        "agent": agent,
        "judge": judge,
        "scorer": scorer,
        "providers": providers,
        "expected_gateway_runtime_sha256": shared_runtime_identity(root),
        "gateway_settings": json.loads(gateway.read_text()) if gateway.exists() else None,
        "gateway_settings_absence": None
        if gateway.exists()
        else "No effective gateway startup file available; live preflight still required",
        "schedule": {
            "seed": config.seed,
            "repeats": config.repeats,
            "seed_scope": "Attempt order only; dataset selection is frozen in the manifest. Not an LLM seed.",
        },
        "limits": config.limits.model_dump(),
    }
