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

## Phase 5: automation (done; workflows have not run on GitHub yet)

Two workflows in `.github/workflows/`:

- **CI** (`ci.yml`): tests on Python 3.11 and 3.12 and a dashboard build from synthetic data, on every push.
- **Refresh and publish** (`refresh.yml`): Mondays 14:17 UTC, or run it by hand. It re-pulls Assessor data for auctions that
  are still before their sale date, re-scores them, commits `data/`, rebuilds the dashboard and deploys it to GitHub Pages.
  All logic lives in `lacounty-tax refresh` (tested); the YAML only wires it up.

### One-time setup

1. Merge this work into the default branch (scheduled workflows only run from there).
2. Settings -> Pages -> Source: **GitHub Actions**.
3. Settings -> Actions -> General -> Workflow permissions: **Read and write**.
4. Actions -> "Refresh and publish" -> Run workflow, to check that it works.

Until an auction has been scored, the published page is the synthetic demo (with its banner).

### Using it

- **New auction book:** parse it where you can download the PDF, then commit the result:
  `lacounty-tax parse book.pdf --sale-date 2026-12-05` writes `items.csv` and `meta.json`. Or run the workflow manually with
  `book_url` and `sale_date` to do the same on the runner (the county site may refuse it; see below).
  After `sale_date` the auction is frozen: scheduled runs stop re-pulling it, and its last snapshot stays.
- **HUD rents:** once a year, `lacounty-tax load-fmr` with the new table and commit `data/reference/hud_safmr.csv`.
- **Assumptions:** edit `config/assumptions.toml`; the next run re-scores with it.
- **Preview the site:** `lacounty-tax build-site --out _site` (real data) and open `_site/index.html`.

### If the county sites block GitHub's runners

Both county hosts sit behind bot protection that refused the cloud sandbox this was built in, and may refuse Actions runners too.
The refresh step then does **not** fail: it adds a warning annotation and a table in the run summary, commits nothing, and still
republishes the dashboard from the data already in the repo. In that case run it from your own machine and push:

```bash
lacounty-tax refresh          # same command CI runs
git add data && git commit -m "Refresh data" && git push
```

## Map

The dashboard's **Map** tab plots every parcel that has coordinates: rentals colored by cap-rate class (one blue hue,
recomputed with the bid slider and filters), land and excluded parcels as small grey dots, with hover tooltips and a click
through to the detail drawer. The layer checkboxes and all filters apply; the note under the map says how many matching
parcels have no coordinates.

Coordinates come from LA County's public parcel GIS service (parcel polygon centroid by AIN), so vacant lots without a street
address are mapped too:

```bash
lacounty-tax geocode 2026B     # -> data/auctions/2026B/coordinates.csv (only missing AINs are looked up)
```

The weekly refresh runs this step too (`--no-geocode` skips it) and never fails because of it. Coordinates never change, so they
are looked up once per parcel.

- **Not yet run against the live service.** The default layer URL and the `AIN` field name are best known values. If the first run
  reports errors or matches nothing, pass `--gis-url` (a parcel layer's `.../query` URL) and `--ain-field`, or set
  `LACOUNTY_GIS_URL` / `LACOUNTY_GIS_AIN_FIELD`. Parcels the layer doesn't contain (some condo units, for example) stay unmapped.
- **County outline and limits.** The map draws a dashed Los Angeles County outline (`data/reference/boundaries/la_county.geojson`) and limits
  panning and zooming to the county and its surroundings. The outline is made by dissolving every polygon of the county's own
  "City and Community Boundaries" layer (about 4,075 sq mi, following the coastline; thousands of sliver holes between neighbouring
  shapes are filled and specks dropped). That step needs shapely: `pip install -e '.[geo]'`, then add `--county-out` to `load-boundaries`.
- **City boundaries** come from the county's own "City and Community Boundaries" layer (88 incorporated cities plus unincorporated areas). The easiest way,
  with no API URL and no network access from this tool:

  ```bash
  # On LA County GeoHub open the dataset, choose Download -> GeoJSON (the CSV download has no shapes), then:
  lacounty-tax load-boundaries city_layer.geojson --county-out data/reference/boundaries/la_county.geojson
  # -> data/reference/boundaries/la_cities.geojson (88 cities) and la_county.geojson (dissolved outline)
  ```

  By default only the 88 incorporated cities are kept. `--include-unincorporated` also keeps communities and islands (dotted lines),
  but never the large "AREA" regions (Antelope Valley, Santa Clarita Valley, ...) that overlap the cities. Shapes are simplified to
  about 20 m (`--simplify`). The name field is detected by which candidate is actually filled in (`CITY_COMM_NAME` in this layer;
  override with `--name-field`). Alternatively `lacounty-tax fetch-boundaries` downloads straight from the county's ArcGIS Online layer
  (`services.arcgis.com/RmCCgQtiZLDCtblq/.../admin_dist_SDE_DIST_DRP_CITY_COMM_BDY/FeatureServer/0`, the default; override with `--url`). That host
  answered GitHub Actions runners in `la-county-elections`, unlike the county's own web servers. Commit the result and rebuild; the map then shows a "City boundaries" checkbox.
  The loader has been run on the real downloaded file (88 cities; 17 landmark checks all land in the right city; simplification error
  under 0.5% of area). `fetch-boundaries` has not been run against the live service. `tests/fixtures/la_city_layer_attributes.csv` is
  the layer's attribute table, used to test field detection.
- **Background: solid colour + elevation, no map service.** By default the map needs no external service at all: the county is a solid
  land colour on a grey surround, with a shaded-relief (hillshade) image of the terrain on top, the dashed county outline, the city lines
  and the parcels. The relief is built once from public terrain tiles and committed:

  ```bash
  lacounty-tax build-relief      # -> data/reference/relief/la_relief.png + la_relief.json (~0.8 MB, 7 s, zoom 10 = ~127 m/pixel)
  ```

  It is a transparent overlay (black shadows, white highlights, clipped to the county), so it works over the light and dark land colours.
  Elevation: Mapzen terrain tiles (NASA SRTM, USGS 3DEP and other public sources) on AWS Open Data; negative values (harbours) are
  clamped to sea level. `--zoom 9` makes a smaller, coarser image; `--z-factor` changes the vertical exaggeration. Checkboxes: "Elevation"
  turns the shading off (plain solid colour); "Streets" is off by default and switches to CARTO/OpenStreetMap street tiles; if those cannot
  load, the box quietly unchecks itself and the built-in background stays (no error banner).
- **No CDN.** Leaflet 1.9.4 is vendored (`src/lacounty_tax/templates/vendor/leaflet`, byte-identical to the official release and to the copy in
  `la-county-elections`) and copied to `vendor/leaflet/` beside `index.html` by `build-site`, so the page works fully offline unless you switch on Streets.

## Roadmap

All five phases plus the map are in place. Ideas not built: automatic discovery of new auction books, winning-bid history
(to calibrate the bid multiple), alerts, Census rent cross-checks.
