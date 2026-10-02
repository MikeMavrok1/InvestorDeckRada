"""Company name -> investor-relations site, without any API key.

Order of trust:
  1. a verified override in companies.csv;
  2. SEC EDGAR's public ticker list pins down the legal name and ticker;
  3. Wikidata gives the company's official website (matched on the ticker), whose home
     page is read for its "Investors" link, plus guessed investor hostnames;
  4. a web search, only if that found nothing: search engines throttle scripted
     queries within a few requests, so this cannot carry a large universe.
Every candidate is opened and scored. The ticker check is what stops a look-alike
(searching "Vertex" also returns Vertex Inc.'s IR site).
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urljoin, urlsplit

from bs4 import BeautifulSoup
from rapidfuzz import fuzz

from .discover import score_link
from .fetch import Fetcher, FetchError
from .models import Company
from .trace import Trace

SEC_TICKERS = "https://www.sec.gov/files/company_tickers.json"
WIKIDATA = "https://www.wikidata.org/w/api.php"
SEARCH = "https://html.duckduckgo.com/html/?q={q}"
GOOD_ENOUGH = 9.0  # ticker + name + investor site + listing links
MIN_SCORE = 5.0

_SUFFIXES = re.compile(
    r"\b(?:inc|incorporated|corp|corporation|co|company|ltd|limited|plc|se|ag|nv|sa|"
    r"holdings?|group|the|adr|ads)\b"
)
_IR_HOST = re.compile(r"^(?:investors?|ir|investor-relations)\.", re.I)
_IR_PATH = re.compile(r"/(investors?|investor-relations|ir)(?=/|$)", re.I)
_IR_TEXT = re.compile(r"\binvestors?\b", re.I)
_NOISE_HOSTS = re.compile(
    r"wikipedia|linkedin|yahoo|bloomberg|marketwatch|nasdaq\.com|sec\.gov|seekingalpha|"
    r"globenewswire|businesswire|prnewswire|stockanalysis|google|facebook|twitter|"
    r"reuters|fool\.com|morningstar|investing\.com|duckduckgo|youtube|crunchbase|zacks|"
    r"macrotrends|tipranks|cnbc|wsj\.com|barrons|benzinga|marketscreener|"
    r"(?:^|\.)(?:x|ft)\.com$|"
    r"financialfilings|quartr|simplywall|marketbeat|finviz|tradingview", re.I,
)


def _norm(name: str) -> str:
    name = re.sub(r"/.*$", "", name.lower())  # SEC titles like "VERTEX ... INC / MA"
    name = re.sub(r"[^a-z0-9 ]+", " ", name)
    return re.sub(r"\s+", " ", _SUFFIXES.sub(" ", name)).strip()


# -- 1. overrides ------------------------------------------------------------------


def _from_overrides(query: str, path: Path) -> Company | None:
    if not path.exists():
        return None
    q = _norm(query)
    with path.open(encoding="utf-8", newline="") as f:
        for row in csv.DictReader(f):
            if not (row.get("ir_url") or "").strip():
                continue
            if q in (_norm(row["name"]), (row.get("ticker") or "").lower()) or (
                fuzz.ratio(q, _norm(row["name"])) >= 90
            ):
                return Company(
                    query=query, name=row["name"], ticker=row.get("ticker") or None,
                    ir_url=row["ir_url"].strip(), resolved_by="override (companies.csv)",
                )
    return None


# -- 2. SEC EDGAR ------------------------------------------------------------------


def _from_sec(query: str, fetcher: Fetcher, trace: Trace) -> tuple[str, str | None, str | None]:
    """(legal name, ticker, cik). The SEC list is ordered by market cap, which breaks ties."""
    try:
        rows = list(json.loads(fetcher.get(SEC_TICKERS, api=True).text).values())
    except (FetchError, ValueError) as e:
        trace.warn("resolve", f"SEC ticker list unavailable ({e}); continuing with the raw name")
        return query, None, None
    q = _norm(query)
    best, best_score = None, 0.0
    for row in rows:
        title = _norm(row["title"])
        if q == title or q == row["ticker"].lower():
            best, best_score = row, 100.0
            break
        s = fuzz.WRatio(q, title)
        if title.startswith(q + " "):
            s = max(s, 95)
        if s > best_score:
            best, best_score = row, s
    if best is None or best_score < 88:
        trace.warn("resolve", f"no confident SEC match for '{query}'")
        return query, None, None
    trace.info(
        "resolve",
        f"SEC EDGAR: '{best['title']}' ticker {best['ticker']} CIK {best['cik_str']} "
        f"(match {best_score:.0f})",
    )
    return best["title"], best["ticker"], str(best["cik_str"])


# -- 3. Wikidata -> official website -------------------------------------------------


def _wikidata(params: dict, fetcher: Fetcher) -> dict:
    return json.loads(fetcher.get(f"{WIKIDATA}?{urlencode(params)}", api=True).text)


def _official_site(query: str, name: str, ticker: str | None, fetcher: Fetcher, trace: Trace) -> str | None:
    try:
        ids: list[str] = []
        for term in dict.fromkeys([query, _norm(name)]):
            hits = _wikidata(
                {"action": "wbsearchentities", "search": term, "language": "en",
                 "type": "item", "limit": 5, "format": "json"}, fetcher,
            ).get("search", [])
            ids += [h["id"] for h in hits if h["id"] not in ids]
        if not ids:
            return None
        entities = _wikidata(
            {"action": "wbgetentities", "ids": "|".join(ids[:8]), "props": "claims|labels",
             "languages": "en", "format": "json"}, fetcher,
        ).get("entities", {})
    except (FetchError, ValueError, KeyError) as e:
        trace.warn("resolve", f"Wikidata lookup failed ({str(e)[:60]})")
        return None

    fallback = None
    for qid in ids[:8]:
        claims = entities.get(qid, {}).get("claims", {})
        label = entities.get(qid, {}).get("labels", {}).get("en", {}).get("value", "")
        sites = [c["mainsnak"]["datavalue"]["value"] for c in claims.get("P856", [])
                 if "datavalue" in c.get("mainsnak", {})]
        if not sites:
            continue
        tickers = [q["datavalue"]["value"] for c in claims.get("P414", [])
                   for q in c.get("qualifiers", {}).get("P249", []) if "datavalue" in q]
        if ticker and ticker in tickers:
            trace.info("resolve", f"Wikidata {qid}: official site {sites[0]} (ticker {ticker} matches)")
            return sites[0]
        # No ticker recorded on Wikidata: accept a near-exact name match instead. The
        # ticker is still checked later, on the investor site itself.
        if fallback is None and fuzz.ratio(_norm(label), _norm(name)) >= 90:
            fallback = (qid, sites[0])
    if fallback:
        trace.info("resolve", f"Wikidata {fallback[0]}: official site {fallback[1]} (matched by name)")
        return fallback[1]
    return None


def _registrable(host: str) -> str:
    parts = host.lower().removeprefix("www.").split(".")
    n = 3 if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "ac") else 2
    return ".".join(parts[-n:])


def _as_ir_root(url: str) -> str | None:
    """Reduce a link to the root of an investor site, or None if it is not one."""
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or _NOISE_HOSTS.search(p.netloc):
        return None
    if _IR_HOST.match(p.netloc):
        return f"https://{p.netloc}/"
    if m := _IR_PATH.search(p.path):
        return f"https://{p.netloc}{p.path[: m.end()]}"
    return None


def _from_corporate_site(site: str, fetcher: Fetcher, trace: Trace) -> list[str]:
    """Investor links on the corporate home page, then guessed investor hostnames."""
    out: list[str] = []
    try:
        r = fetcher.get(site)
        if r.ok:
            soup = BeautifulSoup(r.text, "lxml")
            for a in soup.find_all("a", href=True):
                url = urljoin(r.final_url, a["href"].strip())
                root = _as_ir_root(url)
                if root is None and _IR_TEXT.search(a.get_text(" ", strip=True)):
                    p = urlsplit(url)
                    if p.scheme in ("http", "https") and not _NOISE_HOSTS.search(p.netloc):
                        root = url  # "Investors" link to an unusual address: keep as is
                if root and root not in out:
                    out.append(root)
    except FetchError as e:
        trace.warn("resolve", f"corporate site {site} unreachable ({str(e)[:50]})")
    host = urlsplit(site).netloc
    domain = _registrable(host)
    guesses = [f"https://{prefix}.{domain}/" for prefix in ("investors", "ir", "investor")]
    guesses += [f"https://{host}/{path}/" for path in ("investors", "investor-relations")]
    for guess in guesses:
        if guess not in out:
            out.append(guess)
    return out


def _guess_site(name: str) -> str | None:
    """www.<first word>.com: for when Wikidata's site is a stale or regional one that 404s
    (Takeda: takeda.co.jp answers 404; the global site and its /investors are on takeda.com)."""
    token = re.sub(r"[^a-z0-9]", "", _norm(name).split(" ")[0]) if _norm(name) else ""
    return f"https://www.{token}.com/" if len(token) >= 4 else None


# -- 4. web search -------------------------------------------------------------------


def _search(query: str, fetcher: Fetcher, trace: Trace) -> list[str]:
    try:
        r = fetcher.get(SEARCH.format(q=quote_plus(query)))
    except FetchError:
        return []
    if r.status != 200:
        trace.warn("resolve", f"web search throttled (HTTP {r.status})")
        return []
    urls: list[str] = []
    for href in re.findall(r'class="result__a"[^>]*href="([^"]+)"', r.text):
        href = href.replace("&amp;", "&")
        target = unquote(parse_qs(urlsplit(href).query).get("uddg", [href])[0])
        if (root := _as_ir_root(target)) and root not in urls:
            urls.append(root)
    return urls[:5]


# -- scoring -------------------------------------------------------------------------


def _score(url: str, name: str, ticker: str | None, fetcher: Fetcher) -> tuple[float, str]:
    try:
        r = fetcher.get(url)
    except FetchError as e:
        return -1.0, f"unreachable ({str(e)[:40]})"
    if not r.ok:
        return -1.0, f"HTTP {r.status}"
    soup = BeautifulSoup(r.text, "lxml")
    text = soup.get_text(" ", strip=True)
    title = soup.title.get_text(" ", strip=True) if soup.title else ""
    token = _norm(name).split(" ")[0] if _norm(name) else ""
    notes: list[str] = []
    score = 0.0

    has_name = bool(token) and token in text.lower()
    has_ticker = bool(ticker) and bool(re.search(rf"\b{re.escape(ticker)}\b", text))
    if not (has_name or has_ticker):
        return -1.0, "page mentions neither the company name nor its ticker"
    if has_ticker:
        score += 3
        notes.append("ticker on page")
    if token and token in title.lower():
        score += 2
        notes.append("name in title")
    p = urlsplit(r.final_url)
    if _IR_HOST.match(p.netloc) or _IR_PATH.search(p.path) or "investor" in title.lower():
        score += 3
        notes.append("investor site")
    links = [
        score_link(urljoin(r.final_url, a["href"]), a.get_text(" ", strip=True))
        for a in soup.find_all("a", href=True)
    ]
    if any(s >= 3 for s in links):
        score += 2
        notes.append("has events/presentations links")
    return score, ", ".join(notes)


def _best(
    urls: list[str], name: str, ticker: str | None, fetcher: Fetcher, trace: Trace,
    seen: dict[str, tuple[float, str]],
) -> None:
    for url in urls:
        if url in seen:
            continue
        seen[url] = _score(url, name, ticker, fetcher)
        if seen[url][0] >= 0:
            trace.info("resolve", f"candidate {url}  score {seen[url][0]:.0f}  ({seen[url][1]})")
        if seen[url][0] >= GOOD_ENOUGH:
            return


def resolve(
    query: str,
    fetcher: Fetcher,
    trace: Trace,
    overrides: Path,
    use_overrides: bool = True,
) -> Company:
    if query.startswith(("http://", "https://")):
        return Company(query=query, name=urlsplit(query).netloc, ir_url=query, resolved_by="url given")
    if use_overrides and (hit := _from_overrides(query, overrides)):
        trace.ok("resolve", f"{hit.name} -> {hit.ir_url} (verified override)")
        return hit

    name, ticker, cik = _from_sec(query, fetcher, trace)
    company = Company(query=query, name=name, ticker=ticker, cik=cik)
    seen: dict[str, tuple[float, str]] = {}
    how = ""

    site = _official_site(query, name, ticker, fetcher, trace)
    if site:
        _best(_from_corporate_site(site, fetcher, trace), name, ticker, fetcher, trace, seen)
        how = "Wikidata official site"
    if (not seen or max(s for s, _ in seen.values()) < MIN_SCORE) and (guess := _guess_site(name)):
        if guess != site:
            trace.info("resolve", f"trying {guess} (name-derived corporate site)")
            _best(_from_corporate_site(guess, fetcher, trace), name, ticker, fetcher, trace, seen)
            how = "name-derived corporate site"
    if not seen or max(s for s, _ in seen.values()) < MIN_SCORE:
        trace.info("resolve", "official-site route found nothing usable; trying a web search")
        _best(_search(f"{name} investor relations", fetcher, trace), name, ticker, fetcher, trace, seen)
        how = "web search"

    ranked = sorted(seen.items(), key=lambda kv: kv[1][0], reverse=True)
    if ranked and ranked[0][1][0] >= MIN_SCORE:
        company.ir_url = ranked[0][0]
        company.resolved_by = f"{how}, validated ({ranked[0][1][1]})"
        trace.ok("resolve", f"{name} -> {company.ir_url}")
    else:
        trace.fail("resolve", f"could not find an investor-relations site for '{query}'")
    return company
