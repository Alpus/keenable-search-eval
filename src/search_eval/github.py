"""GitHub transport and a conservative, resumable CodeRabbit review lifecycle."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tarfile
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import httpx
import yaml

from .core import EvalError, atomic_json, coderabbit_settings, now, verify_allowance


class GitHubReadError(EvalError):
    """A transient GET failed. No write may be inferred or repeated from this error."""


def confirmed_trigger(attempt):
    """A complete saved receipt permits read-only recovery, never another trigger."""
    try:
        return (
            type(attempt.get("trigger_id")) is int
            and attempt["trigger_id"] > 0
            and isinstance(attempt.get("repository"), str)
            and re.fullmatch(r"[^/]+/[^/]+", attempt["repository"]) is not None
            and type(attempt.get("pr_number")) is int
            and attempt["pr_number"] > 0
            and re.fullmatch(r"[0-9a-f]{40}", attempt.get("head_sha", "")) is not None
            and re.fullmatch(r"[0-9a-f]{40}", attempt.get("expected_base", "")) is not None
            and all(
                datetime.fromisoformat(attempt[k].replace("Z", "+00:00")).tzinfo is not None
                for k in ("trigger_time", "expires_at")
            )
        )
    except (KeyError, ValueError, TypeError, AttributeError):
        return False


class GitHub:
    def __init__(self, token: str, client=None):
        self.token = token
        self.client = client or httpx.Client(
            base_url="https://api.github.com",
            timeout=45,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )

    def call(self, method, path, *, body=None, params=None, missing_ok=False):
        # Writes are never automatically retried: callers reconcile saved intent first.
        try:
            response = self.client.request(method, path, json=body, params=params)
        except httpx.TransportError as exc:
            if method == "GET":
                raise GitHubReadError(f"GitHub GET {path}: transient transport failure") from exc
            raise
        if missing_ok and response.status_code == 404:
            return None
        # GitHub returns this specific conflict for ref reads on an empty repo.
        if (
            missing_ok
            and method == "GET"
            and response.status_code == 409
            and re.fullmatch(r"/repos/[^/]+/[^/]+/git/ref/.+", path)
        ):
            try:
                error = response.json()
            except ValueError:
                error = None
            if isinstance(error, dict) and error.get("message") == "Git Repository is empty.":
                return None
        if method == "GET" and (response.status_code in {408, 429} or response.status_code >= 500):
            raise GitHubReadError(f"GitHub GET {path}: HTTP {response.status_code}")
        if response.status_code >= 400:
            raise EvalError(
                f"GitHub {method} {path}: HTTP {response.status_code}; inspect saved state before retry"
            )
        return response.json() if response.content else {}

    def pages(self, path, key=None):
        pages = []
        for page in range(1, 1001):
            params = {"per_page": 30, "page": page}
            body = self.call("GET", path, params=params)
            items = body[key] if key else body
            pages.append(
                {
                    "page": page,
                    "per_page": 30,
                    "parameters": params,
                    "endpoint": path,
                    "observed_at": now(),
                    "items": items,
                }
            )
            if len(items) < 30:
                return pages
        raise EvalError("Pagination limit exceeded; collection is incomplete")


def token_from_environment():
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise EvalError("Set GITHUB_TOKEN in the local .env; never commit credentials")
    return token


def collect(api: GitHub, repo: str, pr: int, head: str):
    return {
        "review_comments": api.pages(f"/repos/{repo}/pulls/{pr}/comments"),
        "reviews": api.pages(f"/repos/{repo}/pulls/{pr}/reviews"),
        "issue_comments": api.pages(f"/repos/{repo}/issues/{pr}/comments"),
        "checks": api.pages(f"/repos/{repo}/commits/{head}/check-runs", key="check_runs"),
        "commit_statuses": api.pages(f"/repos/{repo}/commits/{head}/statuses"),
    }


def items(raw, endpoint):
    return [item for page in raw.get(endpoint, []) for item in page["items"]]


def terminal_review(
    raw,
    head,
    trigger_time,
    app_id=None,
    check_name=None,
    old_checks=(),
    *,
    completion_contract=None,
    repo=None,
    old_statuses=(),
):
    contract = completion_contract or {"kind": "check_run"}
    if contract.get("kind", "check_run") == "commit_status":
        return _terminal_commit_status(raw, head, trigger_time, contract, repo, old_statuses)
    if contract.get("kind", "check_run") != "check_run":
        raise EvalError("Unknown completion contract kind")
    app_id = contract.get("app_id", app_id)
    check_name = contract.get("check_name", check_name)
    if not app_id or not check_name:
        raise EvalError(
            "Completion contract requires observed CodeRabbit App ID and review check name"
        )
    trigger = datetime.fromisoformat(trigger_time.replace("Z", "+00:00"))

    def after(value):
        return bool(value) and datetime.fromisoformat(value.replace("Z", "+00:00")) >= trigger

    checks = [
        c
        for c in items(raw, "checks")
        if c.get("head_sha") == head
        and c.get("app", {}).get("id") == app_id
        and c.get("name") == check_name
        and c["id"] not in old_checks
        and c.get("status") == "completed"
        and after(c.get("started_at"))
        and after(c.get("completed_at"))
    ]
    reviews = [
        r
        for r in items(raw, "reviews")
        if r.get("commit_id") == head
        and r.get("user", {}).get("login") == "coderabbitai[bot]"
        and after(r.get("submitted_at"))
    ]
    if not checks:
        return None
    check = max(checks, key=lambda c: c["completed_at"])
    if check.get("conclusion") == "neutral":
        # A late auto-review-disabled check can overlap a manually triggered review.
        return None
    if check.get("conclusion") != "success":
        return {"status": "failed", "reason": f"CodeRabbit check: {check.get('conclusion')}"}
    if not reviews:
        return None
    review = max(reviews, key=lambda r: r["submitted_at"])
    # Do not infer success from acknowledgement, quota text or a green generic check.
    end = max(
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        for v in [review["submitted_at"], check["completed_at"]]
    )
    return {
        "status": "completed",
        "duration_seconds": (end - trigger).total_seconds(),
        "check_id": check["id"],
        "review_id": review["id"],
        "completed_at": end.isoformat(),
    }


def _terminal_commit_status(raw, head, trigger_time, contract, repo, old_statuses):
    required = ("creator_id", "creator_login", "context", "success_description")
    if (
        not repo
        or any(not contract.get(k) for k in required)
        or type(contract["creator_id"]) is not int
    ):
        raise EvalError(
            "Commit-status completion requires observed creator, context and success description"
        )
    trigger = datetime.fromisoformat(trigger_time.replace("Z", "+00:00"))
    endpoint = f"/repos/{repo}/commits/{head}/statuses"
    item_url = f"https://api.github.com/repos/{repo}/statuses/{head}"
    pages = raw.get("commit_statuses", [])
    if not pages or any(p.get("endpoint") != endpoint or p.get("error") for p in pages):
        return None

    def timestamp(value):
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError("Missing status timezone")
        return stamp

    statuses = []
    for status in items(raw, "commit_statuses"):
        if status.get("context") != contract["context"]:
            continue
        try:
            created, updated = timestamp(status["created_at"]), timestamp(status["updated_at"])
            if type(status["id"]) is not int or updated < created:
                return None
        except (KeyError, TypeError, ValueError, AttributeError):
            return None
        statuses.append((created, updated, status["id"], status))
    if not statuses:
        return None
    created, updated, _, status = max(statuses, key=lambda row: row[:3])
    creator = status.get("creator", {})
    # Select the newest context state before considering success. A newer pending
    # or untrusted status must never expose an older successful result.
    if (
        status.get("url") != item_url
        or status["id"] in old_statuses
        or creator.get("id") != contract["creator_id"]
        or creator.get("login") != contract["creator_login"]
        or created < trigger
        or updated < trigger
    ):
        return None
    if status.get("state") in {"failure", "error"}:
        return {"status": "failed", "reason": f"CodeRabbit commit status: {status['state']}"}
    if (
        status.get("state") != "success"
        or status.get("description") != contract["success_description"]
    ):
        return None
    reviews = []
    for review in items(raw, "reviews"):
        user = review.get("user", {})
        if (
            review.get("commit_id") != head
            or user.get("id") != contract["creator_id"]
            or user.get("login") != contract["creator_login"]
        ):
            continue
        try:
            submitted = timestamp(review["submitted_at"])
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        if submitted >= trigger:
            reviews.append((submitted, review))
    if reviews:
        submitted, review = max(reviews, key=lambda row: row[0])
        evidence = {"completion_evidence": "submitted_review", "review_id": review["id"]}
    else:
        walkthrough = _zero_findings_walkthrough(raw, head, trigger, contract, repo, timestamp)
        if walkthrough is None:
            return None
        submitted, comment = walkthrough
        evidence = {
            "completion_evidence": "zero_findings_walkthrough",
            "walkthrough_comment_id": comment["id"],
        }
    end = max(submitted, updated)
    return {
        "status": "completed",
        "completion_kind": "commit_status",
        "status_id": status["id"],
        **evidence,
        "duration_seconds": (end - trigger).total_seconds(),
        "completed_at": end.isoformat(),
    }


def _zero_findings_walkthrough(raw, head, trigger, contract, repo, timestamp):
    """Recognize the observed head-bound no-actionable review, never a generic reply."""
    app_id = contract.get("zero_findings_app_id")
    if type(app_id) is not int or not app_id:
        return None
    comments = []
    for page in raw.get("issue_comments", []):
        if not re.fullmatch(
            rf"/repos/{re.escape(repo)}/issues/[0-9]+/comments", page.get("endpoint", "")
        ):
            return None
        for comment in page.get("items", []):
            user = comment.get("user", {})
            if (
                user.get("id") != contract["creator_id"]
                or user.get("login") != contract["creator_login"]
                or (comment.get("performed_via_github_app") or {}).get("id") != app_id
            ):
                continue
            body = comment.get("body", "")
            if "<!-- This is an auto-generated comment: summarize by coderabbit.ai -->" not in body:
                continue
            try:
                created, updated = (
                    timestamp(comment["created_at"]),
                    timestamp(comment["updated_at"]),
                )
            except (KeyError, TypeError, ValueError, AttributeError):
                return None
            # CodeRabbit updates its existing walkthrough after a new review.
            if updated >= trigger and updated >= created:
                comments.append((updated, comment))
    if not comments:
        return None
    updated, comment = max(comments, key=lambda row: row[0])
    body = comment["body"]
    recent = re.findall(r"<!-- recent_review_start -->(.*?)<!-- recent_review_end -->", body, re.S)
    risk = re.findall(
        r"<!-- final_review_risk_start -->(.*?)<!-- final_review_risk_end -->", body, re.S
    )
    if len(recent) != 1 or len(risk) != 1:
        return None
    if not re.search(
        r"(?m)^No actionable comments were generated in the recent review\.(?:[ \t]*🎉)?[ \t]*$",
        recent[0],
    ):
        return None
    if not re.search(
        rf"Reviewing files that changed from the base of the PR and between [0-9a-f]{{40}} and {re.escape(head)}\.",
        recent[0],
    ):
        return None
    coverage = re.findall(r"<!-- final_review_risk_coverage:(\{[^\n]*\}) -->", risk[0])
    if len(coverage) != 1:
        return None
    try:
        covered = json.loads(coverage[0])
    except (ValueError, TypeError):
        return None
    if (
        not isinstance(covered, dict)
        or covered.get("sourceCommitId") != head
        or covered.get("coveredCommitId") != head
        or covered.get("kind") != "reviewed"
    ):
        return None
    if "<!-- walkthrough_start -->" not in body or "<!-- walkthrough_end -->" not in body:
        return None
    return updated, comment


def ensure_repo(api, owner, name, marker, save, attempt):
    repo = api.call("GET", f"/repos/{owner}/{name}", missing_ok=True)
    if repo is None:
        attempt["repository_intent"] = f"{owner}/{name}"
        save()
        user = api.call("GET", "/user")
        route = "/user/repos" if user["login"].lower() == owner.lower() else f"/orgs/{owner}/repos"
        repo = api.call(
            "POST",
            route,
            body={"name": name, "description": marker, "private": True, "auto_init": False},
        )
    if not repo.get("private") or repo.get("description") != marker:
        raise EvalError("Repository identity/visibility mismatch; refusing reuse")
    api.call("PUT", f"/repos/{owner}/{name}/actions/permissions", body={"enabled": False})
    # GitHub can briefly return the old permission after the successful write.
    for poll in range(10):
        permission = api.call("GET", f"/repos/{owner}/{name}/actions/permissions")
        if permission.get("enabled") is False:
            break
        if poll < 9:
            time.sleep(2)
    else:
        raise EvalError("Inherited GitHub Actions are not disabled")
    attempt["repository"] = f"{owner}/{name}"
    save()
    return repo


def _git(args, cwd, env=None):
    result = subprocess.run(
        ["git", "-c", "core.hooksPath=/dev/null", *args],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode:
        # Git stderr can contain auth URLs or credentials. Never echo it into evidence.
        raise EvalError(f"Git operation failed ({args[0]}), exit {result.returncode}")
    return result.stdout.strip()


def _archive_filter(member, destination):
    # Validate containment before preserving Git's exact symlink target bytes.
    accepted = tarfile.data_filter(member, destination)
    if accepted is not None and member.issym():
        accepted = accepted.replace(linkname=member.linkname)
    return accepted


def _stage_snapshot(tree):
    """Index raw bytes and modes, bypassing repository attributes and clean filters."""
    paths = []
    for directory, dirs, files in os.walk(tree, followlinks=False):
        dirs[:] = [d for d in dirs if d != ".git"]
        for name in list(dirs):
            if (Path(directory) / name).is_symlink():
                files.append(name)
                dirs.remove(name)
        paths.extend(Path(directory) / name for name in files)
    paths.sort()
    env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1")

    def binary_git(args, data):
        result = subprocess.run(
            ["git", "-c", "core.hooksPath=/dev/null", *args],
            cwd=tree,
            env=env,
            input=data,
            capture_output=True,
            timeout=300,
        )
        if result.returncode:
            raise EvalError("Raw snapshot Git indexing failed")
        return result.stdout

    regular = [p for p in paths if not p.is_symlink()]
    # Git's --stdin-paths accepts C-quoted names. ASCII JSON quoting agrees for
    # the control characters that would otherwise split a path across lines.
    names = b"".join(
        (json.dumps(str(p.relative_to(tree)), ensure_ascii=False) + "\n").encode() for p in regular
    )
    hashes = (
        binary_git(["hash-object", "-w", "--no-filters", "--stdin-paths"], names).splitlines()
        if regular
        else []
    )
    entries = []
    for p, sha in zip(regular, hashes, strict=True):
        mode = b"100755" if p.stat().st_mode & 0o111 else b"100644"
        entries.append(mode + b" " + sha + b"\t" + os.fsencode(p.relative_to(tree)) + b"\0")
    for p in paths:
        if p.is_symlink():
            sha = binary_git(
                ["hash-object", "-w", "--no-filters", "--stdin"], os.fsencode(os.readlink(p))
            ).strip()
            entries.append(b"120000 " + sha + b"\t" + os.fsencode(p.relative_to(tree)) + b"\0")
    binary_git(["read-tree", "--empty"], b"")
    binary_git(["update-index", "-z", "--index-info"], b"".join(entries))


def _snapshot(api, source_repo, sha, dest):
    if not source_repo or not sha or len(sha) != 40:
        raise EvalError("Snapshot requires a frozen source repository and full commit SHA")
    response = api.client.get(f"/repos/{source_repo}/tarball/{sha}", follow_redirects=True)
    response.raise_for_status()
    with tempfile.TemporaryDirectory() as folder:
        archive = Path(folder) / "source.tar.gz"
        archive.write_bytes(response.content)
        with tarfile.open(archive) as tar:
            members = tar.getmembers()
            if sum(m.size for m in members) > 2_000_000_000:
                raise EvalError("Snapshot archive exceeds size limit")
            prefixes = {m.name.split("/")[0] for m in members}
            if len(prefixes) != 1:
                raise EvalError("Unexpected archive roots")
            prefix = next(iter(prefixes))
            tar.extractall(Path(folder) / "unpacked", filter=_archive_filter)
        import shutil

        shutil.copytree(Path(folder) / "unpacked" / prefix, dest, dirs_exist_ok=True, symlinks=True)


def push_inputs(api, task, settings, repo, artifact_dir, attempt, save):
    data = task["input"]
    data = dict(data)
    data["source_repo"] = data.get("source_repo") or data.get("repo")
    authored = "base_files" in data and "head_files" in data
    for field in () if authored else ("source_repo", "base_sha", "head_sha"):
        if not data.get(field):
            raise EvalError(f"Historical snapshot is unresolved: missing {field}")
    headref = api.call("GET", f"/repos/{repo}/git/ref/heads/eval-head", missing_ok=True)
    baseref = api.call("GET", f"/repos/{repo}/git/ref/heads/eval-base", missing_ok=True)
    if headref and baseref:
        if (
            not attempt.get("expected_head")
            or not attempt.get("expected_base")
            or headref["object"]["sha"] != attempt["expected_head"]
            or baseref["object"]["sha"] != attempt["expected_base"]
        ):
            raise EvalError("Remote head cannot be reconciled against saved push intent")
        return attempt["expected_head"]
    if headref or baseref:
        raise EvalError("Partial remote push requires inspection; refusing blind replay")
    with tempfile.TemporaryDirectory() as folder:
        temp = Path(folder)
        tree = temp / "repo"
        tree.mkdir()
        _git(["init", "-b", "eval-base"], tree)
        _git(["config", "user.name", "Search Evaluation"], tree)
        _git(["config", "user.email", "evaluation@localhost"], tree)
        for branch, sha in [
            ("eval-base", data.get("base_sha")),
            ("eval-head", data.get("head_sha")),
        ]:
            if branch == "eval-head":
                _git(["checkout", "-b", branch], tree)
                # Isolated temporary reconstruction, not an existing user checkout.
                import shutil

                for child in tree.iterdir():
                    if child.name == ".git":
                        continue
                    if child.is_dir() and not child.is_symlink():
                        shutil.rmtree(child)
                    else:
                        child.unlink()
            if authored:
                files = data["base_files" if branch == "eval-base" else "head_files"]
                for name, content in files.items():
                    relative = Path(name)
                    if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
                        raise EvalError("Unsafe authored input path")
                    target = tree / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_text(content)
            else:
                _snapshot(api, data["source_repo"], sha, tree)
                _stage_snapshot(tree)
                source_tree = _git(["write-tree"], tree)
                expected_tree = data.get("base_tree" if branch == "eval-base" else "head_tree")
                if not expected_tree or source_tree != expected_tree:
                    raise EvalError("Reconstructed source tree differs from the pinned Git tree")
                atomic_json(
                    artifact_dir / f"{branch}-source.json",
                    {
                        "source_commit": sha,
                        "expected_tree": expected_tree,
                        "actual_tree": source_tree,
                        "verified": True,
                    },
                )
            if branch == "eval-head":
                for patch in data.get("head_overrides", []):
                    relative = Path(patch["path"])
                    if relative.is_absolute() or ".." in relative.parts or ".git" in relative.parts:
                        raise EvalError("Unsafe control patch path")
                    target = tree / relative
                    if target.is_symlink():
                        raise EvalError("Control patch cannot follow symlinks")
                    text = target.read_text()
                    if text.count(patch["old_text"]) != patch["expected_old_count"]:
                        raise EvalError("Control patch no longer matches the pinned snapshot")
                    target.write_text(text.replace(patch["old_text"], patch["new_text"]))
            config_file = tree / ".coderabbit.yaml"
            if config_file.is_symlink():
                config_file.unlink()
            (tree / ".coderabbit.yaml").write_text(yaml.safe_dump(settings, sort_keys=False))
            _stage_snapshot(tree)
            _git(["commit", "--allow-empty", "-m", "Evaluation input"], tree)
            if branch == "eval-base":
                attempt["expected_base"] = _git(["rev-parse", "HEAD"], tree)
        head = _git(["rev-parse", "HEAD"], tree)
        attempt["expected_head"] = head
        attempt["push_intent"] = True
        save()
        askpass = temp / "askpass"
        askpass.write_text(
            '#!/bin/sh\ncase "$1" in *Username*) echo x-access-token;; *) printf "%s" "$EVAL_GIT_TOKEN";; esac\n'
        )
        askpass.chmod(0o700)
        env = dict(
            os.environ, GIT_ASKPASS=str(askpass), GIT_TERMINAL_PROMPT="0", EVAL_GIT_TOKEN=api.token
        )
        _git(
            [
                "-c",
                "credential.helper=",
                "push",
                "--atomic",
                f"https://github.com/{repo}.git",
                "eval-base",
                "eval-head",
            ],
            tree,
            env=env,
        )
        return head


def review_events(root: Path, owner: str) -> list[float]:
    """Read our durable review ledger. Uncertain sends consume quota conservatively.

    Repository/PR identifies one attempt in this harness. Conflicting trigger IDs
    are not silently collapsed. Completed reviews age from completion, which is
    later than server admission. Unresolved sends retain an admission-delay risk.
    Reviews outside these saved runs are unobservable.
    """
    events = {}
    for path in sorted((root / "runs").glob("*/state.json")):
        try:
            state = json.loads(path.read_text())
            rows = state.get("attempts", [state])
            if not isinstance(rows, list):
                raise ValueError("attempts must be a list")
            for row in rows:
                if not isinstance(row, dict):
                    raise ValueError("attempt must be an object")
                if not any(row.get(k) for k in ("trigger_time", "trigger_intent", "trigger_id")):
                    continue
                repo = row.get("repository")
                if not isinstance(repo, str) or not re.fullmatch(r"[^/]+/[^/]+", repo):
                    raise ValueError("trigger lacks repository identity")
                if repo.split("/")[0].casefold() != owner.casefold():
                    continue
                pr = row.get("pr_number")
                if type(pr) is not int or pr < 1 or not row.get("id"):
                    raise ValueError("trigger lacks PR/attempt identity")
                actual = row.get("trigger_time")
                stamp = actual or row.get("trigger_intent")
                if not isinstance(stamp, str):
                    raise ValueError("trigger lacks timestamp")
                parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    raise ValueError("trigger timestamp lacks timezone")
                if row.get("status") == "completed" or "completed_at" in row:
                    completed = row.get("completed_at")
                    if not actual or not isinstance(completed, str):
                        raise ValueError("completed review lacks actual trigger/completion time")
                    completion = datetime.fromisoformat(completed.replace("Z", "+00:00"))
                    if completion.tzinfo is None or completion < parsed:
                        raise ValueError("completion timestamp lacks timezone or precedes trigger")
                    parsed = max(parsed, completion)
                event = events.setdefault(
                    (repo.casefold(), pr),
                    {"ids": set(), "attempt_ids": set(), "actual": [], "intent": []},
                )
                event["attempt_ids"].add(row["id"])
                if len(event["attempt_ids"]) > 1:
                    raise ValueError("multiple attempt identities for one PR")
                if row.get("trigger_id") is not None:
                    if type(row["trigger_id"]) is not int:
                        raise ValueError("invalid trigger ID")
                    event["ids"].add(row["trigger_id"])
                if len(event["ids"]) > 1:
                    raise ValueError("multiple triggers for one PR")
                event["actual" if actual else "intent"].append(parsed.timestamp())
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            raise EvalError(f"Cannot establish review quota from {path}: {exc}") from exc
    return [max(e["actual"] or e["intent"]) for e in events.values()]


def pace_review_trigger(config, attempt, artifact_dir: Path, *, nonblocking=False):
    """Wait for quota, or return seconds until the next check without reserving a trigger."""
    cap = getattr(config.limits, "max_review_events_per_hour", None)
    if cap is None:
        return 0
    runs = next((p for p in artifact_dir.resolve().parents if p.name == "runs"), None)
    if runs is None:
        raise EvalError("Review quota pacing requires an artifact directory under runs/")
    root = runs.parent
    state_path = runs / config.run_id / "state.json"
    try:
        state = json.loads(state_path.read_text())
        rows = state["attempts"]
        if not isinstance(rows, list) or not any(a.get("id") == attempt["id"] for a in rows):
            raise ValueError("current attempt missing")
    except (OSError, ValueError, KeyError, AttributeError) as exc:
        raise EvalError(f"Cannot establish current quota state from {state_path}") from exc
    last_release = None
    while True:
        verify_allowance(config, root, len(rows))
        current = time.time()
        try:
            expiry = datetime.fromisoformat(attempt["allowance_valid_until"].replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry.timestamp() <= current:
                raise ValueError("expired")
        except (KeyError, ValueError, AttributeError) as exc:
            raise EvalError("Allowance expired or invalid while waiting for review quota") from exc
        active = sorted(t for t in review_events(root, config.github_owner) if t + 3602 > current)
        if len(active) < cap:
            return 0
        # If a tighter cap follows a larger historical burst, enough events must expire.
        release = active[len(active) - cap] + 3602
        delay = min(30, release - current, expiry.timestamp() - current)
        if nonblocking:
            return delay
        if release != last_release:
            print(
                f"Review quota: {len(active)}/{cap} recent events; waiting until "
                f"{datetime.fromtimestamp(release).astimezone().isoformat()}",
                flush=True,
            )
            last_release = release
        time.sleep(delay)


def execute(config, task, arm, attempt, artifact_dir, save, register, *, nonblocking=False):
    api = GitHub(token_from_environment())
    if confirmed_trigger(attempt):
        return collect_review(api, config, attempt, artifact_dir, nonblocking=nonblocking)
    marker = f"search-eval:{attempt['id']}"
    name = f"se-{attempt['id']}"
    repo_data = ensure_repo(api, config.github_owner, name, marker, save, attempt)
    repo = repo_data["full_name"]
    settings = coderabbit_settings(arm, config.profiles, getattr(config, "review_profile", "chill"))
    atomic_json(artifact_dir / "effective-settings.json", settings)
    head = push_inputs(api, task, settings, repo, artifact_dir, attempt, save)
    prs = api.call(
        "GET",
        f"/repos/{repo}/pulls",
        params={"state": "all", "head": f"{config.github_owner}:eval-head"},
    )
    if len(prs) > 1:
        raise EvalError("Multiple PRs match attempt identity")
    if prs:
        pr = prs[0]
    else:
        attempt["pr_intent"] = True
        save()
        pr = api.call(
            "POST",
            f"/repos/{repo}/pulls",
            body={
                "head": "eval-head",
                "base": "eval-base",
                "title": task["input"].get("title", "Review the proposed change"),
                "body": task["input"].get("body", "Review this change for actionable defects."),
            },
        )
    if (
        pr["head"]["sha"] != head
        or pr["base"]["ref"] != "eval-base"
        or pr["base"].get("sha") != attempt.get("expected_base")
    ):
        raise EvalError("PR identity/head changed")
    attempt.update({"review_url": pr["html_url"], "head_sha": head, "pr_number": pr["number"]})
    save()
    comments = api.pages(f"/repos/{repo}/issues/{pr['number']}/comments")
    triggers = [
        c for p in comments for c in p["items"] if f"<!-- {marker} -->" in c.get("body", "")
    ]
    if len(triggers) > 1:
        raise EvalError("Duplicate review triggers detected")
    if not triggers:
        if attempt.get("trigger_intent"):
            raise EvalError(
                "Prior trigger response uncertain. Reconcile manually; never automatically resend"
            )
        before = api.pages(f"/repos/{repo}/commits/{head}/check-runs", key="check_runs")
        attempt["old_checks"] = [c["id"] for p in before for c in p["items"]]
        if config.suite_options.get("completion_contract", {}).get("kind") == "commit_status":
            before_statuses = api.pages(f"/repos/{repo}/commits/{head}/statuses")
            attempt["old_statuses"] = [c["id"] for p in before_statuses for c in p["items"]]
        if nonblocking:
            if pace_review_trigger(config, attempt, artifact_dir, nonblocking=True):
                return {"status": "pending", "reason": "Waiting for review quota"}
        else:
            pace_review_trigger(config, attempt, artifact_dir)
        attempt["trigger_intent"] = now()
        if not attempt.get("expires_at"):
            allowance_end = datetime.fromisoformat(attempt["allowance_valid_until"])
            start_time = datetime.fromisoformat(attempt["trigger_intent"])
            if allowance_end <= start_time:
                raise EvalError("Allowance expired during source preparation")
            attempt["expires_at"] = min(
                allowance_end, start_time + timedelta(seconds=config.limits.review_timeout_seconds)
            ).isoformat()
        save()
        register(attempt)
        trigger = api.call(
            "POST",
            f"/repos/{repo}/issues/{pr['number']}/comments",
            body={"body": f"@coderabbitai full review\n\n<!-- {marker} -->"},
        )
    else:
        trigger = triggers[0]
        if not attempt.get("expires_at"):
            raise EvalError("Existing trigger lacks saved deadline; reconcile before resuming")
        register(attempt)
    attempt.update({"trigger_id": trigger["id"], "trigger_time": trigger["created_at"]})
    save()
    return collect_review(api, config, attempt, artifact_dir, nonblocking=nonblocking)


def collect_review(api, config, attempt, artifact_dir, *, final_only=False, nonblocking=False):
    """Only GETs. Expired reviews receive one final server-timestamp reconciliation."""
    if not confirmed_trigger(attempt):
        raise EvalError("Read-only recovery requires a confirmed trigger and saved identity")
    repo, head, pr = attempt["repository"], attempt["head_sha"], attempt["pr_number"]
    deadline = datetime.fromisoformat(attempt["expires_at"]).timestamp()
    contract = config.suite_options.get("completion_contract", {})

    def read():
        fresh = api.call("GET", f"/repos/{repo}/pulls/{pr}")
        if (
            fresh["head"]["sha"] != head
            or fresh["base"].get("sha") != attempt["expected_base"]
            or fresh["base"].get("ref") != "eval-base"
        ):
            raise EvalError("Head/base drift invalidates this attempt")
        raw = collect(api, repo, pr, head)
        atomic_json(artifact_dir / "github.json", raw)
        result = terminal_review(
            raw,
            head,
            attempt["trigger_time"],
            contract.get("app_id"),
            contract.get("check_name"),
            attempt.get("old_checks", []),
            completion_contract=contract,
            repo=repo,
            old_statuses=attempt.get("old_statuses", []),
        )
        if result and result["status"] == "completed":
            if datetime.fromisoformat(result["completed_at"]).timestamp() > deadline:
                return {
                    "status": "timed_out",
                    "reason": "Server review completed after the original deadline",
                    "late_completion": result,
                }
        return result

    try:
        while True:
            expired = time.time() > deadline
            result = read()
            if (
                result is not None
                and result["status"] == "completed"
                and not (expired or final_only)
            ):
                # Archive a post-completion collection without extending the deadline.
                time.sleep(min(2, config.limits.poll_seconds))
                result = read()
                if result is None and not nonblocking:
                    continue
            if result is not None:
                return result
            if expired or time.time() > deadline:
                return {
                    "status": "timed_out",
                    "reason": "No verified terminal review within the original deadline",
                }
            if final_only or nonblocking:
                return {
                    "status": "read_pending",
                    "reason": "Confirmed review has no terminal evidence yet",
                }
            time.sleep(min(config.limits.poll_seconds, max(0.01, deadline - time.time())))
    except GitHubReadError as exc:
        log = artifact_dir / "github-read-errors.json"
        errors = json.loads(log.read_text()) if log.exists() else []
        errors.append(
            {
                "observed_at": now(),
                "trigger_id": attempt["trigger_id"],
                "head_sha": head,
                "error": str(exc),
                "recovery": "read_only",
            }
        )
        atomic_json(log, errors)
        raise
