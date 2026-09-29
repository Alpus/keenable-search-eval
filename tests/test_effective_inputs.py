"""Saved protocol inputs bind executed agent behavior without credentials."""

import json
import shutil

import pytest

from search_eval import devdex_worker as worker
from search_eval.core import ROOT, EvalError, prepare_state, read_config
from search_eval.effective_inputs import resolve_agent, resolve_inputs


@pytest.fixture
def setup(tmp_path):
    (tmp_path / "data").mkdir()
    for name in ("devdex-docs-protocol.json", "devdex-docs-manifest.json"):
        shutil.copyfile(ROOT / "data" / name, tmp_path / "data" / name)
    cfg = read_config(ROOT / "configs/devdex_docs.yaml")
    task = {
        "id": "fixture",
        "kind": "developer_retrieval",
        "role": "scored",
        "input": {"query": "Docs?"},
    }
    return cfg, [task], tmp_path


def test_resolved_defaults_and_secrets_absent(setup, monkeypatch):
    cfg, _, root = setup
    monkeypatch.setenv("ANTHROPIC_API_KEY", "never-persist-this")
    value = resolve_inputs(cfg, root)
    assert value["agent"]["requested_model"] == "claude-opus-4-8"
    assert value["agent"]["sampling"] == {"temperature": None, "top_p": None, "seed": None}
    assert value["limits"]["max_attempts"] == 60
    assert value["providers"]["exa"]["request_parameters_without_query"]["type"] == "auto"
    assert "never-persist-this" not in json.dumps(value)
    implicit = cfg.model_copy(update={"suite_options": {}})
    assert resolve_inputs(implicit, root)["agent"] == value["agent"]


def test_model_mismatch_rejected_before_execution(setup):
    cfg, _, root = setup
    cfg.suite_options["agent"]["model"] = "different-model"
    with pytest.raises(ValueError, match="pinned protocol"):
        resolve_inputs(cfg, root)


def test_protocol_byte_drift_invalidates_resume_and_frozen_snapshot(setup):
    cfg, tasks, root = setup
    directory = root / "runs" / cfg.run_id
    state = prepare_state(cfg, tasks, directory, root)
    assert (
        json.loads((directory / "effective-inputs.json").read_text())
        == state["input_identity"]["effective_inputs"]
    )
    path = root / "data/devdex-docs-protocol.json"
    path.write_text(path.read_text() + "\n")
    with pytest.raises(EvalError, match="fingerprint"):
        prepare_state(cfg, tasks, directory, root)


def test_requested_snapshot_uses_resolved_model_and_settings(setup):
    cfg, tasks, root = setup
    agent = resolve_agent(cfg.suite_options, root)
    snapshot = worker.request_snapshot(tasks[0], agent)
    assert snapshot["model"] == agent["requested_model"]
    assert snapshot["settings"] == agent["settings"]
    assert (
        snapshot["system_prompt"]
        == json.loads((root / agent["protocol_file"]).read_text())["system_prompt"]
    )


def test_custom_protocol_cannot_silently_replace_original_model(setup):
    cfg, _, root = setup
    path = root / "data/devdex-docs-protocol.json"
    protocol = json.loads(path.read_text())
    protocol["model"] = "another-model"
    path.write_text(json.dumps(protocol))
    cfg.suite_options["agent"]["model"] = "another-model"
    with pytest.raises(ValueError, match="must remain"):
        resolve_agent(cfg.suite_options, root)


@pytest.mark.parametrize(
    "endpoint",
    [
        None,
        "http://example.com",
        "https://user:secret@example.com",
        "https://example.com?api_key=secret",
        "https://example.com#secret",
    ],
)
def test_judge_endpoint_rejects_implicit_or_credential_bearing_transport(endpoint):
    from search_eval.effective_inputs import judge_endpoint

    with pytest.raises(ValueError):
        judge_endpoint(endpoint)


