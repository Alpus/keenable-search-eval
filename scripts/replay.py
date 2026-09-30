"""Rebuild preserved results without network, credentials or old Git history."""

import hashlib
import json
import shutil
import subprocess
from pathlib import Path

runs = Path('/app/runs')
results = []
for run_id, runtime, hashes in (
    ('devdex_docs-pilot-001', '/app', 'expected.json'),
    ('martian-pilot-002', '/martian', 'martian-expected.json'),
):
    run = runs / run_id
    shutil.copytree(Path('/recorded') / run_id, run, dirs_exist_ok=True)
    subprocess.run(
        [
            f'{runtime}/.venv/bin/python',
            '-c',
            'import sys; from pathlib import Path; '
            'from search_eval.report import build_report; build_report(Path(sys.argv[1]))',
            str(run),
        ],
        cwd=runtime,
        check=True,
    )
    expected = json.loads((Path('/recorded') / hashes).read_text())
    actual = {name: hashlib.sha256((run / name).read_bytes()).hexdigest() for name in expected}
    if actual != expected:
        changed = [name for name in expected if actual[name] != expected[name]]
        raise RuntimeError(f'{run_id}: reproduced files differ: {changed}')
    results.append({'passed': True, 'run_id': run_id, 'sha256': actual})
    print(f'Verified: runs/reproduced/{run_id}/report.md')
(runs / 'reproduction-check.json').write_text(
    json.dumps({'passed': True, 'runs': results}, indent=2) + '\n'
)
