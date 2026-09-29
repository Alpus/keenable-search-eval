"""Real temporary Git repositories validate archive reconstruction before any remote write."""

import io
import json
import subprocess
import tarfile
from types import SimpleNamespace

import httpx
import pytest

from search_eval import github as gh
from search_eval.core import EvalError


def git(folder, *args, binary=False):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=folder,
        check=True,
        capture_output=True,
    ).stdout
    return result if binary else result.decode().strip()


@pytest.fixture
def transport(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    git(source, "init", "-b", "main")
    git(source, "config", "user.name", "Fixture")
    git(source, "config", "user.email", "fixture@example.invalid")
    (source / ".git/info/attributes").write_text("* -text\n")
    (source / ".gitattributes").write_text("* text=auto eol=lf\n")
    (source / "crlf.txt").write_bytes(b"original\r\nbytes\r\n")
    (source / "directory").mkdir()
    (source / "directory/file").write_text("target")
    (source / "directory-link").symlink_to("directory/")
    (source / ".gitignore").write_text("*.generated\n")
    (source / "tracked.generated").write_text("Tracked despite the ignore rule.\n")
    (source / "client.py").write_text("value = 1\n")
    (source / "obsolete.py").write_text("removed_in_head = True\n")
    (source / "run.sh").write_text("#!/bin/sh\nexit 0\n")
    (source / "run.sh").chmod(0o755)
    (source / "alias.py").symlink_to("client.py")
    git(source, "add", "--force", ".")
    git(source, "commit", "-m", "Original base")
    base = git(source, "rev-parse", "HEAD")
    base_tree = git(source, "rev-parse", "HEAD^{tree}")
    (source / "client.py").write_text("value = 2\n")
    (source / "obsolete.py").unlink()
    (source / "new.py").write_text("added_in_head = True\n")
    git(source, "add", "--all")
    git(source, "commit", "-m", "Original head")
    head = git(source, "rev-parse", "HEAD")
    head_tree = git(source, "rev-parse", "HEAD^{tree}")
    archives = {
        sha: git(source, "archive", "--format=tar.gz", "--prefix=source/", sha, binary=True)
        for sha in (base, head)
    }
    downloads, pushes, saves = [], [], []
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", str(remote))

    def get(path, **kwargs):
        assert kwargs == {"follow_redirects": True}
        sha = path.rsplit("/", 1)[-1]
        downloads.append(sha)
        return httpx.Response(
            200,
            content=archives[sha],
            request=httpx.Request("GET", "https://example.invalid" + path),
        )

    def call(method, path, **kwargs):
        assert method == "GET" and "/git/ref/heads/eval-" in path
        assert kwargs == {"missing_ok": True}
        return None

    original_git = gh._git

    def isolated_git(args, cwd, env=None):
        if "push" in args:
            assert "--atomic" in args
            assert args[-3] == "https://github.com/example/attempt.git"
            pushes.append(list(args))
            args = [
                str(remote) if x == "https://github.com/example/attempt.git" else x for x in args
            ]
        return original_git(args, cwd, env)

    monkeypatch.setattr(gh, "_git", isolated_git)
    api = SimpleNamespace(token="offline-fixture", client=SimpleNamespace(get=get), call=call)
    task = {
        "id": "historical",
        "role": "scored",
        "input": {
            "repo": "example/source",
            "base_sha": base,
            "head_sha": head,
            "base_tree": base_tree,
            "head_tree": head_tree,
        },
    }
    artifact = tmp_path / "artifacts"
    artifact.mkdir()
    attempt = {}
    return SimpleNamespace(
        api=api,
        task=task,
        source=source,
        remote=remote,
        artifact=artifact,
        attempt=attempt,
        archives=archives,
        downloads=downloads,
        pushes=pushes,
        saves=saves,
        save=lambda: saves.append(dict(attempt)),
    )


def execute(t, settings=None):
    return gh.push_inputs(
        t.api,
        t.task,
        settings or {"reviews": {"auto_review": {"enabled": False}}},
        "example/attempt",
        t.artifact,
        t.attempt,
        t.save,
    )


def test_exact_archive_trees_preserve_ignored_files_modes_symlinks_and_deletions(transport):
    t = transport
    head = execute(t)
    assert len(t.pushes) == 1 and len(t.downloads) == 2
    assert git(t.remote, "rev-parse", "eval-head") == head == t.attempt["expected_head"]
    assert git(t.remote, "rev-parse", "eval-head^") == t.attempt["expected_base"]
    for branch, field in (("eval-base", "base_tree"), ("eval-head", "head_tree")):
        evidence = json.loads((t.artifact / f"{branch}-source.json").read_text())
        assert evidence["verified"] and evidence["actual_tree"] == t.task["input"][field]
        assert (
            git(t.remote, "show", f"{branch}:tracked.generated")
            == "Tracked despite the ignore rule."
        )
        assert git(t.remote, "ls-tree", branch, "run.sh").startswith("100755")
        assert git(t.remote, "ls-tree", branch, "alias.py").startswith("120000")
        assert git(t.remote, "show", f"{branch}:crlf.txt", binary=True) == b"original\r\nbytes\r\n"
        assert git(t.remote, "show", f"{branch}:directory-link", binary=True) == b"directory/"
        assert "auto_review" in git(t.remote, "show", f"{branch}:.coderabbit.yaml")
    assert git(t.remote, "ls-tree", "eval-head", "obsolete.py") == ""
    assert git(t.remote, "show", "eval-head:client.py") == "value = 2"
    # Identical injected configuration creates no artificial configuration diff.
    assert ".coderabbit.yaml" not in git(t.remote, "diff", "--name-only", "eval-base", "eval-head")
    assert t.saves[-1]["push_intent"] is True


@pytest.mark.parametrize("which", ["base_tree", "head_tree"])
def test_tree_mismatch_blocks_every_push(transport, which):
    t = transport
    t.task["input"][which] = "0" * 40
    with pytest.raises(EvalError, match="differs from the pinned Git tree"):
        execute(t)
    assert t.pushes == [] and t.saves == []
    assert not list(t.remote.glob("refs/heads/*"))


def test_fixed_control_checks_original_tree_before_applying_patch(transport):
    t = transport
    t.task["role"] = "control"
    t.task["input"]["head_overrides"] = [
        {
            "path": "client.py",
            "old_text": "value = 2",
            "new_text": "value = 3",
            "expected_old_count": 1,
        }
    ]
    execute(t)
    evidence = json.loads((t.artifact / "eval-head-source.json").read_text())
    assert evidence["actual_tree"] == t.task["input"]["head_tree"]
    assert git(t.remote, "show", "eval-head:client.py") == "value = 3"
    assert git(t.remote, "show", "eval-base:client.py") == "value = 1"


def test_mismatched_control_patch_blocks_push(transport):
    t = transport
    t.task["input"]["head_overrides"] = [
        {
            "path": "client.py",
            "old_text": "not in source",
            "new_text": "fixed",
            "expected_old_count": 1,
        }
    ]
    with pytest.raises(EvalError, match="Control patch no longer matches"):
        execute(t)
    assert not t.pushes


def test_authored_fixture_uses_distinct_full_file_sets_without_archives(transport):
    t = transport
    t.task = {
        "role": "development",
        "input": {
            "base_files": {"nested/client.py": "value = 1\n", "old.txt": "remove\n"},
            "head_files": {
                "nested/client.py": "value = 2\n",
                ".gitignore": "*.generated\n",
                "new.generated": "still included\n",
            },
        },
    }
    execute(t)
    assert t.downloads == [] and len(t.pushes) == 1
    assert git(t.remote, "show", "eval-base:nested/client.py") == "value = 1"
    assert git(t.remote, "show", "eval-head:nested/client.py") == "value = 2"
    assert git(t.remote, "ls-tree", "eval-head", "old.txt") == ""
    assert git(t.remote, "show", "eval-head:new.generated") == "still included"
    assert not list(t.artifact.glob("*-source.json"))


def test_archive_missing_tracked_ignored_file_blocks_push(transport):
    t = transport
    sha = t.task["input"]["head_sha"]
    modified = io.BytesIO()
    with tarfile.open(fileobj=io.BytesIO(t.archives[sha]), mode="r:gz") as original:
        with tarfile.open(fileobj=modified, mode="w:gz") as replacement:
            for member in original.getmembers():
                if member.name == "source/tracked.generated":
                    continue
                replacement.addfile(
                    member, original.extractfile(member) if member.isfile() else None
                )
    t.archives[sha] = modified.getvalue()
    with pytest.raises(EvalError, match="differs from the pinned Git tree"):
        execute(t)
    assert not t.pushes


@pytest.mark.parametrize("target", ["../../escape", "/absolute/escape"])
def test_archive_filter_rejects_escaping_symlinks(tmp_path, target):
    member = tarfile.TarInfo("source/link")
    member.type = tarfile.SYMTYPE
    member.linkname = target
    with pytest.raises(tarfile.FilterError):
        gh._archive_filter(member, tmp_path)


def test_archive_filter_rejects_symlink_chain_escape(tmp_path):
    (tmp_path / "source").mkdir()
    (tmp_path / "source/other").symlink_to(tmp_path.parent)
    member = tarfile.TarInfo("source/link")
    member.type = tarfile.SYMTYPE
    member.linkname = "other/escape"
    with pytest.raises(tarfile.FilterError):
        gh._archive_filter(member, tmp_path)


def test_raw_staging_handles_quoted_unicode_and_newline_paths(tmp_path):
    git(tmp_path, "init")
    for name in ['quote"file', "line\nbreak", "ünicode"]:
        (tmp_path / name).write_bytes(b"raw\r\n")
    gh._stage_snapshot(tmp_path)
    for name in ['quote"file', "line\nbreak", "ünicode"]:
        assert git(tmp_path, "show", ":" + name, binary=True) == b"raw\r\n"
