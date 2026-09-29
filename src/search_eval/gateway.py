"""Authenticated, attempt-scoped Streamable HTTP MCP gateway."""

from __future__ import annotations

import contextlib
import contextvars
import fcntl
import fnmatch
import hashlib
import hmac
import json
import os
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.responses import JSONResponse

from search_eval.core import shared_runtime_identity
from search_eval.providers import Limits, ProviderError, public_url, read_page, search_provider

_context = contextvars.ContextVar("gateway_auth")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def secret_match(token: str, expected: str) -> bool:
    return bool(token and expected) and hmac.compare_digest(digest(token), expected)


def review_identity(url):
    """Canonical public GitHub PR identity, independent of view or casing."""
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or parsed.hostname.lower() != "github.com"
            or parsed.username
            or parsed.password
        ):
            return None
        match = re.fullmatch(
            r"/([^/]+)/([^/]+)/pull/(\d+)(?:/(?:files|commits|checks))?/?",
            parsed.path,
            re.IGNORECASE,
        )
        if match and int(match[3]) > 0:
            return f"https://github.com/{match[1].lower()}/{match[2].lower()}/pull/{int(match[3])}"
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def safe_url_evidence(value):
    try:
        parsed = urlsplit(value)
        if parsed.scheme in {"http", "https"} and parsed.hostname:
            return urlunsplit((parsed.scheme, parsed.hostname.lower(), parsed.path[:2048], "", ""))
    except (ValueError, TypeError, AttributeError):
        pass
    return None


def active(attempt):
    if attempt.get("status") != "running" or attempt.get("authorized") is not True:
        raise ProviderError("inactive_attempt")
    try:
        expiry = datetime.fromisoformat(attempt["expires_at"])
        if expiry.tzinfo is None or expiry <= datetime.now(UTC):
            raise ValueError()
    except (KeyError, TypeError, ValueError):
        raise ProviderError("attempt_expired_or_missing_expiry") from None


def sanitized(value):
    """Scrub known provider credentials even from echoed provider payloads."""
    secrets = [os.getenv(k, "") for k in ("EXA_API_KEY", "KEENABLE_API_KEY")]
    if isinstance(value, str):
        for secret in secrets:
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    if isinstance(value, list):
        return [sanitized(v) for v in value]
    if isinstance(value, dict):
        return {
            k: sanitized(v)
            for k, v in value.items()
            if k.lower() not in {"authorization", "x-api-key", "api_key", "token"}
        }
    return value


