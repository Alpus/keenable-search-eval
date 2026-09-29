"""Account preparation reuses the real config/schedule/repository interfaces."""

import importlib.util
import json
import os
import shutil
from pathlib import Path

import pytest

from search_eval.core import EvalError, atomic_json

spec = importlib.util.spec_from_file_location(
    "prepare", Path(__file__).parents[1] / "scripts/prepare.py"
)
prepare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(prepare)
PRESETS = [
    Path("configs") / n for n in ("devdex_docs.yaml", "martian.yaml", "martian-controls.yaml")
]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    source = Path(__file__).parents[1]
    for name in ("configs", "data"):
        shutil.copytree(source / name, tmp_path / name)
    (tmp_path / ".env").write_text("# existing credentials stay intact\n")
    (tmp_path / ".gateway").mkdir()
    for key in ("GITHUB_TOKEN", "KEENABLE_API_KEY", "EXA_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.setenv(key, "test-value")
    for key in ("MARTIAN_API_KEY", "MCP_SEARCH_A_TOKEN", "MCP_SEARCH_B_TOKEN", "PUBLIC_MCP_URL"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("SEARCH_EVAL_ROUTING", str(tmp_path / ".gateway/routing.json"))
    return tmp_path


def resolve(root, url="https://test.example"):
    return prepare.resolve_configs(PRESETS, "example-user", url, root)


def test_init_preserves_env_and_tokens_on_repeat(workspace):
    assert prepare.initialize(PRESETS, workspace) == ""
    first = (workspace / ".env").read_bytes()
    assert b"# existing credentials stay intact" in first
    assert os.environ["MARTIAN_API_KEY"] == "test-value"
    assert os.environ["MCP_SEARCH_A_TOKEN"] != os.environ["MCP_SEARCH_B_TOKEN"]
    assert len(os.environ["MCP_SEARCH_A_TOKEN"]) >= 40
    prepare.initialize(PRESETS, workspace)
    assert (workspace / ".env").read_bytes() == first
    assert (workspace / ".env").stat().st_mode & 0o777 == 0o600
    assert len(json.loads((workspace / ".gateway/routing.json").read_text())["profiles"]) == 2


def test_missing_keys_stop_before_token_generation(workspace, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN")
    with pytest.raises(EvalError, match="GITHUB_TOKEN"):
        prepare.initialize(PRESETS, workspace)
    assert "MCP_SEARCH_A_TOKEN" not in os.environ


def test_config_resolution_preserves_models_and_refuses_changed_resume(workspace):
    result = resolve(workspace)
    for source, (_, c) in zip(PRESETS, result):
        original = prepare.read_config(workspace / source)
        assert c.suite_options == original.suite_options
        assert c.github_owner == "example-user"
    c = result[1][1]
    atomic_json(workspace / "runs" / c.run_id / "state.json", {"config": c.model_dump()})
    assert resolve(workspace) == result
    with pytest.raises(EvalError, match="Frozen inputs changed"):
        resolve(workspace, "https://new.example")


class FakeGitHub:
    def __init__(self):
        self.repos = {}
        self.creates = 0

    def call(self, method, path, *, body=None, missing_ok=False):
        if path == "/user":
            return {"login": "example-user"}
        if method == "POST":
            self.creates += 1
            name = "example-user/" + body["name"]
            self.repos[name] = dict(body, id=self.creates, full_name=name)
            return self.repos[name]
        if path.endswith("/actions/permissions"):
            return {"enabled": False}
        return self.repos.get(path.removeprefix("/repos/"))


def test_repo_preparation_reuses_all_48_and_refuses_unrelated_repo(workspace):
    api = FakeGitHub()
    resolved = resolve(workspace)
    first = prepare.prepare_repositories(resolved, api, workspace)
    assert len(first) == api.creates == 48
    assert prepare.prepare_repositories(resolved, api, workspace) == first
    assert api.creates == 48
    api.repos[first[0]["name"]]["description"] = "unrelated"
    with pytest.raises(EvalError, match="identity/visibility mismatch"):
        prepare.prepare_repositories(resolved, api, workspace)


def test_platform_confirmation_is_bound_to_repo_ids_url_and_token(workspace, monkeypatch):
    prepare.initialize(PRESETS, workspace)
    resolved = resolve(workspace)
    repos = [{"name": "example-user/se-one", "id": 1}]
    binding = prepare.platform_checklist(resolved, repos, workspace)
    text = (workspace / ".gateway/coderabbit-setup.md").read_text()
    assert "MCP_SEARCH_A_TOKEN" in text
    assert os.environ["MCP_SEARCH_A_TOKEN"] not in text
    assert "https://test.example/mcp/search-a" in text
    prepare.confirm_platform(binding, workspace, lambda _: "ready")
    prepare.confirm_platform(binding, workspace, lambda _: pytest.fail("repeat prompted"))
    monkeypatch.setenv("MCP_SEARCH_A_TOKEN", "rotated")
    changed = prepare.platform_checklist(resolved, repos, workspace)
    assert changed != binding
    with pytest.raises(EvalError, match="not confirmed"):
        prepare.confirm_platform(changed, workspace, lambda _: "no")
    receipt = json.loads((workspace / ".gateway/platform-confirmation.json").read_text())
    assert receipt["remote_verified"] is False


def test_combined_quota_confirmation_is_explicit_and_reused(workspace):
    resolved = resolve(workspace)
    answers = iter(["18", "yes"])
    prepare.approve_quota(resolved, workspace, lambda _: next(answers))
    prepare.approve_quota(resolved, workspace, lambda _: pytest.fail("unexpired approval prompted"))
    for _, config in resolved:
        data = json.loads((workspace / config.allowance_file).read_text())
        assert data["verification_method"] == "operator_attestation"
        assert data["additional_spending_usd"] == 0


@pytest.mark.parametrize("value", ["17", "nan", "inf"])
def test_quota_requires_combined_cap(workspace, value):
    with pytest.raises(EvalError, match="combined Martian"):
        prepare.approve_quota(resolve(workspace), workspace, lambda _: value)
    assert not list((workspace / "validation").glob("*-allowance.json"))


@pytest.mark.parametrize(
    "url", ["http://plain.example", "https://u:p@test.example", "https://test.example/path"]
)
def test_invalid_endpoint_rejected(url):
    with pytest.raises(EvalError, match="HTTPS origin"):
        prepare.validate_url(url)
