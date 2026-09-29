"""Bounded search adapters and a DNS-pinned public-page reader."""

from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
import httpx


class ProviderError(Exception):
    """Safe, deliberately content-free operational error."""


def public_url(url: str) -> str:
    try:
        p = urlsplit(url)
        if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
            raise ValueError()
        if p.port not in {None, 80, 443}:
            raise ValueError()
        host = p.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
            raise ValueError()
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address is not None and not address.is_global:
            raise ValueError()
        return urlunsplit((p.scheme, p.netloc.lower(), p.path or "/", p.query, ""))
    except (ValueError, TypeError):
        raise ProviderError("unsafe_url") from None


class PublicResolver(aiohttp.abc.AbstractResolver):
    """Return checked numeric addresses directly to the connector, avoiding DNS re-resolution."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family, type=socket.SOCK_STREAM
        )
        result = []
        for fam, _, proto, _, address in infos:
            ip = address[0]
            if not ipaddress.ip_address(ip).is_global:
                raise ProviderError("unsafe_dns")
            result.append(
                {
                    "hostname": host,
                    "host": ip,
                    "port": port,
                    "family": fam,
                    "proto": proto,
                    "flags": socket.AI_NUMERICHOST,
                }
            )
        if not result:
            raise ProviderError("empty_dns")
        return result

    async def close(self):
        pass


class TextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1
        if tag in {"p", "div", "br", "li", "h1", "h2", "tr"}:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


@dataclass(frozen=True)
class Limits:
    timeout: float = 20
    max_bytes: int = 1_000_000
    max_redirects: int = 3
    max_text_chars: int = 40_000


async def read_page(url: str, limits: Limits, blocked=None) -> dict:
    """Fetch no scripts or embedded resources; every redirect uses the checked resolver."""
    current = public_url(url)
    connector = aiohttp.TCPConnector(resolver=PublicResolver(), use_dns_cache=False)
    try:
        async with asyncio.timeout(limits.timeout):
            async with aiohttp.ClientSession(connector=connector, trust_env=False) as session:
                for hop in range(limits.max_redirects + 1):
                    if blocked and blocked(current):
                        raise ProviderError("answer_page_blocked")
                    async with session.get(current, allow_redirects=False) as response:
                        if response.status in {301, 302, 303, 307, 308}:
                            if hop == limits.max_redirects or not response.headers.get("Location"):
                                raise ProviderError("redirect_limit")
                            current = public_url(urljoin(current, response.headers["Location"]))
                            continue
                        if response.status != 200:
                            raise ProviderError(f"reader_http_{response.status}")
                        mime = response.headers.get("Content-Type", "").split(";")[0].lower()
                        if mime not in {
                            "text/html",
                            "text/plain",
                            "text/markdown",
                            "text/x-markdown",
                        }:
                            raise ProviderError("unsupported_content_type")
                        body = bytearray()
                        async for chunk in response.content.iter_chunked(16384):
                            body.extend(chunk)
                            if len(body) > limits.max_bytes:
                                raise ProviderError("reader_byte_limit")
                        text = body.decode("utf-8", errors="replace")
                        if mime == "text/html":
                            parser = TextParser()
                            parser.feed(text)
                            text = "".join(parser.parts)
                        return {
                            "url": current,
                            "content": text[: limits.max_text_chars],
                            "truncated": len(text) > limits.max_text_chars,
                            "content_type": mime,
                        }
    except ProviderError:
        raise
    except (aiohttp.ClientError, TimeoutError, OSError, ValueError):
        raise ProviderError("reader_request_failed") from None
    raise ProviderError("redirect_limit")


def normalize(raw: dict, provider: str) -> tuple[list, list]:
    if not isinstance(raw, dict) or not isinstance(raw.get("results"), list):
        raise ProviderError("invalid_provider_response")
    results, fields, seen = [], [], set()
    for item in raw["results"]:
        if not isinstance(item, dict):
            continue
        try:
            url = public_url(item.get("url"))
        except ProviderError:
            continue
        if url in seen:
            continue
        seen.add(url)
        field = "snippet" if provider == "keenable" else "highlights"
        value = item.get(field, "")
        if isinstance(value, list):
            value = "\n".join(x for x in value if isinstance(x, str))
        if not isinstance(value, str):
            value = ""
        results.append(
            {"title": str(item.get("title") or "")[:1000], "url": url, "excerpt": value[:1000]}
        )
        fields.append(field)
        if len(results) == 10:
            break
    return results, fields


def request_spec(profile: dict) -> dict:
    """One non-secret request contract shared by execution and frozen inputs."""
    provider, mode, tier = profile["provider"], profile.get("mode"), profile.get("tier")
    if tier not in {"public", "authenticated"}:
        raise ProviderError("explicit_provider_tier_required")
    if provider == "keenable":
        if mode not in {"pro", "realtime"}:
            raise ProviderError("unsupported_search_mode")
        endpoint = "https://api.keenable.ai/v1/search" + ("/public" if tier == "public" else "")
        params = {"mode": mode, "max_results": 10, "snippet_max_length": 1000}
    elif provider == "exa":
        if tier != "authenticated":
            raise ProviderError("unsupported_provider_tier")
        if mode not in {"auto", "fast", "instant", "neural"}:
            raise ProviderError("unsupported_search_mode")
        endpoint = "https://api.exa.ai/search"
        params = {"type": mode, "numResults": 10, "contents": {"highlights": True, "text": False}}
    else:
        raise ProviderError("unknown_provider")
    return {
        "endpoint": endpoint,
        "request_parameters_without_query": params,
        "timeout_seconds": 20,
        "max_response_bytes": 2_000_000,
        "automatic_retries": 0,
    }


async def search_provider(query: str, profile: dict, client=None) -> dict:
    spec = request_spec(profile)
    provider = profile["provider"]
    mode, tier = profile["mode"], profile["tier"]
    key = os.getenv("KEENABLE_API_KEY" if provider == "keenable" else "EXA_API_KEY", "")
    if tier == "authenticated" and not key:
        raise ProviderError("missing_provider_credential")
    headers = (
        {"X-Keenable-Title": os.getenv("SEARCH_EVAL_APP_TITLE", "keenable-search-eval")}
        if tier == "public"
        else {"X-API-Key" if provider == "keenable" else "x-api-key": key}
    )
    url = spec["endpoint"]
    payload = {"query": query, **spec["request_parameters_without_query"]}
    own = client is None
    client = client or httpx.AsyncClient(
        timeout=spec["timeout_seconds"], follow_redirects=False, trust_env=False
    )
    try:
        async with asyncio.timeout(spec["timeout_seconds"]):
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                if response.status_code != 200:
                    raise ProviderError(f"provider_http_{response.status_code}")
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > spec["max_response_bytes"]:
                        raise ProviderError("provider_byte_limit")
                import json

                raw = json.loads(data)
        results, fields = normalize(raw, provider)
        return {
            "results": results,
            "raw": raw,
            "parameters": payload,
            "excerpt_fields": fields,
            "provider": provider,
            "endpoint": url,
            "tier": tier,
            "mode": mode,
        }
    except ProviderError:
        raise
    except (httpx.HTTPError, ValueError, TypeError, TimeoutError):
        raise ProviderError("provider_request_failed") from None
    finally:
        if own:
            await client.aclose()
