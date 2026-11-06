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

## Phase 3: analysis (done; needs the HUD file and a live enrichment to produce real output)

```bash
# once per HUD fiscal year (HUD blocks scripts): either save the "Small Area FMRs by Unit Bedrooms" table
# from the LA County page on https://www.huduser.gov/portal/datasets/fmr.html as a text file, or download the
# nationwide SAFMR .xlsx, then:
lacounty-tax load-fmr safmr_la.txt --year 2027         # -> data/reference/hud_safmr.csv (FY2027 is committed)
lacounty-tax analyze 2026B --config config/assumptions.toml
```

Writes `data/auctions/<id>/analysis/scored.csv` and `summary.json`.

- **Segments:** `rental` (improved, has an address) is ranked; `land` (unimproved/vacant) gets bid per lot
  sqft and bid-to-assessed ratio; `review` is the few parcels flagged improved but with a `VACANT LOT` address.
- **Excluded (kept in the table with a reason):** tax status `CURRENT`, enrichment missing/failed, no
  residential units, more than 4 units, no HUD rent for the ZIP, no bedroom count.
- **Rent:** HUD SAFMR by ZIP and bedrooms. About 37% of LA ZIPs carry HUD's identical metro-wide fallback row;
  those are flagged `fmr_metro_default` and are not ZIP-specific.
  Multi-unit properties split their bedrooms evenly across units.
- **Return:** `cap_rate` at `min_bid * bid_multiple`, `max_bid_for_target` (the highest bid that still
  meets `target_cap_rate`) and `bid_headroom` (that, divided by the minimum bid). Rank = cap rate.
- **Risk columns:** `rehab_risk` (age and structure-to-land assessed value), `redemption_risk` (recent
  transfer, large owner equity, status changes) and a `checklist` reminding you about liens and unknown occupancy.
- All rates/costs are assumptions in `config/assumptions.toml`. The minimum bid is only a floor, so try
  `bid_multiple` of 1.5-3 to see how fast yield falls.

## Phase 4: static dashboard (done, shown with synthetic data)

```bash
lacounty-tax make-demo                                   # fake auction -> demo/auctions/DEMO (for trying the UI)
lacounty-tax build-site --data-dir demo/auctions --out docs
lacounty-tax build-site                                  # real data: data/auctions/*/analysis -> docs/index.html
```

`docs/index.html` is one self-contained file (data, CSS and JS inlined; no CDN), ready for GitHub Pages
(Settings -> Pages -> deploy from `docs/`). Open it straight from disk to preview.

- Tabs for **Rentals** (ranked), **Land**, and **Excluded** (with the reason breakdown); search, jurisdiction,
  type, max-bid and min-cap filters; sortable table; click a row (or a chart point) for the full return model.
- **Bid slider:** the minimum bid is only a floor, so cap rates, ranks, max bid and the "at or above target"
  count are recomputed in the browser for any expected winning bid (1x-6x the minimum), and the target
  cap rate is editable. The formula is the same as `analyze.py` (a test runs the JS under Node and compares).
- Several auctions in `data/auctions/` show up in an auction picker; synthetic ones are labelled and sorted last.
- The demo uses invented addresses/AINs and a banner; it is not real data and should not be relied on.

## Roadmap

5. Scheduled GitHub Actions workflow (and a Pages deploy)
