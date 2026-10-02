"""Tiered fetching with a polite, cached, browser-coherent HTTP client.

Tier 1 is curl_cffi impersonating Chrome: the User-Agent, TLS handshake and HTTP/2
settings all match a real browser. That matters because several IR platforms sit
behind Akamai, which resets connections whose User-Agent and TLS fingerprint disagree
(plain curl/requests with a Chrome User-Agent fails; this passes).

Tier 2 is a headless Chromium (Playwright, optional) for pages that only render
their lists with JavaScript.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from curl_cffi import requests as cffi


class FetchError(Exception):
    pass


@dataclass
class Response:
    url: str
    final_url: str
    status: int
    headers: dict[str, str]
    content: bytes
    from_cache: bool = False
    tier: str = "http"
    _text: str | None = field(default=None, repr=False)

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    @property
    def text(self) -> str:
        if self._text is None:
            m = re.search(r"charset=([\w-]+)", self.headers.get("content-type", ""), re.I)
            enc = m.group(1) if m else "utf-8"
            try:
                self._text = self.content.decode(enc, errors="replace")
            except LookupError:
                self._text = self.content.decode("utf-8", errors="replace")
        return self._text


class Fetcher:
    def __init__(
        self,
        cache_dir: Path,
        offline: bool = False,
        max_age_hours: float = 12.0,
        min_interval: float = 1.0,
        timeout: float = 40.0,
        respect_robots: bool = True,
    ):
        self.cache_dir = cache_dir
        self.offline = offline
        self.max_age_s = max_age_hours * 3600
        self.min_interval = min_interval
        self.timeout = timeout
        self.respect_robots = respect_robots
        self.session = cffi.Session(impersonate="chrome")
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self.requests_made = 0
        self.cache_hits = 0

    # -- public -----------------------------------------------------------------

    def get(
        self,
        url: str,
        *,
        byte_range: tuple[int, int] | None = None,
        render: bool = False,
        headers: dict[str, str] | None = None,
        api: bool = False,
    ) -> Response:
        """`api=True` marks a documented machine endpoint (SEC EDGAR JSON, the Wikidata
        API): it is called as an API under its own access policy, so the crawler rules in
        robots.txt, which every web page fetch obeys, are not consulted."""
        key = self._key(url, byte_range, render)
        cached = self._load(url, key)
        if cached is not None:
            self.cache_hits += 1
            return cached
        if self.offline:
            raise FetchError(f"offline and not in cache: {url}")
        if self.respect_robots and not api and not self._allowed(url):
            raise FetchError(f"disallowed by robots.txt: {url}")

        resp = self._render(url) if render else self._http(url, byte_range, headers)
        # Only complete answers are cached, so a throttled or blocked reply is retried.
        if resp.status in (200, 206):
            self._store(url, key, resp)
        return resp

    # -- tiers ------------------------------------------------------------------

    def _http(
        self, url: str, byte_range: tuple[int, int] | None, headers: dict[str, str] | None
    ) -> Response:
        hdrs = dict(headers or {})
        if byte_range:
            hdrs["Range"] = f"bytes={byte_range[0]}-{byte_range[1]}"
        last_err: Exception | None = None
        for attempt in range(3):
            self._throttle(url)
            try:
                r = self.session.get(
                    url, headers=hdrs, timeout=self.timeout, allow_redirects=True
                )
                self.requests_made += 1
                if r.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                    time.sleep(2.0 * (attempt + 1))
                    continue
                return Response(
                    url=url,
                    final_url=str(r.url),
                    status=r.status_code,
                    headers={k.lower(): v for k, v in r.headers.items()},
                    content=r.content,
                )
            except Exception as e:  # curl errors: resets, timeouts, DNS
                last_err = e
                time.sleep(1.5 * (attempt + 1))
        raise FetchError(f"{type(last_err).__name__}: {str(last_err)[:160]}")

    def _render(self, url: str) -> Response:
        try:
            from playwright.sync_api import sync_playwright
        except ImportError as e:
            raise FetchError("playwright not installed (uv sync --extra browser)") from e
        self._throttle(url)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page()
                    r = page.goto(url, wait_until="domcontentloaded", timeout=45_000)
                    try:
                        # Pages with analytics beacons never go fully idle: wait a
                        # bounded time for the list to render, then take what is there.
                        page.wait_for_load_state("networkidle", timeout=10_000)
                    except Exception:
                        pass
                    html = page.content()
                    status = r.status if r else 0
                    final = page.url
                finally:
                    browser.close()
        except Exception as e:
            raise FetchError(f"render failed: {type(e).__name__}: {str(e)[:160]}") from e
        self.requests_made += 1
        return Response(
            url=url,
            final_url=final,
            status=status,
            headers={"content-type": "text/html; charset=utf-8"},
            content=html.encode("utf-8"),
            tier="browser",
        )

    # -- politeness -------------------------------------------------------------

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        wait = self._last_hit.get(host, 0) + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_hit[host] = time.monotonic()

    def _allowed(self, url: str) -> bool:
        parts = urlsplit(url)
        host = parts.netloc
        if host not in self._robots:
            rp: RobotFileParser | None = RobotFileParser()
            try:
                r = self._http(f"{parts.scheme}://{host}/robots.txt", None, None)
                if r.ok and "text" in r.headers.get("content-type", "text"):
                    rp.parse(r.text.splitlines())
                else:
                    rp = None  # no robots.txt: nothing is disallowed
            except FetchError:
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch("*", url)

    # -- cache ------------------------------------------------------------------

    @staticmethod
    def _key(url: str, byte_range: tuple[int, int] | None, render: bool) -> str:
        raw = f"{url}|{byte_range}|{'render' if render else 'http'}"
        return hashlib.sha1(raw.encode()).hexdigest()[:20]

    def _paths(self, url: str, key: str) -> tuple[Path, Path]:
        host = re.sub(r"[^\w.-]", "_", urlsplit(url).netloc)
        d = self.cache_dir / host
        return d / f"{key}.json", d / f"{key}.bin"

    def _load(self, url: str, key: str) -> Response | None:
        meta_p, body_p = self._paths(url, key)
        if not (meta_p.exists() and body_p.exists()):
            return None
        meta = json.loads(meta_p.read_text(encoding="utf-8"))
        if not self.offline and time.time() - meta["fetched_at"] > self.max_age_s:
            return None
        return Response(
            url=url,
            final_url=meta["final_url"],
            status=meta["status"],
            headers=meta["headers"],
            content=body_p.read_bytes(),
            from_cache=True,
            tier=meta.get("tier", "http"),
        )

    def _store(self, url: str, key: str, resp: Response) -> None:
        meta_p, body_p = self._paths(url, key)
        meta_p.parent.mkdir(parents=True, exist_ok=True)
        body_p.write_bytes(resp.content)
        meta_p.write_text(
            json.dumps(
                {
                    "url": url,
                    "final_url": resp.final_url,
                    "status": resp.status,
                    "headers": resp.headers,
                    "tier": resp.tier,
                    "fetched_at": time.time(),
                }
            ),
            encoding="utf-8",
        )
