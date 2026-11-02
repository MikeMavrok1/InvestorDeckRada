# IR Deck Finder

Give it a company name; it finds the company's investor-relations (IR) site, works out which
document there is the latest corporate presentation, downloads it, and explains how it decided.

```
uv sync
uv run irdecks run --all                 # Moderna, BioNTech, CRISPR Therapeutics, Beam Therapeutics
uv run irdecks run "Intellia Therapeutics"   # any name: resolved from scratch
uv run irdecks run --all --offline       # replay the last run from cache, no network
uv run pytest                            # 125 offline tests (138 with the web extra); --live hits the real sites
uv sync --extra web && uv run irdecks serve   # optional web UI, see "Web UI" below
```

Each run prints a step-by-step decision trace and writes `output/report.html`,
`output/manifest.json` and the PDFs under `output/<TICKER>/`.

## Result on the four test companies (run on 2 Oct 2026)

| Company | Picked | Dated | Why this one |
|---|---|---|---|
| Moderna | Second Quarter 2026 earnings slides | 31 Jul 2026 | No standing corporate deck on the site; this is the newest investor deck |
| BioNTech | Second Quarter 2026 Earnings Presentation | 4 Aug 2026 | Same: no standing corporate deck; newest investor deck |
| CRISPR Therapeutics | Corporate Presentation ("Q3 2026 Corporate Deck") | 3 Aug 2026 | Standing corporate deck, current |
| Beam Therapeutics | Corporate Presentation ("Corporate Deck August 2026") | 4 Aug 2026 | Standing corporate deck, current; a newer program-specific deck (BEAM-302 ERS update, 8 Sep) is reported separately |

All four are confirmed PDFs, 29 to 43 landscape pages, with the date cross-checked against the
cover slide.

## How it works

One generic pipeline, no per-site scraper. Every stage writes to the trace.

| Stage | Module | What it does |
|---|---|---|
| Resolve | `resolve.py` | Name → IR site. SEC EDGAR ticker list fixes the legal name and ticker; Wikidata gives the official website (matched on ticker); the home page's "Investors" link and guessed `investors.`/`ir.` hosts are opened and scored. Web search is a last resort. |
| Fetch | `fetch.py` | curl_cffi impersonating Chrome, per-host rate limit, robots.txt, retries, disk cache. A host that does not answer at all is skipped for the rest of the run. Headless Chromium as a second tier. |
| Discover | `discover.py` | Scores links and sitemap entries by words in the URL and anchor text, visits the best few listing pages, and follows the most recent event-detail pages. |
| Extract | `extract.py` | Finds file links (not only `.pdf`), and reads the "row" of text around each for its title and date. Also reads file URLs out of `<script>` data, and HTML kept as strings in the page's JSON data (Next.js `__NEXT_DATA__`). In a grid with one file per column, the title is the row header plus the column header ("Presentation – Q2 2026"). |
| Platform feed | `feeds.py` | When no deck survives (no links, or every file found was rejected) and the site is a Q4 site, reads its JSON events and quarterly-results feeds (this year and last), on the host the page's own `data-path` attribute names if it has one. Runs before the browser tier. |
| Classify | `classify.py` | Keyword rules sort each document into corporate overview, earnings, conference, investor day, data update, scientific, transcript, press release, filing, other. "Transcript", "press release" or an SEC form (10-Q, 8-K…) in the title, link text or filename decides the type outright: deck words elsewhere cannot outvote it. |
| Date | `dates.py` | Collects every date clue and ranks them (see below). |
| Probe | `retrieve.py` | A 1 KB range request per shortlisted file gets the real filename, size and upload time. |
| Select | `select.py` | The rule for "correct and most recent" (see below). |
| Verify | `retrieve.py` | Downloads the pick, checks it is a PDF and a slide deck (landscape, at least 4 pages), reads the cover slide, saves it. A file that fails is dropped and the next candidate is tried. |
| LLM fallback | `llm.py` | Optional. Used only when a page yields no links or the pick's type is a near-tie. |
| Report | `report.py` | HTML report and JSON manifest. |

## What the four sites taught

**Moderna** (Issuer Direct on Duda). The page has one PDF anchor. The other 218 files are in a
JavaScript array inside the HTML, as objects with `presentationURL`, `transcriptURL`,
`pressReleaseURL`. The extractor reads those objects directly, so no browser is needed, and the key
name tells slides from transcripts.