class Gateway:
    def __init__(self, routing=None, traces=None, search_fn=search_provider, reader_fn=read_page):
        self.runtime_identity = shared_runtime_identity()
        self.routing = Path(routing or os.getenv("SEARCH_EVAL_ROUTING", "/routing/attempts.json"))
        self.traces = Path(traces or os.getenv("SEARCH_EVAL_TRACE_DIR", "/traces"))
        self.search_fn, self.reader_fn = search_fn, reader_fn
        self.limits = Limits(
            timeout=float(os.getenv("SEARCH_EVAL_READER_TIMEOUT", "20")),
            max_bytes=int(os.getenv("SEARCH_EVAL_READER_MAX_BYTES", "1000000")),
            max_redirects=int(os.getenv("SEARCH_EVAL_READER_MAX_REDIRECTS", "3")),
            max_text_chars=int(os.getenv("SEARCH_EVAL_READER_MAX_TEXT_CHARS", "40000")),
        )
        if min(self.limits.timeout, self.limits.max_bytes, self.limits.max_text_chars) <= 0:
            raise ValueError("Reader limits must be positive")
        if not 0 <= self.limits.max_redirects <= 10:
            raise ValueError("Invalid redirect limit")

    def write_effective_config(self):
        self.traces.mkdir(parents=True, exist_ok=True)
        target = self.traces / "gateway-config.json"
        temporary = target.with_name(f".gateway-config-{uuid.uuid4().hex}.tmp")
        temporary.write_text(
            json.dumps(
                {
                    "version": 1,
                    "runtime_sha256": self.runtime_identity,
                    "reader": vars(self.limits),
                    "provider_transport": {"timeout_seconds": 20, "max_response_bytes": 2_000_000},
                },
                sort_keys=True,
                indent=2,
            )
            + "\n"
        )
        os.replace(temporary, target)

    def reject(self, error, endpoint, *, review_url=None, operation=None, auth=None, value=None):
        """Keep pre-call failures observable without storing tokens or query text."""
        aid, attempt = None, {}
        try:
            data = self.load()
            token = (auth or {}).get("token", "")
            if endpoint == "devdex":
                matches = [
                    (i, a)
                    for i, a in data["attempts"].items()
                    if secret_match(token, a.get("token_sha256", ""))
                ]
            elif any(
                p.get("endpoint", name) == endpoint
                and secret_match(token, p.get("connection_token_sha256", ""))
                for name, p in data["profiles"].items()
            ):
                identity = review_identity(review_url)
                matches = [
                    (i, a)
                    for i, a in data["attempts"].items()
                    if identity and review_identity(a.get("review_url")) == identity
                ]
            else:
                matches = []
            if len(matches) == 1:
                aid, attempt = matches[0]
        except ProviderError:
            pass
        event = {
            "event": "rejected",
            "gateway_runtime_sha256": self.runtime_identity,
            "event_id": uuid.uuid4().hex,
            "timestamp": time.time(),
            "endpoint": endpoint if endpoint in {"devdex", "search-a", "search-b"} else None,
            "attempt_id": aid,
            "configuration": attempt.get("configuration"),
            "operation": operation if operation in {"search", "fetch"} else None,
            "review_url": review_identity(review_url),
            "url": safe_url_evidence(value),
            "error": error,
        }
        self.traces.mkdir(parents=True, exist_ok=True)
        path = self.traces / (digest(aid) + ".rejections.jsonl" if aid else "rejections.jsonl")
        with path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(sanitized(event)) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def load(self):
        try:
            data = json.loads(self.routing.read_text())
            if not isinstance(data.get("profiles"), dict) or not isinstance(
                data.get("attempts"), dict
            ):
                raise TypeError()
            return data
        except (OSError, ValueError, TypeError, AttributeError):
            raise ProviderError("routing_unavailable") from None

    def authenticate(self, endpoint, token):
        data = self.load()
        if endpoint == "devdex":
            matches = [
                (i, a)
                for i, a in data["attempts"].items()
                if secret_match(token, a.get("token_sha256", ""))
            ]
            if len(matches) != 1:
                raise ProviderError("unauthorized")
            aid, a = matches[0]
            active(a)
            return {"endpoint": endpoint, "token": token, "attempt": aid}
        matches = [
            p
            for p, v in data["profiles"].items()
            if v.get("endpoint", p) == endpoint
            and secret_match(token, v.get("connection_token_sha256", ""))
        ]
        if len(matches) != 1:
            raise ProviderError("unauthorized")
        return {"endpoint": endpoint, "token": token, "profile": matches[0]}

    def resolve(self, auth, review_url):
        fresh = self.authenticate(auth["endpoint"], auth["token"])
        data = self.load()
        if fresh["endpoint"] == "devdex":
            aid = fresh["attempt"]
            attempt = data["attempts"][aid]
            profile = attempt.get("profile")
        else:
            matches = [
                (i, a)
                for i, a in data["attempts"].items()
                if review_identity(review_url)
                and review_identity(a.get("review_url")) == review_identity(review_url)
            ]
            if len(matches) != 1:
                raise ProviderError("unknown_review")
            aid, attempt = matches[0]
            profile = fresh["profile"]
        if (
            attempt.get("authorized") is not True
            or attempt.get("status") != "running"
            or profile not in attempt.get("allowed_profiles", [])
            or profile not in data["profiles"]
        ):
            raise ProviderError("disallowed_attempt")
        active(attempt)
        return aid, attempt, profile, data["profiles"][profile]

    def reserve(self, aid, attempt, profile, profile_data, operation):
        if attempt.get("expected_gateway_runtime_sha256") != self.runtime_identity:
            raise ProviderError("gateway_runtime_mismatch")
        active(attempt)
        self.traces.mkdir(parents=True, exist_ok=True)
        path = self.traces / (digest(aid) + ".jsonl")
        stable = {k: v for k, v in attempt.items() if k not in {"status", "authorized"}}
        attempt_fingerprint = digest(json.dumps(stable, sort_keys=True))
        fingerprint = digest(
            json.dumps(
                {"attempt": stable, "profile": profile_data, "reader": vars(self.limits)},
                sort_keys=True,
            )
        )
        limit = attempt.get(f"max_{operation}_calls")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 0:
            raise ProviderError("missing_call_limit")
        with path.open("a+", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.seek(0)
            try:
                rows = [json.loads(line) for line in stream]
            except ValueError:
                raise ProviderError("corrupt_reservation_log") from None
            reservations = [r for r in rows if r.get("event") == "reserved"]
            if any(r.get("attempt_fingerprint") != attempt_fingerprint for r in reservations):
                raise ProviderError("changed_attempt_mapping")
            if any(
                r["profile"] == profile and r["fingerprint"] != fingerprint for r in reservations
            ):
                raise ProviderError("changed_attempt_mapping")
            if sum(r["operation"] == operation for r in reservations) >= limit:
                raise ProviderError("call_cap_exhausted")
            active(attempt)
            call = {
                "event": "reserved",
                "call_id": uuid.uuid4().hex,
                "attempt_id": aid,
                "profile": profile,
                "configuration": attempt.get("configuration"),
                "operation": operation,
                "fingerprint": fingerprint,
                "attempt_fingerprint": attempt_fingerprint,
                "reader": vars(self.limits),
                "gateway_runtime_sha256": self.runtime_identity,
                "timestamp": time.time(),
            }
            stream.write(json.dumps(call) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        return path, call

    def finish(self, path, call, outcome):
        with path.open("a", encoding="utf-8") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            stream.write(json.dumps(sanitized({**call, **outcome, "event": "finished"})) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    async def execute(self, operation, value, review_url=None, auth=None):
        auth = auth or _context.get()
        try:
            if not isinstance(value, str) or not value.strip() or len(value) > 8192:
                raise ProviderError("invalid_tool_input")
            aid, attempt, profile, pdata = self.resolve(auth, review_url)
            if operation == "fetch":
                value = public_url(value)
            path, call = self.reserve(aid, attempt, profile, pdata, operation)
        except ProviderError as exc:
            self.reject(
                str(exc),
                auth.get("endpoint"),
                review_url=review_url,
                operation=operation,
                auth=auth,
                value=value if operation == "fetch" else None,
            )
            raise
        patterns = attempt.get("answer_patterns", [])

        def blocked(url):
            return any(fnmatch.fnmatchcase(url.lower(), p.lower()) for p in patterns)

        started = time.monotonic()
        evidence = {"input": value}
        try:
            if operation == "search":
                evidence.update(await self.search_fn(value, pdata))
                evidence["blocked_urls"] = [
                    r["url"] for r in evidence["results"] if blocked(r["url"])
                ]
                output = {"results": [r for r in evidence["results"] if not blocked(r["url"])]}
            elif operation == "fetch":
                if blocked(value):
                    raise ProviderError("answer_page_blocked")
                output = await self.reader_fn(value, self.limits, blocked=blocked)
            else:
                raise ProviderError("unknown_operation")
            output = sanitized(output)
            self.finish(
                path,
                call,
                {
                    **evidence,
                    "output": output,
                    "duration_seconds": time.monotonic() - started,
                    "ok": True,
                },
            )
            return output
        except Exception as exc:  # noqa: BLE001 - boundary must redact arbitrary SDK exceptions
            code = str(exc) if isinstance(exc, ProviderError) else "gateway_operation_failed"
            self.finish(
                path,
                call,
                {
                    "input": value,
                    "error": code,
                    "ok": False,
                    "duration_seconds": time.monotonic() - started,
                },
            )
            raise ProviderError(code) from None


def create_app(gateway=None):
    gateway = gateway or Gateway()
    servers, apps = {}, {}
    for route in ("search-a", "search-b", "devdex"):
        server = FastMCP(
            "Search tools",
            stateless_http=True,
            json_response=True,
            streamable_http_path=f"/mcp/{route}",
            transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
        )
        if route == "devdex":

            async def search(query: str) -> dict:
                """Search the web for relevant sources."""
                return await gateway.execute("search", query)

            async def fetch(url: str) -> dict:
                """Read a public source page."""
                return await gateway.execute("fetch", url)
        else:

            async def search(query: str, review_url: str) -> dict:
                """Search the web for sources. Supply the current pull request URL."""
                return await gateway.execute("search", query, review_url)

            async def fetch(url: str, review_url: str) -> dict:
                """Read a public source page. Supply the current pull request URL."""
                return await gateway.execute("fetch", url, review_url)

        server.tool()(search)
        server.tool()(fetch)
        servers[route], apps[route] = server, server.streamable_http_app()

    class Application:
        async def __call__(self, scope, receive, send):
            if scope["type"] == "lifespan":
                async with contextlib.AsyncExitStack() as stack:
                    for server in servers.values():
                        await stack.enter_async_context(server.session_manager.run())
                    while True:
                        event = await receive()
                        if event["type"] == "lifespan.startup":
                            gateway.write_effective_config()
                            await send({"type": "lifespan.startup.complete"})
                        elif event["type"] == "lifespan.shutdown":
                            await send({"type": "lifespan.shutdown.complete"})
                            return
            elif scope["type"] == "http":
                health = scope["path"].rstrip("/").endswith("/health")
                endpoint_path = (
                    scope["path"].rstrip("/").removesuffix("/health") if health else scope["path"]
                )
                route = endpoint_path.rstrip("/").removeprefix("/mcp/")
                if route not in apps:
                    return await JSONResponse({"error": "not_found"}, 404)(scope, receive, send)
                headers = dict(scope["headers"])
                authorization = headers.get(b"authorization", b"").decode(errors="replace")
                token = authorization[7:] if authorization.startswith("Bearer ") else ""
                try:
                    auth = gateway.authenticate(route, token)
                except ProviderError as exc:
                    gateway.reject(str(exc), route, auth={"endpoint": route, "token": token})
                    return await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
                if health:
                    if scope["method"] != "GET" or route == "devdex":
                        return await JSONResponse({"error": "not_found"}, 404)(scope, receive, send)
                    return await JSONResponse(
                        {"runtime_sha256": gateway.runtime_identity, "reader": vars(gateway.limits)}
                    )(scope, receive, send)
                ctx = _context.set(auth)
                try:
                    scope = {**scope, "path": f"/mcp/{route}"}
                    await apps[route](scope, receive, send)
                finally:
                    _context.reset(ctx)

    return Application()


def main():
    import uvicorn

    uvicorn.run(create_app(), host="0.0.0.0", port=int(os.getenv("PORT", "8000")), access_log=False)


if __name__ == "__main__":
    main()
