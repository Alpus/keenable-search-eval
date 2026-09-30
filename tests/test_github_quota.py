"""Offline tests for durable, rolling included-review quota pacing."""

import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from search_eval import github as gh
from search_eval.core import EvalError, Limits

START = datetime(2026, 9, 29, 10, tzinfo=timezone.utc).timestamp()


def stamp(seconds):
    return datetime.fromtimestamp(START + seconds, timezone.utc).isoformat()


def event(name, seconds, *, actual=True, owner="Alpus"):
    row = {"id": name, "repository": f"{owner}/se-{name}", "pr_number": 1}
    row["trigger_time" if actual else "trigger_intent"] = stamp(seconds)
    if actual:
        row["trigger_id"] = sum(map(ord, name))
    return row


def save(root, name, data):
    path = root / "runs" / name / "state.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def test_ledger_bootstrap_cross_run_duplicates_and_unconfirmed_intent(tmp_path):
    bootstrap = event("bootstrap", 0)
    save(tmp_path, "bootstrap", bootstrap)
    pending = event("pending", 10, actual=False)
    first = event("first", 5)
    save(
        tmp_path,
        "run-a",
        {"attempts": [first, pending, {"id": "precreated", "repository": "Alpus/se-empty"}]},
    )
    copied = dict(first, trigger_intent=stamp(4))
    save(tmp_path, "run-copy", {"attempts": [copied, event("someone-else", 9, owner="other")]})
    save(tmp_path, "devdex", {"attempts": [{"id": "retrieval", "started_at": stamp(1)}]})
    assert sorted(gh.review_events(tmp_path, "alpus")) == [START, START + 5, START + 10]


def test_actual_trigger_supersedes_saved_uncertain_reservation(tmp_path):
    row = event("same", 0, actual=False)
    save(tmp_path, "uncertain-copy", {"attempts": [row]})
    save(tmp_path, "resolved", {"attempts": [event("same", 8)]})
    assert gh.review_events(tmp_path, "Alpus") == [START + 8]


@pytest.mark.parametrize(
    "broken",
    [
        {"repository": None},
        {"pr_number": None},
        {"trigger_time": "bad"},
        {"trigger_time": "2026-09-29T10:00:00"},
        {"trigger_time": None},
    ],
)
def test_ambiguous_trigger_evidence_blocks(tmp_path, broken):
    row = event("bad", 0)
    row.update(broken)
    save(tmp_path, "broken", {"attempts": [row]})
    with pytest.raises(EvalError, match="Cannot establish review quota"):
        gh.review_events(tmp_path, "Alpus")


def test_multiple_actual_triggers_for_one_pr_do_not_undercount(tmp_path):
    row = event("same", 0)
    save(tmp_path, "a", {"attempts": [row]})
    save(tmp_path, "b", {"attempts": [dict(row, trigger_id=row["trigger_id"] + 1)]})
    with pytest.raises(EvalError, match="multiple triggers"):
        gh.review_events(tmp_path, "Alpus")


@pytest.fixture
def pacing(tmp_path, monkeypatch):
    attempt = {"id": "new", "allowance_valid_until": stamp(7200)}
    save(tmp_path, "new-run", {"attempts": [attempt]})
    artifact = tmp_path / "runs/new-run/attempts/new"
    artifact.mkdir(parents=True)
    config = SimpleNamespace(
        run_id="new-run",
        github_owner="Alpus",
        limits=Limits(max_attempts=1, max_review_events_per_hour=2),
    )
    clock = {"now": START + 3590}
    sleeps, allowances = [], []
    monkeypatch.setattr(gh.time, "time", lambda: clock["now"])

    def sleep(seconds):
        assert 0 < seconds <= 30
        sleeps.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(gh.time, "sleep", sleep)
    monkeypatch.setattr(gh, "verify_allowance", lambda *args: allowances.append(args))
    return config, attempt, artifact, clock, sleeps, allowances


def test_rolling_window_waits_through_buffer_rechecks_allowance(tmp_path, pacing, capsys):
    config, attempt, artifact, clock, sleeps, allowances = pacing
    save(
        tmp_path,
        "previous",
        {"attempts": [event("old", 0), event("recent", 60, actual=False), event("expired", -100)]},
    )
    gh.pace_review_trigger(config, attempt, artifact)
    assert clock["now"] == START + 3602
    assert sleeps == [12]
    assert len(allowances) == 2
    assert "waiting until" in capsys.readouterr().out
    assert "trigger_intent" not in attempt and "expires_at" not in attempt


