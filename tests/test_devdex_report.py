"""The generic offline renderer consumes DevDex's native series unchanged."""

import json

from search_eval.report import render
from search_eval.suites import devdex


def test_devdex_generic_fixture_report(tmp_path):
    fixture = json.loads((devdex.ROOT / "validation" / "devdex-native-parity.json").read_text())
    rows = {row["attempt_id"]: row for row in fixture["synthetic_records"]}
    attempts = []
    for episode in fixture["schedule"]:
        if episode["role"] != "development":
            continue
        path = tmp_path / episode["id"]
        path.mkdir()
        (path / "record.json").write_text(json.dumps(rows[episode["id"]]))
        attempts.append({**episode, "artifact_dir": str(path), "duration_seconds": 0.2})
    result = devdex.grade(attempts, tmp_path / "grading", {"evidence_type": "fixture"})
    assert result["complete"]
    assert len(result["report_series"]) == 4
    rendered = render(
        tmp_path,
        {"suite": "devdex_docs", "run_id": "devdex-fixture", "baseline": "exa",
         "repeats": 1, "fixture": True},
        attempts,
        result,
    )
    assert rendered["complete"]
    report = (tmp_path / "report.md").read_text()
    assert "SYNTHETIC FIXTURE, NOT MEASURED" in report
    assert "recall@10" in report and "MRR@10" in report
    assert (tmp_path / "results.png").stat().st_size > 100
    assert (tmp_path / "metrics.csv").read_text().count("fraction") == 4
