# LA County tax-defaulted property auctions

Pipeline to collect the county's tax-defaulted property auction books, enrich the
parcels with Assessor data, rank them as rent/hold candidates, and publish a
dashboard. See the phase plan below; only Phase 1 is implemented.

## Phase 1: parse the auction book (done)

```bash
pip install -e '.[dev]'
lacounty-tax parse path/to/2026B-Auction-Book.pdf --strict   # -> data/auctions/2026B/items.csv
pytest
```

Put downloaded books in `data/inbox/` (git-ignored; raw PDFs are not stored).
`lacounty-tax fetch <url>` downloads one, but the county site's bot protection may
refuse non-browser requests (it did from the cloud sandbox); in that case download
it in a browser instead.

`items.csv` columns: `auction, item, ain` (10 digits, read it as a string),
`min_bid, improved` (county IMP flag), `nsb, legal_description, location,
property_address` (as printed), `zip, zip4, vacant_lot` (address starts with `VAC`), `page`.

## Phase 2: Assessor enrichment (done, not yet run against the live API)

```bash
lacounty-tax enrich 2026B            # -> data/auctions/2026B/enrichment/<today>.csv
lacounty-tax enrich 2026B --limit 20 # try a few parcels first
```

- One snapshot per pull day (a second run the same day overwrites it), with tax status,
  situs address/ZIP, units, year built, sqft, beds/baths, roll land/improvement values and
  the most recent ownership transfer. Failed parcels get a `fetch_error` instead of stopping the run.
- `status_history.csv` gets a row only when a parcel's tax/parcel status changes. This is how
  you spot parcels redeemed (`CURRENT`) before the sale.
- Raw responses are cached in `.cache/assessor/` (git-ignored); `--cache-hours` (default 12) controls
  reuse, so an interrupted run resumes and a scheduled re-pull refreshes. Requests are throttled
  (`--delay`, default 0.5 s) and transient errors are retried with backoff.
- If the Assessor site answers with a bot-protection page, the run stops (exit code 3) and nothing
  is written; progress stays cached. The cloud sandbox this was built in is blocked, so run it from
  your own machine or check whether GitHub Actions is accepted.

## Roadmap

3. Analysis: rent estimates from free HUD/Census data, yield and risk flags
4. Static dashboard (GitHub Pages)
5. Scheduled GitHub Actions workflow
