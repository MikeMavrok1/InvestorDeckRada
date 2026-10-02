"""Turn one HTML page into candidate documents, each with the context needed to judge it.

Two sources are read, neither tied to a particular vendor:
  * anchors that point at a file, with the "row" of text around them;
  * object literals inside <script> blocks that carry a file URL (sites that render
    their lists with JavaScript still ship the data in the page).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag

from .classify import GENERIC_LINK
from .dates import DateHit, add_evidence, find_dates
from .models import Candidate

_DOC_EXT = re.compile(r"\.(?:pdf|pptx?)(?:$|[?#])", re.I)
_DOC_PATH = re.compile(
    r"/static-files/|/files-encrypted/|/system/files/|"
    r"/doc_(?:presentations?|events?|financials?|downloads?)/", re.I,
)
_FILEISH = re.compile(r"/(?:files?|media|assets|documents?|downloads?|uploads?|wp-content)/", re.I)
_NOT_DOC_EXT = re.compile(
    r"\.(?:jpe?g|png|gif|svg|webp|mp[34]|ics|vcs|zip|xlsx?|csv|docx?|html?|aspx|php|js|css)(?:$|[?#])",
    re.I,
)
_DECK_WORDS = re.compile(r"\b(?:presentation|slides?|deck)\b", re.I)
_ROLE_ONLY = re.compile(
    r"^(?:(?:view|download|webcast|earnings|event|supporting)\s+)?"
    r"(?:presentation|slides?|slide\s+deck|materials?|transcript|press\s+release|pdf)s?\W*$",
    re.I,
)
_DECORATION = re.compile(r"[\s›»→>▸▶]+$")  # arrows that dress up a link label
_FILESIZE = re.compile(r"\b\d+(?:\.\d+)?\s?(?:KB|MB|GB)\b", re.I)
_CTA = re.compile(r"\b(?:view|download|open)\s+(?:the\s+)?(?:pdf|file)\b|[›»→▸▶]", re.I)
# clock times; the zone must be upper-case so "at 4:30 PM EDT" goes but a following word stays
_CLOCK = re.compile(r"\b(?:at\s+)?\d{1,2}:\d{2}\s*(?:[AaPp]\.?[Mm]\.?)?(?:\s+[A-Z]{2,4}\b)?")
_LABELS = re.compile(
    r"\b(?:supporting|related|event)\s+materials?\b|\blisten\s+to\s+(?:the\s+)?webcast\b", re.I
)
_TIME_ONLY = re.compile(r"^\W*(?:at\s+)?\d{1,2}:\d{2}", re.I)
_HEADING_CLASS = re.compile(r"widget--label|section-title|block-title|module_title|heading", re.I)
_EVENT_URL = re.compile(
    r"event-?details?|[?&]eventid=|"
    r"/events?/(?!presentations?|past|upcoming|archive|calendar|webcasts?)[^/?#]{6,}", re.I,
)

_PRECISION_ORDER = {"day": 0, "month": 1, "quarter": 2}


def norm(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def is_doc_url(url: str, mime: str | None = None, text: str = "") -> bool:
    if not url or url.startswith(("mailto:", "javascript:", "tel:", "#")):
        return False
    if _NOT_DOC_EXT.search(urlsplit(url).path):
        return False
    if _DOC_EXT.search(url) or _DOC_PATH.search(url) or (mime or "").lower() == "application/pdf":
        return True
    return bool(_FILEISH.search(url) and _DECK_WORDS.search(text))


def is_event_page(url: str) -> bool:
    return bool(_EVENT_URL.search(url))


@dataclass
class PageContext:
    url: str
    heading: str
    event_page: bool
    event_date: DateHit | None


def page_context(soup: BeautifulSoup, url: str) -> PageContext:
    h1 = soup.find("h1")
    heading = norm(h1.get_text(" ", strip=True)) if h1 else ""
    if not heading and soup.title:
        heading = norm(soup.title.get_text())
    event = is_event_page(url)
    event_date = None
    if event:
        # The event's own date sits right under its heading on a detail page.
        start = h1 or soup.body
        chunk, size = [], 0
        for s in start.find_all_next(string=True) if start else []:
            if s.parent.name in ("script", "style"):
                continue
            chunk.append(str(s))
            size += len(s)
            if size > 1500:
                break
        days = [h for h in find_dates(" ".join(chunk)) if h.precision == "day"]
        event_date = days[0] if days else None
    return PageContext(url, heading[:160], event, event_date)


# -- anchors ---------------------------------------------------------------------


def _anchor_is_doc(a: Tag) -> bool:
    return is_doc_url(a.get("href", ""), a.get("type"), a.get_text(" ", strip=True))


def _doc_count(el: Tag) -> int:
    return sum(1 for a in el.find_all("a", href=True) if _anchor_is_doc(a))


def _link_count(el: Tag) -> int:
    return len({
        a["href"] for a in el.find_all("a", href=True)
        if not a["href"].startswith(("#", "javascript:", "mailto:"))
    })


def _text_len(el: Tag) -> int:
    return len(norm(el.get_text(" ", strip=True)))


def _rows(a: Tag) -> tuple[Tag, Tag]:
    """(own row, group row).

    Own row: the largest block around the link that holds no other link at all, so
    text in it can only describe this file.
    Group row: a slightly larger block that may hold sibling links of the same event
    (webcast, slides, transcript, press release) and usually carries its title and date.
    """
    own = a
    while isinstance(own.parent, Tag) and own.parent.name not in ("body", "html"):
        p = own.parent
        if _link_count(p) > 1 or _text_len(p) > 400:
            break
        own = p
    group = own
    while isinstance(group.parent, Tag) and group.parent.name not in ("body", "html"):
        p = group.parent
        if _doc_count(p) > 4 or _link_count(p) > 6 or _text_len(p) > 600:
            break
        group = p
    return own, group


def _is_text(el) -> bool:
    """Visible text only: HTML comments and script/style bodies are not page content."""
    return (
        isinstance(el, NavigableString)
        and not isinstance(el, Comment)
        and el.parent.name not in ("script", "style")
    )


def _around(container: Tag, a: Tag, skip_links: bool = False) -> tuple[str, str]:
    """Text inside `container` before and after the link (link text excluded).

    `skip_links` also drops the labels of sibling links, which name other files.
    """
    before: list[str] = []
    for el in a.previous_elements:
        if el is container:
            break
        if _is_text(el):
            if container not in el.parents:
                break
            if skip_links and any(p.name == "a" for p in el.parents):
                continue
            before.append(str(el))
    after: list[str] = []
    for el in a.next_elements:
        if _is_text(el):
            if a in el.parents:
                continue
            if container not in el.parents:
                break
            after.append(str(el))
    return norm(" ".join(reversed(before))), norm(" ".join(after))


def _row_date(before: str, after: str) -> DateHit | None:
    """Nearest date to the link, preferring exact days over months over quarters."""
    b = find_dates(before)
    f = find_dates(after)
    # distance: hits before the link count from the end, hits after from the start
    ranked = [(_PRECISION_ORDER[h.precision], len(before) - h.pos, h) for h in b]
    ranked += [(_PRECISION_ORDER[h.precision], len(before) + h.pos, h) for h in f]
    ranked = [r for r in ranked if r[0] < 2]  # quarter labels describe content, not the date
    return min(ranked, key=lambda r: (r[0], r[1]))[2] if ranked else None


def _strip_noise(text: str, *remove: str) -> str:
    for r in remove:
        if r:
            text = text.replace(r, " ")
    text = _CLOCK.sub(" ", _LABELS.sub(" ", _CTA.sub(" ", _FILESIZE.sub(" ", text))))
    return norm(text).strip(" -–—|:,•")


def _nearest_label(container: Tag, a: Tag, date_raw: str) -> str:
    """The closest piece of real text before the link: its caption in a busy block."""
    for el in a.previous_elements:
        if el is container:
            break
        if not _is_text(el):
            continue
        if container not in el.parents:
            break
        if any(p.name == "a" for p in el.parents):
            continue  # another link's label, not ours
        text = _strip_noise(str(el), date_raw)
        if len(text) >= 6 and not _TIME_ONLY.match(text):
            return text
    return ""


def _section(a: Tag, limit: int = 600) -> str:
    for i, el in enumerate(a.previous_elements):
        if i > limit:
            break
        if not isinstance(el, Tag):
            continue
        cls = " ".join(el.get("class", []))
        if el.name in ("h1", "h2", "h3", "h4", "h5") or _HEADING_CLASS.search(cls):
            text = norm(el.get_text(" ", strip=True))
            if 2 < len(text) < 80:
                return text
    return ""


def _from_links(soup: BeautifulSoup, ctx: PageContext) -> list[Candidate]:
    out: dict[str, Candidate] = {}
    for a in soup.find_all("a", href=True):
        if not _anchor_is_doc(a):
            continue
        url = urldefrag(urljoin(ctx.url, a["href"].strip()))[0]
        link = _DECORATION.sub("", norm(a.get_text(" ", strip=True)) or norm(a.get("aria-label", "")))
        own, group = _rows(a)
        before, after = _around(own, a)
        hit, hit_source = _row_date(before, after), "listing"
        g_before, g_after = (before, after) if group is own else _around(group, a)
        if hit is None and group is not own:
            # In a shared row only plain text leading up to the link is trusted: what
            # follows may belong to the next item, and a date inside another link's
            # label describes that other file.
            hit = _row_date(_around(group, a, skip_links=True)[0], "")
            hit_source = "shared_row"

        informative = bool(link) and len(link) >= 4 and not GENERIC_LINK.match(link)
        row_text = _strip_noise(f"{before} {after}", hit.raw if hit else "")
        if informative and not _ROLE_ONLY.match(link):
            title, quality = link, 2
        else:
            # "Download" / "Slides" says nothing: the name is in the surrounding row.
            event = row_text
            if len(event) > 120:
                event = _nearest_label(own, a, hit.raw if hit else "") or event
            if len(event) < 6 and group is not own:
                event = _strip_noise(_around(group, a, skip_links=True)[0], hit.raw if hit else "")
            if len(event) < 6 and ctx.event_page:
                event = ctx.heading
            role = link if informative else ""
            if len(event) >= 6:
                title = f"{event} – {role}" if role else event
                quality = 2 if role else 1
            else:
                title, quality = (link or ctx.heading or url.rsplit("/", 1)[-1]), 0

        c = Candidate(
            url=url,
            title=title[:220],
            link_text=link[:120],
            context=norm(f"{g_before} [{link}] {g_after}")[:400],
            section=(ctx.heading if ctx.event_page else _section(a))[:120],
            source_page=ctx.url,
            seen_on=[ctx.url],
            filename=norm(a.get("title", "")) or None,
            title_quality=quality,
        )
        add_evidence(c, hit_source, hit)
        if hit is None and ctx.event_page:
            add_evidence(c, "event_page", ctx.event_date)
        if url not in out or c.title_quality > out[url].title_quality:
            out[url] = c
    return list(out.values())


# -- script data -----------------------------------------------------------------

_JS_STRING = re.compile(r"""(["'])((?:https?:)?\\?/[^"'<>\s]{4,600})\1""")
_KV = re.compile(
    r"""["']?([A-Za-z_$][\w$]*)["']?\s*:\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')"""
)
_TITLE_KEYS = ["title", "event", "eventtitle", "name", "headline", "heading", "label", "subject"]
_URL_SUFFIX = re.compile(r"(?:url|link|href|file|path|pdf|src)$", re.I)


def _unescape(s: str) -> str:
    s = s.replace("\\/", "/")
    s = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m[1], 16)), s)
    return s.replace('\\"', '"').replace("\\'", "'")