def test_tighter_cap_waits_until_enough_events_expire(tmp_path, pacing):
    config, attempt, artifact, clock, sleeps, allowances = pacing
    save(tmp_path, "previous", {"attempts": [event("a", 0), event("b", 100), event("c", 200)]})
    gh.pace_review_trigger(config, attempt, artifact)
    assert clock["now"] == START + 3702
    assert sum(sleeps) == 112 and max(sleeps) <= 30
    assert len(allowances) == len(sleeps) + 1


def test_allowance_revoked_during_wait_blocks_without_reservation(tmp_path, pacing, monkeypatch):
    config, attempt, artifact, _, sleeps, _ = pacing
    save(tmp_path, "previous", {"attempts": [event("a", 0), event("b", 60)]})

    def verify(*args):
        if sleeps:
            raise EvalError("Allowance revoked")

    monkeypatch.setattr(gh, "verify_allowance", verify)
    with pytest.raises(EvalError, match="revoked"):
        gh.pace_review_trigger(config, attempt, artifact)
    assert "trigger_intent" not in attempt


def test_allowance_expiry_interrupts_long_wait(tmp_path, pacing):
    config, attempt, artifact, clock, sleeps, _ = pacing
    attempt["allowance_valid_until"] = stamp(3595)
    save(tmp_path, "previous", {"attempts": [event("a", 0), event("b", 60)]})
    with pytest.raises(EvalError, match="Allowance expired"):
        gh.pace_review_trigger(config, attempt, artifact)
    assert sleeps == [5] and clock["now"] == START + 3595


def test_expired_events_do_not_wait(tmp_path, pacing):
    config, attempt, artifact, _, sleeps, allowances = pacing
    save(tmp_path, "previous", {"attempts": [event("a", -20), event("b", -30)]})
    gh.pace_review_trigger(config, attempt, artifact)
    assert sleeps == [] and len(allowances) == 1


def test_default_disabled_does_not_read_state_or_allowance(tmp_path, monkeypatch):
    config = SimpleNamespace(limits=Limits(max_attempts=1))
    monkeypatch.setattr(
        gh, "verify_allowance", lambda *args: pytest.fail("unexpected allowance read")
    )
    gh.pace_review_trigger(config, {}, tmp_path)
    assert config.limits.max_review_events_per_hour is None


@pytest.mark.parametrize("cap", [0, -1, True, 1.5])
def test_invalid_caps_rejected(cap):
    with pytest.raises(ValidationError):
        Limits(max_attempts=1, max_review_events_per_hour=cap)


def test_conflicting_uncertain_attempts_for_one_pr_block(tmp_path):
    row = event("same", 0, actual=False)
    save(tmp_path, "a", {"attempts": [row]})
    save(tmp_path, "b", {"attempts": [dict(row, id="different")]})
    with pytest.raises(EvalError, match="multiple attempt identities"):
        gh.review_events(tmp_path, "Alpus")


def test_completed_review_ages_from_completion_not_trigger(tmp_path, pacing):
    config, attempt, artifact, clock, sleeps, _ = pacing
    config.limits.max_review_events_per_hour = 1
    completed = dict(event("done", 0), status="completed", completed_at=stamp(180))
    save(tmp_path, "finished", {"attempts": [completed]})
    save(tmp_path, "older-copy", {"attempts": [event("done", 0)]})
    assert gh.review_events(tmp_path, "Alpus") == [START + 180]
    gh.pace_review_trigger(config, attempt, artifact)
    assert clock["now"] == START + 3782
    assert sum(sleeps) == 192


@pytest.mark.parametrize("completed", [None, "bad", "2026-09-29T10:01:00", stamp(-1)])
def test_bad_completion_timestamp_blocks_quota_aging(tmp_path, completed):
    row = dict(event("done", 0), status="completed", completed_at=completed)
    save(tmp_path, "finished", {"attempts": [row]})
    with pytest.raises(EvalError, match="Cannot establish review quota"):
        gh.review_events(tmp_path, "Alpus")


def test_nonblocking_quota_check_returns_delay_without_sleep_or_reservation(tmp_path, pacing):
    config, attempt, artifact, clock, sleeps, allowances = pacing
    save(tmp_path, "previous", {"attempts": [event("old", 0), event("recent", 60)]})
    assert gh.pace_review_trigger(config, attempt, artifact, nonblocking=True) == 12
    assert clock["now"] == START + 3590 and sleeps == [] and len(allowances) == 1
    assert "trigger_intent" not in attempt and "expires_at" not in attempt
