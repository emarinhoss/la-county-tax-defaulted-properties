import json
from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
import requests

from lacounty_tax.assessor import (
    AssessorClient, AssessorError, BlockedError, parse_date, summarize_ownership, summarize_parcel, to_number,
)
from lacounty_tax.cli import main
from lacounty_tax.enrich import build_snapshot, run_enrichment, update_status_history

DETAIL = {"Parcel": {
    "SitusStreet": "37616 NEWBURY PL", "SitusZipCode": "93552-3937", "TaxStatus": "DEFAULTED",
    "TaxDefaultedYear": "2019", "ParcelStatus": "ACTIVE", "NumOfUnits": "1", "YearBuilt": "1990",
    "SqftMain": "1,600", "SqftLot": "7217", "NumOfBeds": "3", "NumOfBaths": "2",
    "RollPreparation_LandValue": "44199", "RollPreparation_ImpValue": "132496"}}
HISTORY = {"Parcel_OwnershipHistory": [
    {"RecordingDate": "06/22/1988", "DocumentNumber": "0989843", "DocumentReasonCodeDesc": "Interspousal"},
    {"RecordingDate": "03/26/2024", "DocumentNumber": "0191595", "DocumentReasonCodeDesc": "Joint Tenancy"},
]}


class Resp:
    def __init__(self, status=200, data=None, text=None, ctype="application/json"):
        self.status_code, self.headers = status, {"content-type": ctype}
        self.text = text if text is not None else json.dumps(data)
        self._data = data

    def json(self):
        if self._data is None:
            raise ValueError("no json")
        return self._data


class FakeSession:
    """Serves canned responses; `script` may hold per-endpoint lists consumed in order."""
    def __init__(self, handler):
        self.handler, self.calls, self.headers = handler, [], {}

    def get(self, url, params=None, timeout=None):
        self.calls.append((url.rsplit("/", 1)[1], params["ain"]))
        return self.handler(url.rsplit("/", 1)[1], params["ain"], len(self.calls))


def ok_handler(endpoint, ain, n):
    return Resp(data=DETAIL if endpoint == "parceldetail" else HISTORY)


def client(handler, **kw):
    return AssessorClient(FakeSession(handler), min_interval=0, backoff=0, sleep=lambda s: None, **kw)


def test_helpers():
    assert to_number("1,600") == 1600 and to_number("") is None and to_number(None) is None
    assert to_number("n/a") is None
    assert parse_date("03/26/2024") == "2024-03-26" and parse_date("") is None and parse_date("x") is None


def test_summaries_handle_missing_data():
    s = summarize_parcel(DETAIL)
    assert s["sqft_main"] == 1600 and s["tax_status"] == "DEFAULTED" and s["situs_zip"] == "93552-3937"
    assert summarize_parcel(None)["tax_status"] is None
    # most recent transfer wins even though it is last in the list; empty history is fine
    assert summarize_ownership(HISTORY)["last_transfer_date"] == "2024-03-26"
    assert summarize_ownership({"Parcel_OwnershipHistory": []})["last_transfer_doc"] is None
    assert summarize_ownership(None)["last_transfer_date"] is None


def test_retries_transient_errors_then_succeeds():
    seq = [Resp(503, text="busy"), Resp(429, text="slow"), Resp(data=DETAIL)]
    c = client(lambda e, a, n: seq[n - 1])
    assert c.parcel_detail("1") == DETAIL
    assert len(c.session.calls) == 3


def test_gives_up_after_retries():
    c = client(lambda e, a, n: Resp(500, text="boom"), retries=2)
    with pytest.raises(AssessorError, match="gave up after 3"):
        c.parcel_detail("1")


def test_timeouts_are_retried():
    def h(e, a, n):
        if n == 1:
            raise requests.Timeout("slow")
        return Resp(data=DETAIL)
    assert client(h).parcel_detail("1") == DETAIL


def test_bot_protection_page_raises_blocked_without_retry():
    c = client(lambda e, a, n: Resp(403, text="<html>Incapsula</html>", ctype="text/html"))
    with pytest.raises(BlockedError):
        c.parcel_detail("1")
    assert len(c.session.calls) == 1


def test_404_is_none_and_bad_json_is_error():
    assert client(lambda e, a, n: Resp(404, text="")).parcel_detail("1") is None
    with pytest.raises(AssessorError, match="invalid JSON"):
        client(lambda e, a, n: Resp(200, text="oops", ctype="text/plain")).parcel_detail("1")