def test_judge_unset_sampling_is_not_sent_as_new_flags():
    from search_eval.effective_inputs import resolve_judge

    resolved = resolve_judge(
        {"model": "original", "base_url": "https://api.anthropic.com/v1", "temperature": 0.0}
    )
    assert all(resolved[k] is None for k in ("max_tokens", "top_p", "seed"))
    with pytest.raises(ValueError):
        resolve_judge({"top_p": 0.9})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "profile",
    [
        {"provider": "keenable", "mode": "pro", "tier": "authenticated"},
        {"provider": "exa", "mode": "auto", "tier": "authenticated"},
    ],
)
async def test_provider_snapshot_contract_is_actual_request(profile, monkeypatch):
    import httpx

    from search_eval.providers import request_spec, search_provider

    monkeypatch.setenv("KEENABLE_API_KEY", "fixture")
    monkeypatch.setenv("EXA_API_KEY", "fixture")
    spec = request_spec(profile)

    def handle(request):
        assert str(request.url) == spec["endpoint"]
        assert json.loads(request.content) == {
            "query": "question",
            **spec["request_parameters_without_query"],
        }
        return httpx.Response(200, json={"results": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        await search_provider("question", profile, client)
    assert spec["timeout_seconds"] == 20
    assert spec["automatic_retries"] == 0


@pytest.mark.parametrize("suite", ["martian", "devdex_docs"])
def test_scorer_revision_and_actual_files_are_frozen(suite):
    import hashlib

    cfg = read_config(ROOT / f"configs/{suite}.yaml")
    resolved = resolve_inputs(cfg, ROOT)
    scorer = resolved["scorer"]
    assert cfg.suite_options["scorer"] == {k: scorer[k] for k in ("name", "revision")}
    vendor = ROOT / "vendor" / ("martian/offline" if suite == "martian" else "devdex")
    for relative, expected in scorer["files"].items():
        assert hashlib.sha256((vendor / relative).read_bytes()).hexdigest() == expected
    cfg.suite_options["scorer"]["revision"] = "wrong-version"
    with pytest.raises(ValueError, match="scorer"):
        resolve_inputs(cfg, ROOT)


def test_judge_cannot_claim_retries_which_execution_ignores():
    from search_eval.effective_inputs import resolve_judge

    assert resolve_judge({})["max_retries"] == 1
    with pytest.raises(ValueError, match="one total attempt"):
        resolve_judge({"max_retries": 3})
    with pytest.raises(ValueError, match="Unknown judge input"):
        resolve_judge({"thinking": "high"})


def test_coderabbit_snapshot_matches_generated_request_and_keeps_model_unknown():
    from search_eval.core import coderabbit_settings

    cfg = read_config(ROOT / "configs/martian.yaml")
    resolved = resolve_inputs(cfg, ROOT)
    assert resolved["agent"]["requested_model"] is None
    for arm in cfg.arms:
        assert resolved["agent"]["requested_settings_by_arm"][arm.id] == coderabbit_settings(
            arm, cfg.profiles, cfg.review_profile
        )
    assert "Not an LLM seed" in resolved["schedule"]["seed_scope"]


def test_config_command_is_read_only_and_needs_no_live_allowance(setup, monkeypatch, capsys):
    from search_eval import cli

    cfg, _, root = setup
    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli, "read_config", lambda _: cfg)
    monkeypatch.setattr(cli, "run", lambda *a, **k: pytest.fail("Execution is forbidden"))
    monkeypatch.setattr(cli, "checks", lambda *a, **k: pytest.fail("Live gates are irrelevant"))
    monkeypatch.setattr("sys.argv", ["search-eval", "config", "--config", "example.yaml"])
    before = sorted(p.relative_to(root) for p in root.rglob("*"))
    assert cli.main() == 0
    saved = json.loads(capsys.readouterr().out)
    assert saved["scorer"]["name"] == "devdex-docs-original"
    assert saved["resolved_config"]["limits"]["max_attempts"] == 60
    assert before == sorted(p.relative_to(root) for p in root.rglob("*"))


def test_report_describes_saved_inputs_not_current_defaults(setup):
    from search_eval.report import input_summary

    cfg, _, root = setup
    saved = resolve_inputs(cfg, root)
    (root / "effective-inputs.json").write_text(json.dumps(saved))
    cfg.seed = 99
    cfg.suite_options["scorer"]["revision"] = "not-the-saved-revision"
    text = "\n".join(input_summary(root))
    assert "Schedule seed: `1729`" in text
    assert saved["scorer"]["revision"] in text
    assert "not-the-saved-revision" not in text
    assert "Requested settings do not prove service application" in text


def test_plan_command_does_not_freeze_run_state(setup, monkeypatch, capsys):
    from search_eval import cli

    cfg, tasks, root = setup
    monkeypatch.setattr(cli, "ROOT", root)
    monkeypatch.setattr(cli, "read_config", lambda _: cfg)
    from types import SimpleNamespace

    monkeypatch.setattr(cli, "suite_module", lambda _: SimpleNamespace(load_tasks=lambda _: tasks))
    monkeypatch.setattr("sys.argv", ["search-eval", "plan", "--config", "example.yaml"])
    assert cli.main() == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["live_calls"] == 0
    assert (root / "runs/plans" / f"{cfg.run_id}.json").is_file()
    assert not (root / "runs" / cfg.run_id / "state.json").exists()
