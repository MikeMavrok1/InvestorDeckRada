"""Recognise which IR website vendor a page comes from.

Extraction does not depend on this (the generic extractor is the baseline). The
fingerprint is recorded so that, at scale, results and failures can be grouped by
vendor and a fetch strategy can be remembered per platform.
"""

from __future__ import annotations

import re

_SIGNATURES: list[tuple[str, re.Pattern]] = [
    ("notified", re.compile(r"nir-widget|node--type-nir|block-nir-|gcs-web\.com", re.I)),
    ("q4", re.compile(r"q4cdn\.com|q4web|Q4 Inc|/feed/\w+\.svc", re.I)),
    ("issuerdirect", re.compile(r"issuerdirect\.com|irp\.cdn-website\.com", re.I)),
    ("equisolve", re.compile(r"equisolve|d1io3yog0oux5\.cloudfront\.net", re.I)),
    ("investis", re.compile(r"investis\.com|idmanagedsolutions", re.I)),
    ("wordpress", re.compile(r"/wp-content/|/wp-includes/", re.I)),
]


def fingerprint(html: str) -> str:
    counts = {name: len(rx.findall(html)) for name, rx in _SIGNATURES}
    name, hits = max(counts.items(), key=lambda kv: kv[1])
    return name if hits >= 2 else "generic"