**BioNTech** (Notified, behind Akamai). Links are bare "Download" buttons, so title and date come
from the surrounding block. Files have no `.pdf` in the URL (`/static-files/<uuid>`). Older decks
are only attached to event-detail pages, which the crawler follows.

**CRISPR Therapeutics** (Notified). The Presentations page does not contain the corporate deck. It
is an undated button on the IR home page. So the home page is always read, and the date comes from
the file: server filename "Q3 2026 Corporate Deck", cover slide "August 3rd, 2026".

**Beam Therapeutics** (Notified). The corporate deck is undated on the listing, and the same URL
is also attached to the J.P. Morgan event of 13 January 2026. Beam replaced the file in place: the
filename, upload time, PDF metadata and cover slide all say August 2026. Trusting the listing would
have dated the deck seven months too early, so when at least two clues from the file agree it is
newer than the listing, the file wins and the report says so. One of them must be written by a
person (filename or cover slide): upload time and PDF metadata alone can both move when a CMS
re-processes old files.

**Bot filtering.** The three Akamai-fronted sites reset the connection when the User-Agent says
Chrome but the TLS handshake does not look like Chrome. Plain `curl`, `requests` and PowerShell all
failed. curl_cffi sends a browser-consistent fingerprint and gets through without a browser.

**Pfizer** (Q4, behind Cloudflare). Not one of the 12; it failed when tried by hand on 2 Oct 2026.
The Quarterly Results page lists its decks with JavaScript, so the static HTML has no deck link.
The headless browser did not help: Cloudflare answered it with a 403 "Just a moment..." challenge
page. Q4 sites load those lists from `/feed/Event.svc/GetEventList` and
`/feed/FinancialReport.svc/GetFinancialReportList`, which answer a plain HTTP request with JSON, no
key. The order of fallbacks is therefore: static HTML, platform feed, browser, LLM. The pick is the
Q2 2026 earnings deck, 4 Aug 2026, 37 landscape pages. Two details: the reports feed dates a
report by its quarter end (30 Jun), so no date is taken from it, and the event feed supplies the
real day. The browser tier now says when it was blocked instead of failing silently.

## Which presentation is "the correct one"

1. Only investor-facing slide decks compete. Transcripts, press releases, filings and scientific
   posters are excluded by type, and anything portrait is excluded after download.
2. If the company keeps a standing corporate deck and it is current (at most 180 days old, or no
   more than about a quarter behind the newest deck), that is the answer, even if a newer
   event-specific deck exists.
3. Otherwise the newest investor deck wins: earnings, conference or investor day.
4. The newest deck of any eligible kind is always reported as `latest_any`, with up to six
   alternates, so a different preference can be applied without re-running.

The two thresholds are judgment calls and are constants at the top of `select.py`.

**Date evidence, most trusted first:** date printed beside the link → date on the event page →
date in the title → date in a row shared with sibling links → date in the filename → date on the
cover slide → month-only listing date → PDF creation date → HTTP `Last-Modified`. A quarter label
("2Q26") is treated as a description, not a date. Future dates are ignored. All clues are kept
in the report, and disagreements over 45 days are flagged.

## Hold-out check

Eight biotechs the code was never tuned on, resolved from the name alone
(`uv run irdecks run --all --set holdout`).

- **First pass: 6 of 8.** Both failures were in name resolution, not extraction.
  - argenx: a filter for social-media hosts matched `x.com` inside `argenx.com`.
  - Ionis: Wikidata has no ticker for it, and the web search fallback was throttled.
- **After two generic fixes: 8 of 8**, plus the four test companies resolved from names alone.
- The run also exposed three extraction bugs that did not change a pick but would have at scale:
  an HTML comment leaking into a title, a date borrowed from a neighbouring list item, and a deck
  attached to two events taking the later date. Each now has a test.

The hold-out picks were checked from their evidence (server filename, cover-slide text, dates), not
by a person opening each site, and 8 names is a small sample.

## Limits

- **LLM fallback has not been run against the live API**: there are no Anthropic credentials on
  this machine. Its control flow is tested with a stub, including rejection of URLs that were not
  on the page.
