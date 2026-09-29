"""Configuration, deterministic scheduling and durable state, independent of suites."""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import importlib
import json
import os
import random
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, model_validator

ROOT = Path(__file__).resolve().parents[2]
SUITES = {"martian": "search_eval.suites.martian", "devdex_docs": "search_eval.suites.devdex"}
TERMINAL = {"completed", "failed", "skipped", "timed_out"}


class EvalError(RuntimeError):
    pass


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Arm(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,39}$")
    native_search: StrictBool = False
    mcp: list[str] = Field(default_factory=list)


class Profile(StrictModel):
    provider: str
    endpoint: str
    connection: str
    token_env: str
    mode: str | None = None
    tier: str = "authenticated"

    @model_validator(mode="after")
    def provider_settings(self):
        expected = {"keenable": "pro", "exa": "auto"}.get(self.provider)
        if self.mode is None:
            self.mode = expected
        if self.mode not in {
            "keenable": {"pro", "realtime"},
            "exa": {"auto", "fast", "instant", "neural"},
        }.get(self.provider, set()) or self.tier not in {"authenticated", "public"}:
            raise ValueError("Unsupported provider mode or tier")
        if self.provider == "exa" and self.tier != "authenticated":
            raise ValueError("Exa requires keyed tier")
        return self


class Limits(StrictModel):
    max_attempts: StrictInt = Field(ge=1, le=10000)
    max_review_events_per_hour: StrictInt | None = Field(default=None, ge=1, le=10000)
    max_search_calls: StrictInt = Field(default=8, ge=1, le=100)
    max_fetch_calls: StrictInt = Field(default=8, ge=1, le=100)
    review_timeout_seconds: StrictInt = Field(default=1800, ge=1, le=7200)
    episode_timeout_seconds: StrictInt = Field(default=300, ge=1, le=1800)
    poll_seconds: StrictInt = Field(default=10, ge=1, le=60)


class Experiment(StrictModel):
    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,59}$")
    suite: str
    manifest: str
    baseline: str
    arms: list[Arm] = Field(min_length=1)
    profiles: dict[str, Profile]
    limits: Limits
    repeats: StrictInt = Field(default=1, ge=1, le=20)
    seed: StrictInt = 1729
    role: str = "scored"
    github_owner: str = "your-github-login"
    review_profile: Literal["chill", "assertive"] = "chill"
    # Base gateway URL: public HTTPS for PR reviews, internal HTTP allowed for retrieval.
    public_mcp_url: str = ""
    allowance_file: str = "validation/allowance.json"
    suite_options: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def check(self):
        if self.suite not in SUITES:
            raise ValueError(f"Unknown suite: {self.suite}")
        kind = suite_contract(self.suite)["task_kind"]
        if self.role not in {"scored", "control", "development"}:
            raise ValueError("Unknown task role")
        ids = [a.id for a in self.arms]
        if len(set(ids)) != len(ids) or self.baseline not in ids:
            raise ValueError("Configuration IDs must be unique and include the baseline")
        for name, p in self.profiles.items():
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", name):
                raise ValueError("Invalid MCP profile ID")
            if p.provider not in {"keenable", "exa"} or p.endpoint not in {"search-a", "search-b"}:
                raise ValueError("Unsupported provider or endpoint")
        if len({p.endpoint for p in self.profiles.values()}) != len(self.profiles):
            raise ValueError("Duplicate MCP endpoint")
        if len({p.connection for p in self.profiles.values()}) != len(self.profiles):
            raise ValueError("Duplicate MCP connection label")
        for arm in self.arms:
            if len(set(arm.mcp)) != len(arm.mcp) or set(arm.mcp) - self.profiles.keys():
                raise ValueError("Duplicate or unknown MCP profile")
            if kind == "developer_retrieval" and (arm.native_search or len(arm.mcp) != 1):
                raise ValueError(
                    "Developer retrieval requires exactly one MCP profile and no native search"
                )
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}", self.github_owner):
            raise ValueError("Invalid GitHub owner")
        return self


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def digest(value) -> str:
    data = (
        value
        if isinstance(value, bytes)
        else json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    )
    return hashlib.sha256(data).hexdigest()


def atomic_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(name):
            os.unlink(name)


@contextlib.contextmanager
def lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EvalError("Another runner owns this workspace; resume after it exits") from exc
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def read_config(path: Path) -> Experiment:
    return Experiment.model_validate(yaml.safe_load(path.read_text()))


def suite_module(name: str):
    if name not in SUITES:
        raise EvalError(f"Unsupported suite {name}")
    return importlib.import_module(SUITES[name])


