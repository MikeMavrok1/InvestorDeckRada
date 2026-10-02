"""From an IR home page, find the pages that are likely to list presentations.

No fixed URL patterns: links are scored by the words in their path and anchor text,
the best few are visited, and recent event-detail pages are followed because some
sites attach the deck only there.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from .dates import find_dates
from .extract import is_doc_url, is_event_page, norm
from .fetch import Fetcher, FetchError, Response
from .trace import Trace

_POSITIVE = [
    (re.compile(r"presentation"), 5),
    (re.compile(r"\bevents?\b|events-"), 3),
    (re.compile(r"webcast"), 3),
    (re.compile(r"(?:investor|analyst|r&d|rd|science)[-\s]days?"), 3),
    (re.compile(r"quarterly[-\s](?:results|earnings|reports)"), 3),
    (re.compile(r"financial[-\s]results|earnings"), 2),
]
_NEGATIVE = [
    (re.compile(r"sec[-\s]filings?|governance|committee|leadership|board"), -6),
    (re.compile(r"press[-\s]releases?|news[-\s]releases?|/news/|news-details"), -5),
    (re.compile(r"stock|dividend|analyst[-\s]coverage|faq|contact|alerts?|\brss\b|privacy|"
                r"terms|careers|login|subscribe|e-?mail|request"), -6),
    (re.compile(r"annual[-\s]reports?|proxy|esg|sustainab"), -3),
    (re.compile(r"scientific|publication|poster|medical[-\s]meeting"), -2),
]
_LOCALE = re.compile(r"^/(?:de|fr|es|it|ja|jp|zh|cn|ko|pt|nl)(?:/|$)", re.I)
_SKIP_EXT = re.compile(r"\.(?:ics|vcs|zip|jpe?g|png|gif|svg|xml|rss|mp[34])(?:$|\?)", re.I)


@dataclass
class EventRef:
    url: str
    title: str
    when: date | None


def _host(url: str) -> str:
    return urlsplit(url).netloc.lower().removeprefix("www.")


def _canon(url: str) -> str:
    url = urldefrag(url)[0]
    p = urlsplit(url)
    path = re.sub(r"/(?:default\.aspx|index\.html?)$", "", p.path).rstrip("/")
    query = f"?{p.query}" if p.query and not re.fullmatch(r"page=0", p.query) else ""
    return f"{p.scheme}://{_host(url)}{path}{query}"


def score_link(url: str, text: str) -> float:
    path = urlsplit(url).path.lower()
    blob = f"{path} {text.lower()}"
    score = sum(pts for rx, pts in _POSITIVE if rx.search(blob))
    if score <= 0:
        return 0.0
    if path.rstrip("/").rsplit("/", 1)[-1].count("-") >= 4:
        score -= 4  # a long slug is an article, not a listing page
    return score + sum(pts for rx, pts in _NEGATIVE if rx.search(blob))


def _usable(url: str, root: str) -> bool:
    p = urlsplit(url)
    return (
        p.scheme in ("http", "https")
        and _host(url) == _host(root)
        and not _SKIP_EXT.search(p.path)
        and not (_LOCALE.match(p.path) and not _LOCALE.match(urlsplit(root).path))
        and not is_doc_url(url)
        and not (p.query and re.search(r"page=[1-9]", p.query))  # newest items are on page 0
    )


Scored = dict[str, tuple[float, str]]  # canonical url -> (score, url to fetch)


def harvest(soup: BeautifulSoup, page_url: str, root: str) -> tuple[Scored, dict[str, EventRef]]:
    """Listing-page links with scores, and event-detail links with their dates."""
    listings: Scored = {}
    events: dict[str, EventRef] = {}
    for a in soup.find_all("a", href=True):
        url = urldefrag(urljoin(page_url, a["href"].strip()))[0]
        if not _usable(url, root):
            continue
        key = _canon(url)
        text = norm(a.get_text(" ", strip=True))
        if is_event_page(url):
            if key not in events:
                events[key] = EventRef(url, text, _event_date(a))
            elif not events[key].title and text:
                events[key].title = text
            continue
        s = score_link(url, text)
        if s > listings.get(key, (0, ""))[0]:
            listings[key] = (s, url)
    return listings, events


def _event_date(a: Tag) -> date | None:
    """Date printed in the same row as an event link."""
    row = a
    for _ in range(6):
        p = row.parent
        if not isinstance(p, Tag) or p.name in ("body", "html"):
            break
        if len(norm(p.get_text(" ", strip=True))) > 400:
            break
        row = p
    days = [h for h in find_dates(norm(row.get_text(" ", strip=True))) if h.precision == "day"]
    return days[0].value if days else None


def _sitemap(fetcher: Fetcher, root: str) -> Scored:
    p = urlsplit(root)
    try:
        r = fetcher.get(f"{p.scheme}://{p.netloc}/sitemap.xml")
    except FetchError:
        return {}
    if not r.ok or "<loc>" not in r.text:
        return {}
    out: Scored = {}
    for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", r.text):
        if _usable(loc, root) and not is_event_page(loc):
            s = score_link(loc, "")
            if s > 0:
                out[_canon(loc)] = (s, loc)
    return out


def discover(
    fetcher: Fetcher,
    root: str,
    trace: Trace,
    today: date,
    max_listing: int = 8,
    max_events: int = 10,
    min_score: float = 3.0,
) -> list[tuple[str, Response]]:
    """Fetch the home page, the best listing pages and the most recent event pages."""
    pages: list[tuple[str, Response]] = []
    try:
        home = fetcher.get(root)
    except FetchError as e:
        trace.fail("discover", f"cannot open IR home page: {e}")
        return pages
    if not home.ok:
        trace.fail("discover", f"IR home page answered HTTP {home.status}")
        return pages
    root = home.final_url
    pages.append((root, home))
    seen = {_canon(root)}

    queue, all_events = harvest(BeautifulSoup(home.text, "lxml"), root, root)
    for key, item in _sitemap(fetcher, root).items():
        queue[key] = max(queue.get(key, (0, "")), item)

    visited = 0
    while visited < max_listing:
        todo = {k: v for k, v in queue.items() if k not in seen and v[0] >= min_score}
        if not todo:
            break
        key = max(todo, key=lambda k: todo[k][0])
        score, url = todo[key]
        seen.add(key)
        try:
            r = fetcher.get(url)
        except FetchError as e:
            trace.warn("discover", f"skip {url}: {e}")
            continue
        if not r.ok:
            trace.warn("discover", f"skip {url}: HTTP {r.status}")
            continue
        visited += 1
        pages.append((r.final_url, r))
        trace.info("discover", f"listing page (score {score:.0f}): {url}")
        more, evs = harvest(BeautifulSoup(r.text, "lxml"), r.final_url, root)
        for k, item in more.items():
            queue[k] = max(queue.get(k, (0, "")), item)
        for k, e in evs.items():
            all_events.setdefault(k, e)

    # Past events only: an upcoming event has no deck yet.
    dated = sorted(
        (e for e in all_events.values() if e.when and e.when <= today),
        key=lambda e: e.when,
        reverse=True,
    )
    undated = [e for e in all_events.values() if e.when is None]
    picks = dated[:max_events] or undated[: max_events // 2]
    for e in picks:
        if _canon(e.url) in seen:
            continue
        seen.add(_canon(e.url))
        try:
            r = fetcher.get(e.url)
        except FetchError as err:
            trace.warn("discover", f"skip event {e.url}: {err}")
            continue
        if r.ok:
            pages.append((r.final_url, r))
    if picks:
        trace.info(
            "discover",
            f"followed {len(picks)} most recent event pages "
            f"({len(all_events)} event links seen)",
        )
    return pages