- **The browser tier was not needed by any of the 12 companies.** It was tested directly (it
  renders argenx's JavaScript event list: 4 files instead of 1) and through a fake site. Titles on
  rendered pages are less precise. It is blocked by Cloudflare (Pfizer) and probably by any similar
  bot shield; the trace says so.
- **Only Q4 has a platform feed adapter**, and it was verified on two companies (Pfizer,
  and Bristol Myers Squibb, whose feed is on a `q4web.com` host named in its page; BMS is not one of
  the 12 and was checked by hand and offline, not with `pytest --live`). Of the 12 companies
  plus Pfizer, 9 are Notified, 1 is Issuer Direct and 1 is Q4. The Notified hosts answer 403
  "Access Denied" to Q4's `/feed/` paths, so the Q4 route does not apply to them; they work through
  the generic HTML extraction. Other vendors' feeds have not been looked at. A site on an unknown
  platform that builds its list with JavaScript behind a bot shield still ends in "no eligible
  presentation found".
- Hosts that fingerprint as Q4 but have no feed (Alnylam answers 404) simply fall through to the
  browser tier.
- Moderna and BioNTech publish no standing corporate deck, so an earnings deck is returned. If the
  J.P. Morgan deck is what a reader means by "corporate presentation", it is in the alternates.
- Only the first page of a paginated list is read, on the assumption that newest comes first.
- Numeric dates are read in US order unless the first number is above 12.
- Keyword rules are English-only.

## Web UI

An optional Streamlit front end over the same pipeline (`src/irdecks/webui.py`). It changes
nothing about how a deck is chosen; it shows the `RunResult`.

```
uv sync --extra web          # add --extra browser to keep the headless-browser tier too
uv run irdecks serve         # http://localhost:8501  (--port to change)
```

Sidebar: a name or IR-site box, buttons for the four test companies, and an **Offline (replay
from cache)** switch (the CLI's `--offline`; it also turns the LLM fallback off). A run shows a
spinner, then the pick, why it was chosen, the
candidates (pick, latest of any kind, alternates) with why each won or lost, every date clue with
its rank and any disagreement over 45 days, the cover-slide text, the PDF (download, page count,
orientation, link to the original) and the full trace. Failures (nothing found, a bot challenge
on the browser tier, an offline cache miss) show a message, not a stack trace. Only the pick,
`latest_any` and up to six alternates are in a result, so those are the candidates listed.
A UI run saves the PDF under `output/<TICKER>/` like the CLI, but does not write `report.html` or
`manifest.json`. Start it from the repository root: it reads `companies.csv` and `output/cache`
there (`IRDECKS_OUT_DIR` moves the output folder).

`uv run pytest tests/test_webui.py` drives the app with `streamlit.testing.v1`, offline (13 tests);
without the web extra the file is skipped.

## Scaling to 1,000+ companies

- **Cost today:** about 18 requests and 20 seconds per company cold, limited by a 1 request/second
  per-host courtesy delay. Hosts are independent, so 20 workers would cover 1,000 companies in
  roughly 20 minutes.
- **Resolve once, store it.** The IR URL changes rarely. Keep it in a table with the date it was
  last validated; re-resolve only on failure. Replace the web-search fallback with a keyed search
  API or licensed reference data.
- **Remember the fetch tier per host.** 9 of the 12 sites here are one vendor (Notified). Record
  platform and the tier that worked, so the browser is only launched where it is needed. Add a
  feed adapter per vendor (Q4 is done) rather than a rule per company.
- **Incremental runs.** Store ETag, `Last-Modified` and the PDF hash. Most daily checks become one
  conditional request per listing page, and a changed hash on an unchanged URL catches in-place
  replacements like Beam's.
- **Confidence as a queue.** Every result has a confidence score and a trace. Low confidence, a
  date conflict, or a change of deck type from the previous run goes to a human review list.
- **Regression set.** The saved pages in `tests/fixtures/` are the start of a golden set. Add a
  page for every failure found, and alert when a platform's hit rate drops (a vendor redesign).
- **Second source.** US-listed companies often furnish decks to the SEC as 8-K Exhibit 99 (6-K for
  foreign filers). EDGAR is uniform, so it is a cross-check and a fallback when a site is down.
- **LLM budget.** Rules handled all 12 companies. Keep the LLM for the long tail, cache its
  answers by page hash, and batch.
- **Storage.** SQLite for the prototype, then Postgres for metadata and object storage for PDFs.

## Notes on access

Every web page fetch obeys robots.txt and the per-host delay; responses are cached for 12 hours.
robots.txt is read the RFC 9309 way (protego): the longest matching rule wins. Python's
`urllib.robotparser` takes the first one, and that refused Bayer's deck, whose file host says
`Disallow: /` above `Allow: /api/public/`.
SEC EDGAR's ticker file and the Wikidata API are called as the machine APIs they are. SEC asks
automated clients to identify themselves with a contact address in the User-Agent. This prototype
does not do that yet and should before it runs at volume.
