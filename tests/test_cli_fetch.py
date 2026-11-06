from pathlib import Path

import pandas as pd
import pytest

from lacounty_tax import fetch
from lacounty_tax.cli import main

FIXTURE = Path(__file__).parent / "fixtures" / "2026B-sample-pages.pdf"


def test_cli_parse_writes_csv(tmp_path, capsys):
    out = tmp_path / "items.csv"
    assert main(["parse", str(FIXTURE), "--out", str(out), "--strict"]) == 0
    df = pd.read_csv(out, dtype={"ain": str, "zip": str, "zip4": str})
    assert len(df) == 63 and df.loc[0, "ain"] == "2024002096"
    assert "63 items" in capsys.readouterr().out


def test_cli_parse_default_path_uses_detected_auction(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["parse", str(FIXTURE)]) == 0
    assert (tmp_path / "data/auctions/2026B/items.csv").exists()


class FakeResp:
    def __init__(self, status, body, ctype):
        self.status_code, self.content, self.headers = status, body, {"content-type": ctype}


def test_download_rejects_bot_challenge_page(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: FakeResp(403, b"<html>Incapsula</html>", "text/html"))
    with pytest.raises(fetch.FetchError, match="data/inbox"):
        fetch.download_pdf("https://example.test/x.pdf", tmp_path / "x.pdf")
    assert not (tmp_path / "x.pdf").exists()


def test_download_saves_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(fetch.requests, "get", lambda *a, **k: FakeResp(200, b"%PDF-1.4 ...", "application/pdf"))
    dest = fetch.download_pdf("https://example.test/x.pdf", tmp_path / "sub" / "x.pdf")
    assert dest.read_bytes().startswith(b"%PDF")