def test_throttle_waits_between_requests():
    sleeps, t = [], [0.0]
    c = AssessorClient(FakeSession(ok_handler), min_interval=1.0, sleep=sleeps.append, clock=lambda: t[0])
    c.parcel_detail("1")
    c.parcel_detail("1")
    assert sleeps == [1.0]


def items(*ains):
    return pd.DataFrame({"auction": "T1", "ain": list(ains)})


NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


def test_snapshot_uses_cache_and_expires_it(tmp_path):
    c = client(ok_handler)
    snap = build_snapshot(items("111"), c, tmp_path, now=NOW)
    assert snap.loc[0, "last_transfer_date"] == "2024-03-26" and pd.isna(snap.loc[0, "fetch_error"])
    assert len(c.session.calls) == 2
    build_snapshot(items("111"), c, tmp_path, now=NOW + timedelta(hours=1))      # cached
    assert len(c.session.calls) == 2
    build_snapshot(items("111"), c, tmp_path, now=NOW + timedelta(hours=13))     # expired
    assert len(c.session.calls) == 4


def test_per_parcel_failures_are_recorded_not_fatal(tmp_path):
    def h(e, a, n):
        return Resp(500, text="x") if a == "bad" else ok_handler(e, a, n)
    snap = build_snapshot(items("bad", "good"), client(h, retries=0), tmp_path, now=NOW)
    assert "HTTP 500" in snap.loc[0, "fetch_error"]
    assert pd.isna(snap.loc[1, "fetch_error"]) and snap.loc[1, "tax_status"] == "DEFAULTED"


def test_unknown_parcel_is_flagged(tmp_path):
    snap = build_snapshot(items("404"), client(lambda e, a, n: Resp(404, text="")), tmp_path, now=NOW)
    assert snap.loc[0, "fetch_error"] == "parcel not found"


def test_blocked_aborts_whole_run(tmp_path):
    c = client(lambda e, a, n: Resp(403, text="<html>", ctype="text/html"))
    with pytest.raises(BlockedError):
        build_snapshot(items("1", "2"), c, tmp_path, now=NOW)


def test_status_history_records_only_changes(tmp_path):
    hp = tmp_path / "status_history.csv"
    def snap(status, when, err=None):
        return pd.DataFrame([{"auction": "T1", "ain": "111", "pulled_at": when, "tax_status": status,
                              "parcel_status": "ACTIVE", "tax_defaulted_year": "2019", "fetch_error": err}])
    assert update_status_history(hp, snap("DEFAULTED", "2026-11-01T00:00:00")) == 1
    assert update_status_history(hp, snap("DEFAULTED", "2026-11-02T00:00:00")) == 0
    assert update_status_history(hp, snap(None, "2026-11-03T00:00:00", err="HTTP 500")) == 0
    assert update_status_history(hp, snap("CURRENT", "2026-11-04T00:00:00")) == 1
    h = pd.read_csv(hp, dtype=str)
    assert list(h["tax_status"]) == ["DEFAULTED", "CURRENT"]


def test_run_enrichment_writes_snapshot_and_history(tmp_path):
    d = tmp_path / "T1"
    d.mkdir()
    pd.DataFrame({"auction": "T1", "ain": ["0123456789", "9876543210"]}).to_csv(d / "items.csv", index=False)
    out, snap, changed = run_enrichment(d, client(ok_handler), tmp_path / "cache", now=NOW)
    assert out == d / "enrichment" / "2026-11-07.csv" and changed == 2
    back = pd.read_csv(out, dtype={"ain": str})
    assert list(back["ain"]) == ["0123456789", "9876543210"]       # leading zero survives
    assert (d / "status_history.csv").exists()


def test_cli_enrich_reports_block(tmp_path, monkeypatch, capsys):
    d = tmp_path / "data" / "T1"
    d.mkdir(parents=True)
    pd.DataFrame({"auction": "T1", "ain": ["1"]}).to_csv(d / "items.csv", index=False)
    import lacounty_tax.cli as cli
    monkeypatch.setattr(cli, "AssessorClient", lambda **k: client(
        lambda e, a, n: Resp(403, text="<html>", ctype="text/html")))
    rc = main(["enrich", "T1", "--data-dir", str(tmp_path / "data"), "--cache-dir", str(tmp_path / "c")])
    assert rc == 3 and "bot" in capsys.readouterr().err
    assert main(["enrich", "NOPE", "--data-dir", str(tmp_path / "data")]) == 2
