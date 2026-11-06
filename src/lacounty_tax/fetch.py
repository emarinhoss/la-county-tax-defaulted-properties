"""Download an auction book PDF.

The county site sits behind bot protection and may refuse requests from
datacenter IPs (including cloud CI). We do not try to evade that: if the
response is not a PDF we fail with a clear message, and the book can be
placed in ``data/inbox/`` by hand instead.
"""
from __future__ import annotations

from pathlib import Path

import requests

USER_AGENT = "lacounty-tax-research/0.1 (personal research tool)"


class FetchError(RuntimeError):
    pass


def download_pdf(url: str, dest: Path, *, timeout: float = 60.0) -> Path:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=timeout)
    body = resp.content
    if resp.status_code != 200 or not body.startswith(b"%PDF"):
        raise FetchError(
            f"Expected a PDF from {url} but got HTTP {resp.status_code} "
            f"({resp.headers.get('content-type', 'unknown type')}). The site may be "
            "blocking automated requests; download the file in a browser and put it "
            "in data/inbox/ instead."
        )
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(body)
    return dest