def suite_contract(name: str) -> dict:
    module = suite_module(name)
    kind = getattr(module, "TASK_KIND", None)
    if kind not in {"pr_review", "developer_retrieval"}:
        raise EvalError(f"Unsupported or undeclared task kind: {kind}")
    return {
        "task_kind": kind,
        "required_env": tuple(getattr(module, "REQUIRED_ENV", ())),
        "allowance_services": tuple(getattr(module, "ALLOWANCE_SERVICES", ())),
        "live_allowed": getattr(module, "LIVE_ALLOWED", False) is True,
    }


def shared_runtime_identity(root: Path = ROOT) -> str:
    """Shared image code and dependency lock; excludes runner-only vendor/gold."""
    files = sorted((root / "src").rglob("*.py")) + [root / "uv.lock"]
    return digest({str(p.relative_to(root)): digest(p.read_bytes()) for p in files if p.is_file()})


def code_identity(root: Path = ROOT) -> str:
    files = sorted((root / "src").rglob("*.py")) + [root / "uv.lock"]
    files += sorted(
        p
        for p in (root / "vendor").rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    )
    return digest({str(p.relative_to(root)): digest(p.read_bytes()) for p in files if p.is_file()})


def input_identity(config: Experiment, tasks: list[dict], root: Path = ROOT) -> dict:
    from .effective_inputs import resolve_inputs

    manifest = root / config.manifest
    return {
        "config": config.model_dump(),
        "manifest": digest(manifest.read_bytes()),
        "tasks": tasks,
        "code": code_identity(root),
        "effective_inputs": resolve_inputs(config, root),
    }


def fingerprint(config: Experiment, tasks: list[dict], root: Path = ROOT) -> str:
    return digest(input_identity(config, tasks, root))


def frozen_manifest(run_dir: Path, state: dict) -> dict:
    identity = state.get("input_identity", {})
    path = run_dir / "manifest.json"
    if digest(identity) != state.get("fingerprint") or not path.is_file():
        raise EvalError("Frozen input identity is missing or changed")
    raw = path.read_bytes()
    if digest(raw) != identity.get("manifest"):
        raise EvalError("Frozen manifest hash changed")
    if identity.get("config") != state.get("config"):
        raise EvalError("Saved configuration changed")
    if "effective_inputs" in identity:
        effective = run_dir / "effective-inputs.json"
        if (
            not effective.is_file()
            or json.loads(effective.read_text()) != identity["effective_inputs"]
        ):
            raise EvalError("Frozen effective inputs changed")
    return json.loads(raw)


def verify_run_code(state: dict, root: Path = ROOT) -> None:
    if state.get("input_identity", {}).get("code") != code_identity(root):
        raise EvalError(
            "Run source code changed; refuse grading or reporting with another implementation"
        )


def schedule(config: Experiment, tasks: list[dict]) -> list[dict]:
    kind = suite_contract(config.suite)["task_kind"]
    if any(t.get("kind") != kind for t in tasks):
        raise EvalError("Manifest task kind is incompatible with suite executor")
    eligible = [t for t in tasks if t["role"] == config.role]
    if not eligible:
        raise EvalError(f"No executable {config.role} tasks in the frozen manifest")
    if len({t["id"] for t in eligible}) != len(eligible):
        raise EvalError("Duplicate task IDs")
    attempts = []
    rng = random.Random(config.seed)
    for repeat in range(1, config.repeats + 1):
        ordered = eligible.copy()
        rng.shuffle(ordered)
        for task in ordered:
            arms = config.arms.copy()
            rng.shuffle(arms)
            for arm in arms:
                identity = [config.run_id, task["id"], arm.id, repeat]
                aid = digest(identity)[:24]
                attempts.append(
                    {
                        "id": aid,
                        "task_id": task["id"],
                        "role": task["role"],
                        "configuration": arm.id,
                        "repeat": repeat,
                        "status": "pending",
                        "artifact_dir": f"attempts/{aid}",
                    }
                )
    if len(attempts) > config.limits.max_attempts:
        raise EvalError(
            f"Schedule needs {len(attempts)} attempts; cap is {config.limits.max_attempts}"
        )
    return attempts


