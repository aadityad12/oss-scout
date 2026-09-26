"""Tiny read-only GitHub REST client.

Transport is `gh api` when the gh CLI is available (works locally with your
keyring login, and in Claude cloud sessions through the GitHub proxy), with a
urllib fallback that reads GH_TOKEN / GITHUB_TOKEN.

This client only ever issues GET requests. Writing to GitHub is not something
the scout does; the hooks in .claude/ enforce that for the Claude step too.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Callable, Iterator

API = "https://api.github.com"


class NotFound(Exception):
    pass


class RateLimited(Exception):
    pass


Transport = Callable[[str], Any]  # path-with-query -> parsed JSON (raises NotFound)


def gh_cli_transport(path: str) -> Any:
    proc = subprocess.run(
        ["gh", "api", "-X", "GET", "-H", "Accept: application/vnd.github+json", path],
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        err = proc.stderr + proc.stdout
        if "HTTP 404" in err or "Not Found" in err:
            raise NotFound(path)
        if "rate limit" in err.lower() or "HTTP 429" in err:
            raise RateLimited(err.strip())
        raise RuntimeError(f"gh api {path} failed: {err.strip()[:300]}")
    return json.loads(proc.stdout) if proc.stdout.strip() else None


def urllib_transport(path: str) -> Any:
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    req = urllib.request.Request(f"{API}/{path}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "oss-scout")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise NotFound(path) from e
        if e.code in (403, 429) and e.headers.get("x-ratelimit-remaining") == "0":
            raise RateLimited(path) from e
        raise


def default_transport() -> Transport:
    return gh_cli_transport if shutil.which("gh") else urllib_transport


class GitHub:
    def __init__(self, cache_dir: Path | None = None, transport: Transport | None = None,
                 search_interval: float = 6.0):
        self.cache_dir = cache_dir
        self.transport = transport or default_transport()
        self.search_interval = search_interval  # search API: 30 req/min, plus secondary limits on bursts
        self._last_search = 0.0
        self.search_blocked = False
        self.calls = 0

    # -- core ---------------------------------------------------------------

    def get(self, path: str, params: dict | None = None, ttl: float = 0) -> Any:
        if params:
            path = f"{path}?{urllib.parse.urlencode(params)}"
        cached = self._cache_read(path, ttl)
        if cached is not None:
            return cached
        is_search = path.startswith("search/")
        if is_search and self.search_blocked:
            raise RateLimited("search blocked earlier in this run")
        if is_search:
            wait = self.search_interval - (time.monotonic() - self._last_search)
            if wait > 0:
                time.sleep(wait)
            self._last_search = time.monotonic()
        attempts = 2 if is_search else 4
        for attempt in range(attempts):
            try:
                self.calls += 1
                data = self.transport(path)
                break
            except RateLimited as e:
                if attempt == attempts - 1:
                    self.search_blocked |= is_search
                    raise
                print(f"[scout] rate limited on {path[:80]}, backing off: {str(e)[:120]}",
                      file=sys.stderr, flush=True)
                time.sleep(60 * 2 ** attempt)  # 1, 2, 4 minutes
        self._cache_write(path, data, ttl)
        return data

    def paginate(self, path: str, params: dict | None = None, max_items: int = 100,
                 ttl: float = 0) -> Iterator[dict]:
        params = dict(params or {})
        params.setdefault("per_page", min(100, max_items))
        page, seen = 1, 0
        while seen < max_items:
            params["page"] = page
            batch = self.get(path, params, ttl=ttl)
            if isinstance(batch, dict) and "items" in batch:
                batch = batch["items"]
            if not batch:
                return
            for item in batch:
                yield item
                seen += 1
                if seen >= max_items:
                    return
            if len(batch) < params["per_page"]:
                return
            page += 1

    # -- helpers ------------------------------------------------------------

    def repo(self, full_name: str, ttl: float = 86400) -> dict | None:
        try:
            return self.get(f"repos/{full_name}", ttl=ttl)
        except NotFound:
            return None

    def file_text(self, full_name: str, path: str, ttl: float = 7 * 86400) -> str | None:
        try:
            data = self.get(f"repos/{full_name}/contents/{path}", ttl=ttl)
        except NotFound:
            return None
        if not isinstance(data, dict) or data.get("encoding") != "base64":
            return None
        return base64.b64decode(data["content"]).decode("utf-8", errors="replace")

    def search_issues(self, query: str, max_items: int = 50, sort: str | None = None,
                      ttl: float = 0) -> list[dict]:
        params = {"q": query}
        if sort:
            params["sort"] = sort
            params["order"] = "desc"
        return list(self.paginate("search/issues", params, max_items=max_items, ttl=ttl))

    # -- cache --------------------------------------------------------------

    def _cache_path(self, path: str) -> Path | None:
        if not self.cache_dir:
            return None
        return self.cache_dir / (hashlib.sha1(path.encode()).hexdigest() + ".json")

    def _cache_read(self, path: str, ttl: float) -> Any:
        p = self._cache_path(path)
        if not ttl or not p or not p.exists():
            return None
        if time.time() - p.stat().st_mtime > ttl:
            return None
        return json.loads(p.read_text())

    def _cache_write(self, path: str, data: Any, ttl: float) -> None:
        p = self._cache_path(path)
        if not ttl or not p:
            return
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data))
