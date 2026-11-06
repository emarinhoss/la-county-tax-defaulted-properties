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

## Roadmap

2. Assessor enrichment with caching and status history
3. Analysis: rent estimates from free HUD/Census data, yield and risk flags
4. Static dashboard (GitHub Pages)
5. Scheduled GitHub Actions workflow
