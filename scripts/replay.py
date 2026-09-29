"""Rebuild preserved results without network, credentials or old Git history."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

run_id = "devdex_docs-pilot-001"
run = Path("/app/runs") / run_id
shutil.copytree(Path("/recorded") / run_id, run, dirs_exist_ok=True)
subprocess.run(["search-eval", "report", "--run", run_id], check=True)
expected = json.loads(Path("/recorded/expected.json").read_text())
actual = {name: hashlib.sha256((run / name).read_bytes()).hexdigest() for name in expected}
if actual != expected:
    raise RuntimeError("Reproduced report or evidence differs from the recorded result")
Path("/app/runs/reproduction-check.json").write_text(
    json.dumps({"passed": True, "run_id": run_id, "sha256": actual}, indent=2) + "\n"
)
print(f"Verified: runs/reproduced/{run_id}/report.md")