def prepare_state(config: Experiment, tasks: list[dict], run_dir: Path, root: Path = ROOT):
    identity = input_identity(config, tasks, root)
    fp = digest(identity)
    path = run_dir / "state.json"
    if path.exists():
        state = json.loads(path.read_text())
        if state["fingerprint"] != fp:
            raise EvalError(
                "Run fingerprint changed. Use a new run_id; never mix settings or inputs"
            )
        frozen_manifest(run_dir, state)
        return state
    state = {
        "version": 1,
        "created_at": now(),
        "fingerprint": fp,
        "input_identity": identity,
        "config": config.model_dump(),
        "attempts": schedule(config, tasks),
    }
    run_dir.mkdir(parents=True, exist_ok=True)
    raw = (root / config.manifest).read_bytes()
    if digest(raw) != identity["manifest"]:
        raise EvalError("Manifest changed during preparation")
    pending = run_dir / ".manifest.pending"
    with pending.open("wb") as stream:
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(pending, run_dir / "manifest.json")
    atomic_json(run_dir / "effective-inputs.json", identity["effective_inputs"])
    atomic_json(run_dir / "config.json", config.model_dump())
    atomic_json(run_dir / "visible-tasks.json", tasks)
    atomic_json(path, state)
    return state


def coderabbit_settings(arm: Arm, profiles: dict[str, Profile], review_profile="chill") -> dict:
    return {
        "language": "en-US",
        "reviews": {"profile": review_profile, "auto_review": {"enabled": False}},
        "knowledge_base": {
            "web_search": {"enabled": arm.native_search},
            "mcp": {
                "usage": "enabled" if arm.mcp else "disabled",
                "disabled_servers": [p.connection for k, p in profiles.items() if k not in arm.mcp],
            },
            "learnings": {"scope": "local"},
            "issues": {"scope": "local"},
            "pull_requests": {"scope": "local"},
        },
    }


def verify_allowance(config: Experiment, root: Path, attempts: int) -> dict:
    path = root / config.allowance_file
    if not path.exists():
        raise EvalError(f"Live calls blocked: missing verified allowance record at {path}")
    data = json.loads(path.read_text())
    if data.get("verified") is not True or data.get("suite") != config.suite:
        raise EvalError("Live calls blocked: allowance is unverified or for another suite")
    if data.get("run_id") != config.run_id or data.get("role") != config.role:
        raise EvalError("Allowance must bind this exact run and task role")
    if not data.get("evidence") or data.get("max_attempts", 0) < attempts:
        raise EvalError("Allowance has no evidence or insufficient attempt capacity")
    expiry = datetime.fromisoformat(data.get("valid_until", "1900-01-01T00:00:00+00:00"))
    if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
        raise EvalError("Allowance evidence expired")
    if (
        data.get("additional_spending_usd") != 0
        or data.get("automatic_overage_disabled") is not True
    ):
        raise EvalError("Only verified existing quota with no automatic paid overage is authorized")
    needed = {config.profiles[k].provider for a in config.arms for k in a.mcp}
    contract = suite_contract(config.suite)
    needed.update(contract["allowance_services"])
    if contract["task_kind"] == "pr_review":
        needed.add("coderabbit")
    if not needed <= set(data.get("services", [])):
        raise EvalError("Allowance evidence does not cover every selected service")
    return data


def record_operator_allowance(
    config: Experiment, root: Path = ROOT, existing_model_credit_usd: float | None = None
) -> Path:
    """Record the CLI user's explicit quota attestation, never infer provider credit."""
    from datetime import timedelta

    path = (root / config.allowance_file).resolve()
    if not path.is_relative_to(root.resolve()):
        raise EvalError("Allowance file must be inside this checkout")
    contract = suite_contract(config.suite)
    services = {config.profiles[p].provider for arm in config.arms for p in arm.mcp}
    services.update(contract["allowance_services"])
    if contract["task_kind"] == "pr_review":
        services.add("coderabbit")
    data = {
        "verified": True,
        "verification_method": "operator_attestation",
        "suite": config.suite,
        "run_id": config.run_id,
        "role": config.role,
        "max_attempts": config.limits.max_attempts,
        "valid_until": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        "services": sorted(services),
        "additional_spending_usd": 0,
        "automatic_overage_disabled": True,
        "evidence": "Operator explicitly confirmed access, sufficient existing quota and disabled paid overage for this config using --confirm-existing-quota. No account balance was read by this command.",
    }
    if "martian_judge" in services:
        cap = config.suite_options.get("max_judge_usd")
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or cap <= 0:
            raise EvalError("Set a positive suite_options.max_judge_usd before approval")
        import math

        if (
            existing_model_credit_usd is None
            or not math.isfinite(existing_model_credit_usd)
            or existing_model_credit_usd < cap
        ):
            raise EvalError("Declare --existing-model-credit-usd covering the configured judge cap")
        data["max_judge_usd"] = cap
        data["existing_model_credit_usd"] = existing_model_credit_usd
    atomic_json(path, data)
    return path
