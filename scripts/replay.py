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
    ('martian-search11-001', '/search11', 'search11-expected.json'),
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
# Preserve the original failed control evidence beside the explicit derived report.
control_id = 'martian-controls-002'
shutil.copytree(Path('/recorded') / control_id, runs / control_id, dirs_exist_ok=True)
derived_id = f'{control_id}-completed'
derived = runs / derived_id
if derived.exists():
    shutil.rmtree(derived)
subprocess.run(
    [
        '/martian/.venv/bin/python', '/reconcile_martian.py', 'report',
        '--source', '/martian',
        '--sidecar', '/recorded/control-reconciliation',
        '--main-run', '/martian/runs/martian-pilot-002',
        '--derived-report', str(derived),
    ],
    cwd='/martian',
    check=True,
    stdout=subprocess.DEVNULL,
)
expected = json.loads(Path('/recorded/controls-expected.json').read_text())
actual = {name: hashlib.sha256((derived / name).read_bytes()).hexdigest() for name in expected}
if actual != expected:
    changed = [name for name in expected if actual[name] != expected[name]]
    raise RuntimeError(f'{derived_id}: reproduced files differ: {changed}')
results.append({'passed': True, 'run_id': derived_id, 'sha256': actual})
print(f'Verified: runs/reproduced/{derived_id}/report.md')
(runs / 'reproduction-check.json').write_text(
    json.dumps({'passed': True, 'runs': results}, indent=2) + '\n'
)