def _enclosing_object(text: str, start: int, end: int, span: int = 6000) -> str:
    depth, lo = 0, max(0, start - span)
    i = start - 1
    while i >= lo:
        ch = text[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            if depth == 0:
                break
            depth -= 1
        i -= 1
    depth, hi = 0, min(len(text), end + span)
    j = end
    while j < hi:
        ch = text[j]
        if ch == "{":
            depth += 1
        elif ch == "}":
            if depth == 0:
                break
            depth -= 1
        j += 1
    return text[max(i, lo) : j + 1]


def _key_to_role(key: str) -> str:
    key = _URL_SUFFIX.sub("", key)
    words = re.sub(r"([a-z])([A-Z])", r"\1 \2", key).replace("_", " ")
    return norm(words).lower()


def _from_scripts(soup: BeautifulSoup, ctx: PageContext) -> list[Candidate]:
    out: dict[str, Candidate] = {}
    for script in soup.find_all("script"):
        text = script.string or ""
        if len(text) < 40 or (".pdf" not in text.lower() and "static-files" not in text):
            continue
        for m in _JS_STRING.finditer(text):
            raw = _unescape(m.group(2))
            if not is_doc_url(raw):
                continue
            url = urldefrag(urljoin(ctx.url, raw))[0]
            obj = _enclosing_object(text, m.start(), m.end())
            pairs = [(k, _unescape(a or b)) for k, a, b in _KV.findall(obj)]
            fields = {k.lower(): norm(v) for k, v in pairs if v}
            own_key = next((k for k, v in pairs if _unescape(v) == raw), "")
            role = _key_to_role(own_key)

            name = next((fields[k] for k in _TITLE_KEYS if fields.get(k)), "")
            hit = None
            for k, v in fields.items():
                if "date" in k and (days := [h for h in find_dates(v) if h.precision == "day"]):
                    hit = days[0]
                    break
            if not name:
                continue  # a bare URL in a script (e.g. a PDF viewer config) carries no context
            c = Candidate(
                url=url,
                title=(f"{name} – {role}" if role else name)[:220],
                link_text=role,
                context=norm(" | ".join(v for v in fields.values() if not v.startswith("http")))[:400],
                section=ctx.heading[:120],
                source_page=ctx.url,
                seen_on=[ctx.url],
                found_via="script",
                title_quality=3,
            )
            add_evidence(c, "listing", hit)
            out.setdefault(url, c)
    return list(out.values())


# -- public ----------------------------------------------------------------------


def extract_candidates(html: str, page_url: str) -> tuple[list[Candidate], BeautifulSoup]:
    soup = BeautifulSoup(html, "lxml")
    ctx = page_context(soup, page_url)
    found = _from_links(soup, ctx) + _from_scripts(soup, ctx)
    for c in found:
        for hit in find_dates(c.title):
            add_evidence(c, "title", hit)
    return merge(found), soup


def merge(cands: list[Candidate]) -> list[Candidate]:
    """One candidate per URL: keep the best-described copy, pool the evidence."""
    best: dict[str, Candidate] = {}
    for c in cands:
        cur = best.get(c.url)
        if cur is None:
            best[c.url] = c
            continue
        keep, drop = (c, cur) if c.title_quality > cur.title_quality else (cur, c)
        for ev in drop.date_evidence:
            if ev not in keep.date_evidence:
                keep.date_evidence.append(ev)
        for page in drop.seen_on:
            if page not in keep.seen_on:
                keep.seen_on.append(page)
        keep.filename = keep.filename or drop.filename
        best[c.url] = keep
    return list(best.values())
