from datetime import datetime, timedelta, timezone

import pytest

from search_eval.core import (
    Arm,
    EvalError,
    Experiment,
    atomic_json,
    coderabbit_settings,
    lock,
    prepare_state,
    schedule,
    verify_allowance,
)


def config(**changes):
    values = dict(
        run_id="test-run",
        suite="martian",
        manifest="data.json",
        baseline="native",
        arms=[
            dict(id="native", native_search=True, mcp=[]),
            dict(id="none", native_search=False, mcp=[]),
            dict(id="keenable", mcp=["keenable"]),
            dict(id="exa", mcp=["exa"]),
        ],
        profiles={
            "keenable": dict(
                provider="keenable", endpoint="search-a", connection="eval-search-a", token_env="A"
            ),
            "exa": dict(
                provider="exa", endpoint="search-b", connection="eval-search-b", token_env="B"
            ),
        },
        limits=dict(max_attempts=100),
        repeats=1,
    )
    values.update(changes)
    return Experiment.model_validate(values)


def tasks():
    return [
        dict(id=str(i), role="scored", kind="pr_review", input={"body": f"case{i}"})
        for i in range(2)
    ]


def test_repeats_are_distinct_and_deterministic():
    cfg = config(repeats=3)
    plan = schedule(cfg, tasks())
    assert len(plan) == len({a["id"] for a in plan}) == 24
    assert plan == schedule(cfg, tasks())
    assert len({a["id"] for a in schedule(config(seed=2, repeats=3), tasks())}) == 24


def test_cap_duplicate_and_unknown_config():
    with pytest.raises(EvalError):
        schedule(config(limits={"max_attempts": 2}), tasks())
    with pytest.raises(ValueError):
        config(arms=[{"id": "native", "mcp": ["unknown"]}])
    with pytest.raises(ValueError):
        config(repeats=True)
    with pytest.raises(ValueError):
        config(baseline="absent")


def test_combination_is_data_not_a_special_mode():
    cfg = config()
    arm = Arm(id="any_new_name", native_search=True, mcp=["keenable"])
    value = coderabbit_settings(arm, cfg.profiles)["knowledge_base"]
    assert value["web_search"] == {"enabled": True}
    assert value["mcp"] == {"usage": "enabled", "disabled_servers": ["eval-search-b"]}
    both = Arm(id="two", native_search=False, mcp=["keenable", "exa"])
    assert (
        coderabbit_settings(both, cfg.profiles)["knowledge_base"]["mcp"]["disabled_servers"] == []
    )


def test_resume_fingerprints_inputs_config_and_code(tmp_path):
    (tmp_path / "data.json").write_text("{}")
    (tmp_path / "src").mkdir()
    code = tmp_path / "src/example.py"
    code.write_text("v=1")
    cfg = config()
    directory = tmp_path / "runs/test-run"
    first = prepare_state(cfg, tasks(), directory, tmp_path)
    first["attempts"][0]["status"] = "completed"
    atomic_json(directory / "state.json", first)
    assert prepare_state(cfg, tasks(), directory, tmp_path) == first
    with pytest.raises(EvalError):
        prepare_state(config(repeats=2), tasks(), directory, tmp_path)
    code.write_text("v=2")
    with pytest.raises(EvalError):
        prepare_state(cfg, tasks(), directory, tmp_path)


def test_single_runner_lock(tmp_path):
    with lock(tmp_path / "runner.lock"):
        with pytest.raises(EvalError):
            with lock(tmp_path / "runner.lock"):
                pass
    with lock(tmp_path / "runner.lock"):
        pass


def test_verified_allowance_binds_scope_and_expiry(tmp_path):
    cfg = config(allowance_file="allowance.json")
    with pytest.raises(EvalError):
        verify_allowance(cfg, tmp_path, 8)
    record = dict(
        verified=True,
        suite="martian",
        run_id="test-run",
        role="scored",
        evidence="verified-dashboard-snapshot",
        max_attempts=8,
        valid_until=(datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        additional_spending_usd=0,
        automatic_overage_disabled=True,
        services=["coderabbit", "martian_judge", "keenable", "exa"],
    )
    atomic_json(tmp_path / "allowance.json", record)
    assert verify_allowance(cfg, tmp_path, 8) == record
    for field, value in [
        ("run_id", "another"),
        ("max_attempts", 7),
        ("automatic_overage_disabled", False),
        ("services", ["coderabbit"]),
        ("valid_until", "2020-01-01T00:00:00+00:00"),
    ]:
        atomic_json(tmp_path / "allowance.json", {**record, field: value})
        with pytest.raises(EvalError):
            verify_allowance(cfg, tmp_path, 8)


def test_frozen_manifest_survives_source_edit_and_detects_tampering(tmp_path):
    from search_eval.core import frozen_manifest, verify_run_code

    (tmp_path / "data.json").write_text('{"original":true}\n')
    directory = tmp_path / "runs/test-run"
    state = prepare_state(config(), tasks(), directory, tmp_path)
    (tmp_path / "data.json").write_text('{"original":false}')
    assert frozen_manifest(directory, state) == {"original": True}
    verify_run_code(state, tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src/change.py").write_text("changed=True")
    with pytest.raises(EvalError, match="source code"):
        verify_run_code(state, tmp_path)
    (directory / "manifest.json").write_text("{}")
    with pytest.raises(EvalError, match="hash changed"):
        frozen_manifest(directory, state)


def test_review_profile_is_explicit_and_rendered():
    cfg = config()
    assert cfg.review_profile == "chill"
    assert (
        coderabbit_settings(cfg.arms[0], cfg.profiles, cfg.review_profile)["reviews"]["profile"]
        == "chill"
    )
    changed = config(review_profile="assertive")
    assert (
        coderabbit_settings(changed.arms[0], changed.profiles, changed.review_profile)["reviews"][
            "profile"
        ]
        == "assertive"
    )


def test_operator_allowance_is_explicit_bounded_and_reusable(tmp_path):
    from search_eval.core import record_operator_allowance

    cfg = config(suite_options={"max_judge_usd": 2})
    path = record_operator_allowance(cfg, tmp_path, existing_model_credit_usd=3)
    assert path.is_relative_to(tmp_path)
    record = verify_allowance(cfg, tmp_path, 40)
    assert record["verification_method"] == "operator_attestation"
    assert record["max_judge_usd"] == 2
    assert record["existing_model_credit_usd"] == 3
    assert record["additional_spending_usd"] == 0
    assert record["automatic_overage_disabled"] is True
    assert "No account balance was read" in record["evidence"]
    with pytest.raises(EvalError, match="inside this checkout"):
        record_operator_allowance(config(allowance_file="../escape.json"), tmp_path)

    with pytest.raises(EvalError, match="covering"):
        record_operator_allowance(cfg, tmp_path, existing_model_credit_usd=1)
