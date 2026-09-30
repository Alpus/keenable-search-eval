"""Portable evidence bundles replace dependence on historical Git objects."""

import hashlib
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_bundled_dependencies_match_checksums_and_are_self_contained():
    for line in (ROOT / "assets/SHA256SUMS").read_text().splitlines():
        expected, name = line.split()
        path = ROOT / "assets" / name
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected
        with tarfile.open(path) as archive:
            names = set(archive.getnames())
            assert all(not p.startswith("/") and ".." not in Path(p).parts for p in names)
            assert not any(".env" in Path(p).parts or ".git" in Path(p).parts for p in names)
            if name == "devdex-pilot.tar.gz":
                assert "runtime/uv.lock" in names
                assert "expected.json" in names
                assert "runs/devdex_docs-pilot-001/state.json" in names
            elif name == "martian-search11.tar.gz":
                assert "runtime/uv.lock" in names
                assert "runtime/vendor/martian/offline/analysis/score_profiles.py" in names
                assert "expected.json" in names
                assert "runs/martian-search11-001/state.json" in names
                assert "provenance/zero-findings-fix/recovery-receipt.json" in names
            else:
                assert "vendor/devdex/devdex/scorer/suite.py" in names
                assert "vendor/martian/offline/analysis/score_profiles.py" in names
