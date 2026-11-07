/* LA County tax-defaulted property auctions: dashboard logic (no dependencies). */
"use strict";

/* ---------- money model (mirrors lacounty_tax/analyze.py income_metrics) ---------- */
function capMetrics(a, rent, bid, rehab, m) {
  const paid = bid * m;
  const egi = 12 * rent * (1 - a.vacancy_rate);
  const cost = paid * (1 + a.closing_cost_pct) + rehab;
  const noi = egi * (1 - a.opex_pct) - a.property_tax_rate * paid;
  const maxBid = Math.max(
    0,
    (egi * (1 - a.opex_pct) - a.target_cap_rate * rehab) /
      (a.property_tax_rate + a.target_cap_rate * (1 + a.closing_cost_pct))
  );
  return {
    paid, egi, cost, noi,
    cap: cost ? noi / cost : null,
    grossYield: cost ? (12 * rent) / cost : null,
    maxBid,
    headroom: bid ? maxBid / bid : null,
  };
}

/* cap rate x the chance the parcel actually reaches the sale (the ranking metric when rank_by is "adjusted") */
function adjustedCap(cap, pStay) {
  return cap == null ? null : cap * (pStay == null ? 1 : pStay);
}

/* how each colour mode of the Tax roll map sorts a live parcel into a class (null = no data for this mode) */
const ROLL_MODES = {
  pull: { title: "Chance it is pulled before the sale", idx: [0, 3, 5], labels: ["under 15%", "15–50%", "50% or more"],
    of: (r) => { const x = 1 - r.p_stay; return x < 0.15 ? 0 : x < 0.5 ? 1 : 2; } },
  years: { title: "Years in default", idx: [0, 2, 4, 5], labels: ["under 5", "5–9", "10–19", "20 or more"],
    of: (r) => (r.years_in_default == null ? null : r.years_in_default < 5 ? 0 : r.years_in_default < 10 ? 1 : r.years_in_default < 20 ? 2 : 3) },
  paid: { title: "Share of the minimum bid already paid off", idx: [0, 2, 4, 5], labels: ["under 5%", "5–50%", "50–95%", "95% or more"],
    of: (r) => (r.paid_down_pct == null ? null : r.paid_down_pct < 0.05 ? 0 : r.paid_down_pct < 0.5 ? 1 : r.paid_down_pct < 0.95 ? 2 : 3) },
  offered: { title: "Times offered in earlier books", idx: [0, 2, 4, 5], labels: ["never", "once", "twice", "3 or more"],
    of: (r) => (r.offered_before == null ? null : Math.min(r.offered_before, 3)) },
};

if (typeof module !== "undefined") module.exports = { capMetrics, adjustedCap, ROLL_MODES };

/* ---------- everything below needs a browser ---------- */
if (typeof document !== "undefined") (function () {
  const DATA = JSON.parse(document.getElementById("data").textContent);
  const $ = (id) => document.getElementById(id);
  const usd = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  const num = new Intl.NumberFormat("en-US", { maximumFractionDigits: 0 });
  const fmtUsd = (v) => (v == null ? "n/a" : usd.format(v));
  const fmtNum = (v) => (v == null ? "n/a" : num.format(v));
  const fmtPct = (v, d = 1) => (v == null ? "n/a" : (v * 100).toFixed(d) + "%");
  // odds are a screening estimate: never show them as a certainty
  const fmtOdds = (v) => (v == null ? "n/a" : v > 0.99 ? ">99%" : fmtPct(v, 0));
  const fmtX = (v) => (v == null ? "n/a" : (v >= 10 ? v.toFixed(0) : v.toFixed(1)) + "×");

  const REASONS = {
    tax_current: "Taxes paid (status CURRENT)",
    enrichment_failed: "Assessor lookup failed",
    not_enriched: "Not yet enriched",
    improved_flag_without_address: "Flagged improved, no address",
    no_residential_units: "No residential units",
    multi_unit_5plus: "5+ units (outside the 2–4 unit focus)",
    no_fmr_for_zip: "No HUD rent for ZIP",
    no_bedroom_data: "No bedroom data",
  };
  const TYPES = {
    single_family: "Single family", condo: "Condo", multi_unit_2_4: "2–4 units",
    multi_unit_5plus: "5+ units", no_residential_units: "No residential units",
  };
  const RISK_GLYPH = { low: "○ Low", medium: "◐ Medium", high: "● High" };
  const RISK_NOTE = {
    rehab: "Condition proxy from year built and how little of the assessed value is the structure. Not an inspection.",
    redemption: "Heuristic: recent ownership transfer, large owner equity vs. the bid, or status changes make a pre-sale payoff more likely.",
  };

  const state = {
    ai: 0, seg: "rental", m: 1, target: 0.08,
    f: { loc: "", type: "", q: "", minCap: "", maxBid: "", noHighRehab: false, noHighRedemption: false, noFallback: false, onlyDefaulted: false, noPulled: false },
    sort: { rental: ["adjCap", -1], land: ["bidPerSqft", 1], excluded: ["min_bid", 1] },
    shown: 40, selected: null,
  };
  let auction, rows;

  /* ---------- derived data ---------- */
  function prepare() {
    auction = DATA.auctions[state.ai];
    const a = Object.assign({}, auction.assumptions, { target_cap_rate: state.target });
    rows = auction.rows.map((r) => {
      const d = Object.assign({}, r);
      d.eligible = r.segment === "rental" && !r.excluded && r.est_monthly_rent != null;
      d.met = d.eligible ? capMetrics(a, r.est_monthly_rent, r.min_bid, r.rehab_estimate, state.m) : null;
      d.cap = d.met ? d.met.cap : null;
      d.maxBid = d.met ? d.met.maxBid : null;
      d.headroom = d.met ? d.met.headroom : null;
      d.bidPerSqft = r.bid_per_lot_sqft;
      d.adjCap = adjustedCap(d.cap, r.p_stay);                                           // cap rate x odds the parcel stays in the sale
      d.rank = null;
      return d;
    });
    const rankKey = a.rank_by === "cap" ? "cap" : "adjCap";
    rows.filter((r) => r.eligible).sort((x, y) => y[rankKey] - x[rankKey]).forEach((r, i) => (r.rank = i + 1));
    return a;
  }

  const inSeg = {
    rental: (r) => r.segment === "rental" && !r.excluded,
    land: (r) => r.segment === "land" && !r.excluded,
    excluded: (r) => !!r.excluded,
    map: () => true,
    roll: () => true,
  };

  function passes(r) {
    const f = state.f;
    if (f.loc && r.location !== f.loc) return false;
    if (f.type && r.property_type !== f.type) return false;
    if (f.q) {
      const q = f.q.toLowerCase();
      const hay = [r.property_address, r.zip, r.item, r.ain, r.location].join(" ").toLowerCase();
      if (!hay.includes(q)) return false;
    }
    if (f.maxBid !== "" && r.min_bid > +f.maxBid) return false;
    if (f.onlyDefaulted && r.tax_status !== "DEFAULTED") return false;
    if (f.noPulled && r.p_stay != null && r.p_stay < 0.5) return false;                 // likely to leave the sale before it happens
    if (r.segment === "rental" && !r.excluded) {
      if (f.minCap !== "" && (r.cap == null || r.cap < +f.minCap / 100)) return false;
      if (f.noHighRehab && r.rehab_risk === "high") return false;
      if (f.noHighRedemption && r.redemption_risk === "high") return false;
      if (f.noFallback && r.fmr_metro_default) return false;
    }
    return true;
  }

  /* ---------- small DOM helper ---------- */
  function h(tag, attrs, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v == null || v === false) continue;
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else el.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat()) if (k != null) el.append(k.nodeType ? k : document.createTextNode(String(k)));
    return el;
  }

  /* ---------- scales and ticks ---------- */
  function niceTicks(min, max, count = 5) {
    const span = max - min || 1;
    const raw = span / count;
    const mag = Math.pow(10, Math.floor(Math.log10(raw)));
    const n = raw / mag;
    const step = (n < 1.5 ? 1 : n < 3 ? 2 : n < 7 ? 5 : 10) * mag;
    const out = [];
    for (let v = Math.ceil(min / step) * step; v <= max + step * 1e-9; v += step) out.push(+v.toFixed(10));
    return { ticks: out, step };
  }
  function logTicks(min, max) {
    const all = [];
    for (let k = Math.floor(Math.log10(min)); k <= Math.ceil(Math.log10(max)); k++)
      for (const m of [1, 2, 5]) {
        const v = m * Math.pow(10, k);
        if (v >= min * 0.999 && v <= max * 1.001) all.push(v);
      }
    if (all.length <= 8) return all;
    return all.filter((v) => Math.abs(Math.log10(v) - Math.round(Math.log10(v))) < 1e-9);
  }
  const compact = (v) => (v >= 1e6 ? v / 1e6 + "M" : v >= 1e3 ? v / 1e3 + "k" : String(v));

  /* ---------- charts ---------- */
  const tip = $("tip");
  function showTip(evt, lines) {
    tip.replaceChildren(...lines.map((t, i) => h(i ? "div" : "strong", null, t)));
    tip.hidden = false;
    const pad = 12, w = tip.offsetWidth, hh = tip.offsetHeight;
    let x = evt.clientX + pad, y = evt.clientY + pad;
    if (x + w > innerWidth - 8) x = evt.clientX - w - pad;
    if (y + hh > innerHeight - 8) y = evt.clientY - hh - pad;
    tip.style.transform = `translate(${Math.max(8, x)}px, ${Math.max(8, y)}px)`;
  }
  const hideTip = () => (tip.hidden = true);

  function frame(el, build) {
    const width = Math.max(260, el.clientWidth || 520), height = 250;
    const m = { l: 52, r: 14, t: 26, b: 36 };
    const iw = width - m.l - m.r, ih = height - m.t - m.b;
    const svg = build({ width, height, m, iw, ih });
    el.innerHTML = svg;
  }
  const axisText = 'class="ax"';

  function histogram(el, values, o) {
    if (!values.length) return (el.innerHTML = '<p class="empty">No data for the current filters.</p>');
    const lo0 = o.log ? Math.log10(Math.min(...values)) : Math.min(0, Math.min(...values));
    const sorted = values.slice().sort((a, b) => a - b);
    const hi0 = o.log ? Math.log10(sorted[sorted.length - 1]) : sorted[Math.min(sorted.length - 1, Math.floor(sorted.length * 0.97))];
    const tickSet = niceTicks(lo0, hi0 || lo0 + 1, 8);
    const step = tickSet.step;
    const lo = Math.floor(lo0 / step) * step;
    const nb = Math.max(1, Math.ceil((hi0 - lo) / step - 1e-9));
    const hi = lo + nb * step;
    const val = (v) => (o.log ? Math.log10(v) : v);
    const bins = Array.from({ length: nb }, () => 0);
    for (const v of values) bins[Math.min(nb - 1, Math.max(0, Math.floor((val(v) - lo) / step + 1e-9)))]++;
    const ymax = Math.max(...bins);
    const yt = niceTicks(0, ymax, 4);
    const ytop = yt.ticks[yt.ticks.length - 1] >= ymax ? yt.ticks[yt.ticks.length - 1] : yt.ticks[yt.ticks.length - 1] + yt.step;
    frame(el, ({ width, height, m, iw, ih }) => {
      const X = (v) => m.l + ((v - lo) / (hi - lo)) * iw;
      const Y = (v) => m.t + ih - (v / ytop) * ih;
      let s = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${o.label}">`;
      for (let v = 0; v <= ytop + 1e-9; v += yt.step)
        s += `<line class="grid" x1="${m.l}" x2="${width - m.r}" y1="${Y(v)}" y2="${Y(v)}"/><text ${axisText} x="${m.l - 8}" y="${Y(v) + 4}" text-anchor="end">${v}</text>`;
      const bw = iw / nb;
      bins.forEach((c, i) => {
        if (!c) return;
        const x = m.l + i * bw + 1, w = Math.max(1, bw - 2), y = Y(c), hgt = m.t + ih - y, r = Math.min(4, w / 2, hgt);
        s += `<path class="bar" data-i="${i}" d="M${x},${m.t + ih} V${y + r} Q${x},${y} ${x + r},${y} H${x + w - r} Q${x + w},${y} ${x + w},${y + r} V${m.t + ih} Z"/>`;
      });
      const xt = o.log ? tickSet.ticks : niceTicks(lo, hi, 7).ticks;
      for (const t of xt)
        if (t >= lo - 1e-9 && t <= hi + 1e-9)
          s += `<text ${axisText} x="${X(t)}" y="${height - 14}" text-anchor="middle">${o.tick(o.log ? Math.pow(10, t) : t)}</text>`;
      s += `<line class="axis" x1="${m.l}" x2="${width - m.r}" y1="${m.t + ih}" y2="${m.t + ih}"/>`;
      if (o.vline != null && o.vline >= lo && o.vline <= hi)
        s += `<line class="ref" x1="${X(o.vline)}" x2="${X(o.vline)}" y1="${m.t}" y2="${m.t + ih}"/><text class="ref-t" x="${X(o.vline) + 5}" y="${m.t + 11}">${o.vlineLabel}</text>`;
      s += `<text ${axisText} x="${width - m.r}" y="${height - 1}" text-anchor="end" class="axt">${o.xTitle}</text>`;
      s += `<text ${axisText} x="${m.l - 40}" y="12" class="axt">Parcels</text></svg>`;
      el._bins = { bins, lo, step, o };
      return s;
    });
    el.onpointermove = (e) => {
      const t = e.target.closest("[data-i]");
      if (!t) return hideTip();
      const i = +t.dataset.i, { bins, lo, step } = el._bins;
      const a = lo + i * step, b = a + step, last = i === bins.length - 1;
      const f = (v) => o.tick(o.log ? Math.pow(10, v) : v);
      showTip(e, [`${f(a)} to ${f(b)}${last ? " and above" : ""}`, `${bins[i]} parcel${bins[i] === 1 ? "" : "s"}`]);
    };
    el.onpointerleave = hideTip;
  }

  function scatter(el, pts, o) {
    if (!pts.length) return (el.innerHTML = '<p class="empty">No data for the current filters.</p>');
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y);
    const xmin = Math.min(...xs) * 0.8, xmax = Math.max(...xs) * 1.25;
    let ylo, yhi, yTicks;
    if (o.ylog) {
      ylo = Math.min(...ys) * 0.8; yhi = Math.max(...ys) * 1.25;
      yTicks = logTicks(ylo, yhi);
    } else {
      const t = niceTicks(Math.min(0, ...ys), Math.max(...ys, o.hline ?? -Infinity), 5);
      yTicks = t.ticks; ylo = Math.min(0, ...ys, t.ticks[0]); yhi = Math.max(t.ticks[t.ticks.length - 1], ...ys);
    }
    const xTicks = logTicks(xmin, xmax);
    frame(el, ({ width, height, m, iw, ih }) => {
      const lx0 = Math.log10(xmin), lx1 = Math.log10(xmax);
      const X = (v) => m.l + ((Math.log10(v) - lx0) / (lx1 - lx0)) * iw;
      const Y = o.ylog
        ? (v) => m.t + ih - ((Math.log10(v) - Math.log10(ylo)) / (Math.log10(yhi) - Math.log10(ylo))) * ih
        : (v) => m.t + ih - ((v - ylo) / (yhi - ylo)) * ih;
      let s = `<svg viewBox="0 0 ${width} ${height}" role="img" aria-label="${o.label}">`;
      for (const v of yTicks)
        s += `<line class="grid" x1="${m.l}" x2="${width - m.r}" y1="${Y(v)}" y2="${Y(v)}"/><text ${axisText} x="${m.l - 8}" y="${Y(v) + 4}" text-anchor="end">${o.ytick(v)}</text>`;
      for (const v of xTicks)
        s += `<text ${axisText} x="${X(v)}" y="${height - 14}" text-anchor="middle">${o.xtick(v)}</text>`;
      s += `<line class="axis" x1="${m.l}" x2="${width - m.r}" y1="${m.t + ih}" y2="${m.t + ih}"/>`;
      if (o.hline != null && o.hline >= ylo && o.hline <= yhi)
        s += `<line class="ref" x1="${m.l}" x2="${width - m.r}" y1="${Y(o.hline)}" y2="${Y(o.hline)}"/><text class="ref-t" x="${width - m.r - 4}" y="${Y(o.hline) - 5}" text-anchor="end">${o.hlineLabel}</text>`;
      if (o.diag) {
        const a = Math.max(xmin, ylo), b = Math.min(xmax, yhi);
        if (a < b) s += `<line class="ref" x1="${X(a)}" y1="${Y(a)}" x2="${X(b)}" y2="${Y(b)}"/><text class="ref-t" x="${X(b) - 4}" y="${Y(b) + 14}" text-anchor="end">${o.diagLabel}</text>`;
      }
      pts.forEach((p, i) => {
        const sel = state.selected === p.r.ain ? " sel" : "";
        s += `<circle class="dot${sel}" data-i="${i}" cx="${X(p.x)}" cy="${Y(p.y)}" r="${sel ? 6 : 4.5}"/>`;
      });
      s += `<text ${axisText} x="${width - m.r}" y="${height - 1}" text-anchor="end" class="axt">${o.xTitle}</text>`;
      s += `<text ${axisText} x="${m.l - 40}" y="12" class="axt">${o.yTitle}</text></svg>`;
      return s;
    });
    el.onpointermove = (e) => {
      const t = e.target.closest("[data-i]");
      if (!t) return hideTip();
      showTip(e, o.tip(pts[+t.dataset.i].r));
    };
    el.onpointerleave = hideTip;
    el.onclick = (e) => {
      const t = e.target.closest("[data-i]");
      if (t) select(pts[+t.dataset.i].r.ain);
    };
  }

  /* ---------- table ---------- */
  const risk = (v) => (v ? h("span", { class: "risk risk-" + v }, RISK_GLYPH[v]) : "n/a");
  const STATUS_NOTE = "Status is not DEFAULTED. Confirm with the Tax Collector why this parcel is on the sale list and that it is still being sold.";
  const statusCell = (r) =>
    r.tax_status && r.tax_status !== "DEFAULTED"
      ? h("span", { class: "flag", title: STATUS_NOTE }, "⚑ " + r.tax_status)
      : r.tax_status ?? "n/a";
  const addr = (r) =>
    r.property_address && r.property_address !== "VACANT LOT"
      ? r.property_address.replace(/(\d{5}) \d{4}$/, "$1")   // drop the ZIP+4 suffix
      : "Vacant lot (no situs)";
  const COLS = {
    rental: [
      ["rank", "#", (r) => r.rank ?? "–", "num"],
      ["item", "Item", (r) => r.item, "num"],
      ["property_address", "Address", (r) => addr(r), "txt wide"],
      ["property_type", "Type", (r) => TYPES[r.property_type] || "n/a", "txt"],
      ["num_beds", "Bd/Ba · sqft · built", (r) => `${fmtNum(r.num_beds)}/${fmtNum(r.num_baths)} · ${fmtNum(r.sqft_main)} · ${r.year_built ?? "n/a"}`, "txt"],
      ["min_bid", "Min bid", (r) => fmtUsd(r.min_bid), "num"],
      ["est_monthly_rent", "Rent/mo", (r) => fmtUsd(r.est_monthly_rent), "num"],
      ["cap", "Cap rate", (r) => fmtPct(r.cap), "num"],
      ["p_stay", "Odds of sale", (r) => (fmtOdds(r.p_stay)), "num"],
      ["adjCap", "Adj. cap", (r) => fmtPct(r.adjCap), "num strong"],
      ["maxBid", "Max bid", (r) => fmtUsd(r.maxBid), "num"],
      ["headroom", "Headroom", (r) => fmtX(r.headroom), "num"],
      ["years_in_default", "Yrs in default", (r) => (r.years_in_default == null ? "n/a" : r.years_in_default), "num"],
      ["paid_down_pct", "Paid down", (r) => (r.paid_down_pct == null ? "n/a" : fmtPct(r.paid_down_pct, 0)), "num"],
      ["offered_before", "Offered before", (r) => (r.offered_before == null ? "n/a" : r.offered_before + "×"), "num"],
      ["rehab_risk", "Rehab", (r) => risk(r.rehab_risk), "txt"],
      ["redemption_risk", "Redemption", (r) => risk(r.redemption_risk), "txt"],
      ["tax_status", "Tax status", statusCell, "txt"],
    ],
    land: [
      ["item", "Item", (r) => r.item, "num"],
      ["property_address", "Location", (r) => addr(r), "txt wide"],
      ["location", "Jurisdiction", (r) => r.location, "txt"],
      ["sqft_lot", "Lot sqft", (r) => fmtNum(r.sqft_lot), "num"],
      ["min_bid", "Min bid", (r) => fmtUsd(r.min_bid), "num"],
      ["bidPerSqft", "$/lot sqft", (r) => (r.bidPerSqft == null ? "n/a" : "$" + r.bidPerSqft.toFixed(2)), "num strong"],
      ["assessed_value", "Assessed", (r) => fmtUsd(r.assessed_value), "num"],
      ["bid_to_assessed", "Bid ÷ assessed", (r) => fmtPct(r.bid_to_assessed, 0), "num"],
      ["p_stay", "Odds of sale", (r) => (fmtOdds(r.p_stay)), "num"],
      ["years_in_default", "Yrs in default", (r) => (r.years_in_default == null ? "n/a" : r.years_in_default), "num"],
      ["offered_before", "Offered before", (r) => (r.offered_before == null ? "n/a" : r.offered_before + "×"), "num"],
      ["tax_status", "Tax status", statusCell, "txt"],
    ],
    excluded: [
      ["item", "Item", (r) => r.item, "num"],
      ["property_address", "Address", (r) => addr(r), "txt wide"],
      ["segment", "Segment", (r) => r.segment, "txt"],
      ["excluded", "Reason", (r) => REASONS[r.excluded] || r.excluded, "txt"],
      ["min_bid", "Min bid", (r) => fmtUsd(r.min_bid), "num"],
      ["tax_status", "Tax status", statusCell, "txt"],
    ],
  };

  function renderTable(list) {
    const cols = COLS[state.seg];
    const [sk, sd] = state.sort[state.seg];
    const sorted = list.slice().sort((a, b) => {
      const x = a[sk], y = b[sk];
      if (x == null && y == null) return 0;
      if (x == null) return 1;
      if (y == null) return -1;
      return (x > y ? 1 : x < y ? -1 : 0) * sd;
    });
    const head = h("tr", null, ...cols.map(([k, label, , cls]) =>
      h("th", { class: cls.split(" ")[0], "aria-sort": k === sk ? (sd > 0 ? "ascending" : "descending") : "none" },
        h("button", { type: "button", onclick: () => {
          state.sort[state.seg] = [k, k === sk ? -sd : (["cap", "adjCap", "p_stay", "headroom", "maxBid", "years_in_default", "paid_down_pct", "offered_before"].includes(k) ? -1 : 1)];
          render();
        } }, label, k === sk ? (sd > 0 ? " ▲" : " ▼") : ""))));
    const body = sorted.slice(0, state.shown).map((r) =>
      h("tr", { tabindex: 0, class: state.selected === r.ain ? "sel" : "", "data-ain": r.ain,
        onclick: () => select(r.ain), onkeydown: (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); select(r.ain); } } },
        ...cols.map(([, , f, cls]) => h("td", { class: cls }, f(r)))));
    $("thead").replaceChildren(head);
    $("tbody").replaceChildren(...body);
    $("count").textContent = `${fmtNum(list.length)} parcel${list.length === 1 ? "" : "s"} match`;
    const more = $("more");
    more.hidden = sorted.length <= state.shown;
    more.textContent = `Show more (${Math.min(40, sorted.length - state.shown)} of ${sorted.length - state.shown} remaining)`;
  }

  /* ---------- tax roll section of the drawer ---------- */
  function rollSection(r) {
    if (r.p_stay == null) return [];
    const kv = (k, v) => [h("dt", null, k), h("dd", null, v)];
    const f = [];
    if (r.p_stay === 0) {
      f.push(...kv("Status", r.redemption_date ? "Redeemed " + r.redemption_date : "Redeemed or removed from the roll"));
    } else {
      f.push(...kv("Roll status", (r.roll_status || "n/a").toUpperCase()),
        ...kv("In default since", r.years_in_default == null ? "n/a" : `${(auction.snapshot || "").slice(0, 4) - r.years_in_default || "?"} (${r.years_in_default} yrs)`),
        ...kv("Owed today vs. minimum bid", r.redemption_amount_now == null ? "n/a" : `${fmtUsd(r.redemption_amount_now)} vs. ${fmtUsd(r.min_bid)}`),
        ...kv("Already paid off", r.paid_down_pct == null ? "n/a" : fmtPct(r.paid_down_pct, 0)),
        ...kv("Unpaid assessment years", r.unpaid_years == null ? "n/a" : `${fmtNum(r.unpaid_years)}${r.oldest_unpaid_year ? " (oldest " + r.oldest_unpaid_year + ")" : ""}`));
    }
    f.push(...kv("Offered in earlier books", r.offered_before == null ? "n/a" : r.offered_before ? `${r.offered_before}× (${r.offered_sales})` : "never"),
      ...kv("Roll sale reference", r.sale_ref ? r.sale_ref + (r.deeded_year ? ` · deeded ${r.deeded_year}` : "") : "none"),
      ...kv("Odds it reaches the sale", fmtOdds(r.p_stay)));
    return [h("h3", null, "Tax roll"), h("dl", { class: "kv" }, ...f),
      h("p", { class: "risks" }, h("small", null, "Odds combine the share of parcels like this one (same years in default) already redeemed on the roll with an assumed 75% chance that an account which has paid off half its balance is pulled. A screening estimate, not a forecast."))];
  }

  /* ---------- detail drawer ---------- */
  function select(ain) {
    state.selected = ain;
    const r = rows.find((x) => x.ain === ain);
    const d = $("drawer");
    if (!r) { d.hidden = true; return render(); }
    const a = Object.assign({}, auction.assumptions, { target_cap_rate: state.target });
    const kv = (k, v) => [h("dt", null, k), h("dd", null, v)];
    const money = [];
    const rollInfo = rollSection(r);
    if (r.met) {
      const m = r.met, opex = m.egi * a.opex_pct, tax = a.property_tax_rate * m.paid;
      money.push(h("h3", null, "Return model"),
        h("dl", { class: "kv" },
          ...kv("Est. rent / month", fmtUsd(r.est_monthly_rent) + (r.fmr_metro_default ? " (HUD metro-wide fallback)" : "")),
          ...kv("Collected rent / yr", `${fmtUsd(m.egi)} (after ${fmtPct(a.vacancy_rate, 0)} vacancy)`),
          ...kv("Operating costs", `−${fmtUsd(opex)} (${fmtPct(a.opex_pct, 0)})`),
          ...kv("Property tax", `−${fmtUsd(tax)} (${fmtPct(a.property_tax_rate, 2)} of price)`),
          ...kv("Net operating income", fmtUsd(m.noi)),
          ...kv(`Price paid (${fmtX(state.m)} min bid)`, fmtUsd(m.paid)),
          ...kv("Closing + rehab", `${fmtUsd(m.paid * a.closing_cost_pct)} + ${fmtUsd(r.rehab_estimate)}`),
          ...kv("All-in cost", fmtUsd(m.cost)),
          ...kv("Cap rate", fmtPct(m.cap)),
          ...kv(`Max bid for ${fmtPct(state.target, 1)}`, `${fmtUsd(m.maxBid)} (${fmtX(m.headroom)} the minimum)`)));
    }
    const facts = [
      ...kv("AIN", r.ain), ...kv("Item", r.item), ...kv("Jurisdiction", r.location),
      ...kv("ZIP", r.zip ?? "n/a"), ...kv("Minimum bid", fmtUsd(r.min_bid)),
      ...kv("Tax status", r.tax_status ?? "n/a"),
      ...kv("Units / beds / baths", `${fmtNum(r.num_units)} / ${fmtNum(r.num_beds)} / ${fmtNum(r.num_baths)}`),
      ...kv("Living sqft / lot sqft", `${fmtNum(r.sqft_main)} / ${fmtNum(r.sqft_lot)}`),
      ...kv("Year built", r.year_built ?? "n/a"),
      ...kv("Assessed land + improvements", `${fmtUsd(r.land_value)} + ${fmtUsd(r.imp_value)}`),
    ];
    d.replaceChildren(...[
      h("div", { class: "drawer-head" },
        h("h2", null, addr(r)),
        h("button", { type: "button", class: "icon", "aria-label": "Close details", onclick: () => select(null) }, "✕")),
      r.excluded ? h("p", { class: "note" }, "Excluded: " + (REASONS[r.excluded] || r.excluded)) : null,
      r.tax_status && !["DEFAULTED", "CURRENT"].includes(r.tax_status) ? h("p", { class: "note" }, "⚑ Tax status " + r.tax_status + ". " + STATUS_NOTE) : null,
      ...money,
      ...rollInfo,
      r.segment === "rental" ? h("p", { class: "risks" },
        h("span", null, "Rehab risk: ", risk(r.rehab_risk)), " ", h("small", null, RISK_NOTE.rehab), h("br"),
        h("span", null, "Redemption risk: ", risk(r.redemption_risk)), " ", h("small", null, RISK_NOTE.redemption)) : null,
      h("h3", null, "Parcel"), h("dl", { class: "kv" }, ...facts),
      h("h3", null, "Before bidding"), h("p", null, r.checklist),
      r.lat != null && !auction.synthetic ? h("p", null, h("a", { href: `https://www.google.com/maps/search/?api=1&query=${r.lat},${r.lon}`, target: "_blank", rel: "noopener" }, "View on Google Maps ↗")) : null,
      auction.synthetic ? h("p", { class: "note" }, "Synthetic parcel: no Assessor page.")
        : h("p", null, h("a", { href: "https://portal.assessor.lacounty.gov/parceldetail/" + encodeURIComponent(r.ain), target: "_blank", rel: "noopener" }, "Open on the Assessor portal ↗")),
    ].filter(Boolean));
    d.hidden = false;
    d.querySelector("button").focus();
    render();
  }

  /* ---------- map views: the cap-rate map (Map tab) and the tax-roll map (Tax roll tab) ---------- */
  const TILES = {
    light: "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png",
    dark: "https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png",
    fallback: "https://tile.openstreetmap.org/{z}/{x}/{y}.png",
  };
  const isDark = () => {
    const t = document.documentElement.dataset.theme;
    return t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  };
  // one hue, light to dark on the light basemap; reversed on the dark one so "more" is always more prominent
  const RAMP_LIGHT = ["#86b6ef", "#5598e7", "#3987e5", "#256abf", "#184f95", "#0d366b"];
  const RAMP_DARK = ["#184f95", "#256abf", "#3987e5", "#5598e7", "#86b6ef", "#b7d3f6"];
  const kindOf = (r) => (r.excluded ? "excluded" : r.segment === "rental" ? "rental" : "land");

  function capClasses(caps) {
    const sorted = caps.slice().sort((a, b) => a - b);
    const n = Math.min(6, Math.max(1, sorted.length));
    const breaks = [];                                   // lower bound of classes 1..n-1
    for (let i = 1; i < n; i++) breaks.push(sorted[Math.floor((i * sorted.length) / n)]);
    return { n, breaks, min: sorted[0], max: sorted[sorted.length - 1] };
  }
  const classOf = (cap, c) => c.breaks.filter((b) => cap >= b).length;

  const cityStyle = () => (f) => ({
    color: isDark() ? "#a09f95" : "#6f6e69", fill: false,
    ...(f && f.properties && f.properties.kind === "unincorporated" ? { weight: 1, opacity: 0.5, dashArray: "2 4" } : { weight: 1, opacity: 0.8 }),
  });
  // Background = a solid land colour (the county shape) with an optional elevation-shading image on top of it.
  // Separate panes keep the order: land colour (250) < relief image (300) < outlines, city lines and parcels (400+).
  const landStyle = () => ({ stroke: false, fill: true, fillColor: isDark() ? "#2b2b29" : "#fbfbf9", fillOpacity: 1 });
  const countyOutline = () => ({ color: isDark() ? "#c3c2b7" : "#52514e", weight: 2, dashArray: "7 5", opacity: 0.9, fill: false });

  // One independent Leaflet map per tab: same background, own markers/legend. cfg = { ids, draw, onChange }.
  function createMapView(cfg) {
    const el = (k) => $(cfg.ids[k]);
    let map = null, markerLayer = null, tileLayer = null, tilesDark = null, lastFitKey = null;
    let countyLayer = null, landLayer = null, reliefLayer = null, citiesLayer = null;
    let tilesOk = 0, tilesBad = 0, usedFallback = false, tilesDead = false;
    const streetsOn = () => el("streets").checked && !tilesDead;
    const toggleLayer = (layer, on) => {
      if (!layer) return;
      if (on && !map.hasLayer(layer)) layer.addTo(map);
      if (!on && map.hasLayer(layer)) map.removeLayer(layer);
    };

    // Optional street tiles (off by default). If they cannot load we quietly go back to the built-in background.
    function addTiles(url, attribution, subdomains) {
      if (tileLayer) map.removeLayer(tileLayer);
      tilesOk = tilesBad = 0;
      tileLayer = L.tileLayer(url, { subdomains: subdomains || "abc", maxZoom: 19, attribution }).addTo(map);
      tileLayer.on("tileload", () => { tilesOk++; });
      tileLayer.on("tileerror", () => {
        tilesBad++;
        if (tilesOk || tilesBad < 4) return;             // some tiles loaded, or too early to say
        if (!usedFallback) {                             // first provider is failing: try plain OpenStreetMap
          usedFallback = true;
          addTiles(TILES.fallback, "© OpenStreetMap contributors");
          return;
        }
        tilesDead = true;                                // give up silently: the land colour and relief are the background
        el("streets").checked = false;
        el("streetsNote").textContent = "(unavailable)";
        applyBase();
      });
    }

    function addCities() {
      const cities = DATA.boundaries && DATA.boundaries.cities;
      if (!cities) return;
      citiesLayer = L.geoJSON(cities, { interactive: false, style: cityStyle() });
      el("citiesWrap").hidden = false;
      el("box").dataset.cities = String(cities.features.length);
    }

    function addCounty() {
      const county = DATA.boundaries && DATA.boundaries.county;
      if (!county) return;
      for (const [name, z] of [["land", 250], ["relief", 300]]) {
        const pane = map.createPane(name);
        pane.style.zIndex = z;
        pane.style.pointerEvents = "none";
      }
      landLayer = L.geoJSON(county, { pane: "land", renderer: L.svg({ pane: "land" }), interactive: false, style: landStyle() });
      countyLayer = L.geoJSON(county, { interactive: false, style: countyOutline() }).addTo(map);
      const R = DATA.relief;
      if (R) {
        reliefLayer = L.imageOverlay(R.url, R.bounds, { pane: "relief", interactive: false, attribution: R.attribution });
        el("reliefWrap").hidden = false;
      }
      const b = countyLayer.getBounds();
      map.setMaxBounds(b.pad(0.35));                     // limit panning to the county and its surroundings
      map.setMinZoom(Math.max(7, map.getBoundsZoom(b.pad(0.35))));
      map.fitBounds(b);
      el("box").dataset.county = "1";
      el("box").dataset.maxbounds = b.pad(0.35).toBBoxString();     // exposed for tests and debugging
      el("box").dataset.minzoom = map.getMinZoom();
    }

    function applyBase() {
      const streets = streetsOn();
      if (streets) {
        if (!tileLayer) usedFallback ? addTiles(TILES.fallback, "© OpenStreetMap contributors")
          : addTiles(isDark() ? TILES.dark : TILES.light, "© OpenStreetMap contributors © CARTO", "abcd");
      } else if (tileLayer) {
        map.removeLayer(tileLayer);
        tileLayer = null;
      }
      const relief = !streets && !!reliefLayer && el("relief").checked;
      toggleLayer(landLayer, !streets);                  // street tiles already draw the land, so no colour under them
      toggleLayer(reliefLayer, relief);
      if (reliefLayer) reliefLayer.setOpacity(isDark() ? 0.8 : 1);
      if (landLayer) landLayer.setStyle(landStyle());
      if (countyLayer) countyLayer.setStyle(countyOutline());
      el("box").dataset.base = streets ? "streets" : relief ? "relief" : "solid";       // for tests and debugging
    }

    el("streets").addEventListener("change", (e) => {
      if (e.target.checked) { tilesDead = false; usedFallback = false; el("streetsNote").textContent = ""; }
    });
    ["streets", "relief", "cities"].forEach((k) => el(k).addEventListener("change", cfg.onChange));

    function render(list) {
      const box = el("box"), note = el("note"), legend = el("legend");
      if (typeof L === "undefined") {
        box.replaceChildren(h("p", { class: "empty" }, "The map library could not load. The other tabs still work."));
        legend.replaceChildren(); note.textContent = "";
        return;
      }
      if (!map) {
        box.replaceChildren();
        map = L.map(box, { preferCanvas: true }).setView([34.05, -118.25], 9);
        addCounty();                                      // county shape first (it can be filled), then city lines, then markers
        addCities();
        if (citiesLayer && el("cities").checked) citiesLayer.addTo(map);
        markerLayer = L.layerGroup().addTo(map);
        box.addEventListener("keydown", (e) => { if (e.key === "Escape") map.closePopup(); });
      }
      map.invalidateSize();
      const dark = isDark();
      if (tilesDark !== dark) {                          // theme changed: new tiles (and shape colors)
        if (tileLayer) { map.removeLayer(tileLayer); tileLayer = null; }
        tilesDark = dark;
        if (citiesLayer) citiesLayer.setStyle(cityStyle());
      }
      applyBase();
      markerLayer.clearLayers();
      if (citiesLayer) toggleLayer(citiesLayer, el("cities").checked);

      const d = cfg.draw(list, { dark, ramp: dark ? RAMP_DARK : RAMP_LIGHT, grey: dark ? "#a09f95" : "#6f6e69", ringColor: dark ? "#1a1a19" : "#fcfcfb",
                                  strong: dark ? "#f0efec" : "#0b0b0b" });
      d.points.forEach(({ r, style, tip }) => {
        const m = L.circleMarker([r.lat, r.lon], style).addTo(markerLayer);
        m.bindTooltip(tip, { sticky: true });
        m.on("click", () => select(r.ain));
      });
      legend.replaceChildren(...d.legend);
      const unmapped = d.shown.length - d.points.length;
      box.dataset.markers = d.points.length;
      if (!rows.some((r) => r.lat != null)) {
        note.textContent = "No coordinates yet. Run `lacounty-tax geocode " + auction.id + "` (the weekly refresh also does this) and rebuild.";
      } else {
        note.textContent = `${fmtNum(d.points.length)} parcel${d.points.length === 1 ? "" : "s"} on the map` +
          (unmapped ? `; ${fmtNum(unmapped)} matching parcel${unmapped === 1 ? " has" : "s have"} no coordinates (not found in the county GIS layer).` : ".");
      }
      if (d.fitKey !== lastFitKey && !d.points.length && countyLayer) lastFitKey = d.fitKey;       // nothing to frame: keep the county view
      if (d.fitKey !== lastFitKey && d.points.length) {
        map.fitBounds(L.latLngBounds(d.points.map(({ r }) => [r.lat, r.lon])).pad(0.1), { maxZoom: 15 });
        lastFitKey = d.fitKey;
      }
    }
    return { render };
  }

  const swatch = (c, size = 14, ring = false) => h("span", { class: "sw",
    style: ring ? `background:transparent;border:2px solid ${c};width:${size}px;height:${size}px` : `background:${c};width:${size}px;height:${size}px` });

  // --- Map tab: rentals coloured by cap-rate class, land and excluded parcels as small grey dots
  function drawCap(list, ctx) {
    const on = { rental: $("lyRental").checked, land: $("lyLand").checked, excluded: $("lyExcl").checked };
    const shown = list.filter((r) => on[kindOf(r)]);
    const mapped = shown.filter((r) => r.lat != null && r.lon != null);
    const { ramp, grey, ringColor, strong } = ctx;
    const rentals = mapped.filter((r) => kindOf(r) === "rental" && r.cap != null);
    const cls = capClasses(rentals.map((r) => r.cap));
    const color = (r) => ramp[Math.min(ramp.length - 1, Math.round((classOf(r.cap, cls) * (ramp.length - 1)) / Math.max(1, cls.n - 1)))];
    const order = { excluded: 0, land: 1, rental: 2 };            // rentals drawn last, on top
    const points = mapped.sort((a, b) => order[kindOf(a)] - order[kindOf(b)]).map((r) => {
      const k = kindOf(r), sel = state.selected === r.ain;
      const style = k === "rental" && r.cap != null
        ? { radius: sel ? 10 : 7, fillColor: color(r), fillOpacity: 0.92, color: sel ? strong : ringColor, weight: sel ? 3 : 1.5 }
        : k === "land" ? { radius: sel ? 8 : 4, fillColor: grey, fillOpacity: 0.55, color: sel ? ringColor : grey, weight: sel ? 2 : 0 }
        : { radius: sel ? 8 : 3, fillColor: grey, fillOpacity: 0.3, color: sel ? ringColor : grey, weight: sel ? 2 : 0 };
      const tip = () => h("div", null,
        h("strong", null, addr(r)),
        h("div", null, k === "rental" && r.cap != null ? `Min bid ${fmtUsd(r.min_bid)} · cap rate ${fmtPct(r.cap)} · odds of sale ${fmtOdds(r.p_stay)} (#${r.rank})`
          : `Min bid ${fmtUsd(r.min_bid)} · ${k === "excluded" ? REASONS[r.excluded] || "excluded" : "land"}`),
        r.tax_status && r.tax_status !== "DEFAULTED" ? h("div", null, "⚑ " + r.tax_status) : null);
      return { r, style, tip };
    });
    const items = [];
    if (rentals.length) {
      items.push(h("strong", null, `Rental cap rate at ${fmtX(state.m)} the minimum bid:`));
      for (let i = 0; i < cls.n; i++) {
        const lo = i === 0 ? cls.min : cls.breaks[i - 1], hi = i === cls.n - 1 ? cls.max : cls.breaks[i];
        const c = ramp[Math.min(ramp.length - 1, Math.round((i * (ramp.length - 1)) / Math.max(1, cls.n - 1)))];
        items.push(h("span", null, swatch(c), cls.n === 1 ? fmtPct(lo) : `${fmtPct(lo, 0)}–${fmtPct(hi, 0)}`));
      }
    }
    items.push(h("span", null, swatch(grey, 9), "land"), h("span", null, swatch(grey, 6), "excluded"));
    return { shown, points, legend: items, fitKey: JSON.stringify([state.f, on, state.ai]) };
  }

  // --- Tax roll tab: every parcel, coloured by one of the roll's signals
  function drawRoll(list, ctx) {
    const mode = ROLL_MODES[$("rollMode").value] || ROLL_MODES.pull;
    const mapped = list.filter((r) => r.lat != null && r.lon != null);
    const { ramp, grey, ringColor, strong } = ctx;
    const counts = mode.labels.map(() => 0);
    let gone = 0, nodata = 0;
    const tip = (r) => () => h("div", null,
      h("strong", null, addr(r)),
      h("div", null, `Min bid ${fmtUsd(r.min_bid)}` + (r.p_stay == null ? "" : ` · odds of sale ${fmtOdds(r.p_stay)}`)),
      r.p_stay == null ? h("div", null, "No tax-roll data")
        : r.p_stay === 0 ? h("div", null, r.redemption_date ? "Redeemed " + r.redemption_date : "Redeemed or removed from the roll")
        : h("div", null, `${r.years_in_default ?? "?"} yrs in default · ${r.paid_down_pct == null ? "?" : fmtPct(r.paid_down_pct, 0)} of the bid paid · offered ${r.offered_before ?? 0}× before`),
      r.roll_status === "delinquent" ? h("div", null, "⚑ DELINQUENT") : null);
    const points = mapped.map((r) => {
      const sel = state.selected === r.ain;
      let style, layer = 1;
      const c = r.p_stay != null && r.p_stay > 0 ? mode.of(r) : null;
      if (r.p_stay === 0) {                                                  // redeemed or off the roll: a hollow ring
        gone++; layer = 0;
        style = { radius: sel ? 8 : 4, fillColor: "transparent", fillOpacity: 0, color: sel ? strong : grey, weight: sel ? 3 : 1.5, opacity: 0.85 };
      } else if (c == null) {                                                // no roll data for this parcel
        nodata++; layer = 0;
        style = { radius: sel ? 8 : 3, fillColor: grey, fillOpacity: 0.25, color: sel ? ringColor : grey, weight: sel ? 2 : 0 };
      } else {
        counts[c]++; layer = 2;
        style = { radius: sel ? 10 : 6, fillColor: ramp[mode.idx[c]], fillOpacity: 0.92, color: sel ? strong : ringColor, weight: sel ? 3 : 1.5 };
      }
      return { r, style, tip: tip(r), layer };
    }).sort((a, b) => a.layer - b.layer);
    const items = [h("strong", null, mode.title + ":")];
    mode.labels.forEach((lab, i) => items.push(h("span", null, swatch(ramp[mode.idx[i]]), `${lab} (${fmtNum(counts[i])})`)));
    if (gone) items.push(h("span", null, swatch(grey, 11, true), `redeemed or off the roll (${fmtNum(gone)})`));
    if (nodata) items.push(h("span", null, swatch(grey, 9), `no roll data (${fmtNum(nodata)})`));
    return { shown: list, points, legend: items, fitKey: JSON.stringify([state.f, state.ai]) };
  }

  function renderRollFacts() {
    const box = $("rollFacts");
    const withRoll = rows.filter((r) => r.p_stay != null);
    if (!withRoll.length) {
      box.replaceChildren(h("p", { class: "note" }, "No tax-roll data yet. Run `lacounty-tax pull-roll " + auction.id + "` (the weekly refresh does this too) and rebuild."));
      return;
    }
    const live = withRoll.filter((r) => r.p_stay > 0), yrs = live.map((r) => r.years_in_default).filter((v) => v != null).sort((x, y) => x - y);
    const med = yrs.length ? (yrs[(yrs.length - 1) >> 1] + yrs[yrs.length >> 1]) / 2 : null;
    const facts = [
      [fmtNum(live.length), "live accounts"],
      [fmtNum(withRoll.length - live.length), "redeemed or removed"],
      [med == null ? "n/a" : med + " yrs", "median time in default"],
      [fmtNum(live.filter((r) => r.paid_down_pct != null && r.paid_down_pct >= 0.5).length), "paid down 50%+"],
      [fmtNum(live.filter((r) => r.roll_status === "delinquent").length), "delinquent"],
      [fmtNum(rows.filter((r) => r.offered_before > 0).length), "offered before"],
    ];
    box.replaceChildren(...facts.map(([v, k]) => h("div", { class: "fact" }, h("strong", null, v), h("span", null, k))));
  }

  const capView = createMapView({
    ids: { box: "map", note: "mapNote", legend: "legend", streets: "lyStreets", streetsNote: "streetsNote", relief: "lyRelief", reliefWrap: "lyReliefWrap",
           cities: "lyCities", citiesWrap: "lyCitiesWrap" },
    draw: drawCap, onChange: () => render(),
  });
  const rollView = createMapView({
    ids: { box: "rollMap", note: "rollNote", legend: "rollLegend", streets: "rollStreets", streetsNote: "rollStreetsNote", relief: "rollRelief",
           reliefWrap: "rollReliefWrap", cities: "rollCities", citiesWrap: "rollCitiesWrap" },
    draw: drawRoll, onChange: () => render(),
  });

  /* ---------- page ---------- */
  function renderKpis(a) {
    const elig = rows.filter((r) => r.eligible);
    const caps = elig.map((r) => r.cap).sort((x, y) => x - y);
    const med = caps.length ? (caps[(caps.length - 1) >> 1] + caps[caps.length >> 1]) / 2 : null;
    const above = elig.filter((r) => r.cap >= state.target).length;
    const excl = rows.filter((r) => r.excluded).length;
    const flagged = elig.filter((r) => r.tax_status !== "DEFAULTED").length;
    const pulled = elig.filter((r) => r.p_stay != null && r.p_stay < 0.5).length;
    const tiles = [
      ["Parcels in auction", fmtNum(rows.length), `${rows.filter((r) => r.segment === "rental").length} improved · ${rows.filter((r) => r.segment === "land").length} land`],
      ["Rentals ranked", fmtNum(elig.length), "ranked by cap rate × odds of sale" + (pulled ? ` · ${pulled} likely pulled` : "") + (flagged ? ` · ${flagged} not DEFAULTED ⚑` : "")],
      [`At or above ${fmtPct(state.target, 1)} cap`, fmtNum(above), `when paying ${fmtX(state.m)} the minimum bid`],
      ["Median cap rate", fmtPct(med), "ranked rentals"],
      ["Excluded", fmtNum(excl), "taxes paid, no data, out of scope"],
    ];
    $("kpis").replaceChildren(...tiles.map(([k, v, s]) => h("div", { class: "kpi" }, h("div", { class: "k" }, k), h("div", { class: "v" }, v), h("div", { class: "s" }, s))));
  }

  function renderCharts(list) {
    const c1 = $("chart1"), c2 = $("chart2");
    c2.parentElement.hidden = state.seg === "excluded";
    if (state.seg === "rental") {
      $("t1").textContent = "Cap rate distribution";
      $("t2").textContent = "Minimum bid vs. cap rate";
      histogram(c1, list.map((r) => r.cap), {
        label: "Histogram of estimated cap rates for the filtered rentals", xTitle: "Cap rate", tick: (v) => fmtPct(v, 0),
        vline: state.target, vlineLabel: "target " + fmtPct(state.target, 0),
      });
      scatter(c2, list.map((r) => ({ x: r.min_bid, y: r.cap, r })), {
        label: "Scatter of minimum bid against estimated cap rate", xTitle: "Minimum bid (log scale)", yTitle: "Cap rate",
        xtick: (v) => "$" + compact(v), ytick: (v) => fmtPct(v, 0), hline: state.target, hlineLabel: "target " + fmtPct(state.target, 0),
        tip: (r) => [addr(r), `Min bid ${fmtUsd(r.min_bid)} · rent ${fmtUsd(r.est_monthly_rent)}/mo`, `Cap rate ${fmtPct(r.cap)} · rank #${r.rank}`],
      });
    } else if (state.seg === "land") {
      $("t1").textContent = "Minimum bid per lot square foot";
      $("t2").textContent = "Minimum bid vs. assessed value";
      histogram(c1, list.filter((r) => r.bidPerSqft > 0).map((r) => r.bidPerSqft), {
        label: "Histogram of minimum bid per lot square foot", xTitle: "$ per lot sqft (log scale)", log: true, tick: (v) => "$" + (v < 10 ? v.toFixed(2) : v.toFixed(0)),
      });
      scatter(c2, list.filter((r) => r.assessed_value > 0).map((r) => ({ x: r.assessed_value, y: r.min_bid, r })), {
        label: "Scatter of minimum bid against assessed value", xTitle: "Assessed value (log scale)", yTitle: "Minimum bid (log scale)", ylog: true,
        xtick: (v) => "$" + compact(v), ytick: (v) => "$" + compact(v), diag: true, diagLabel: "bid = assessed",
        tip: (r) => [addr(r), `Min bid ${fmtUsd(r.min_bid)} · assessed ${fmtUsd(r.assessed_value)}`, `${fmtNum(r.sqft_lot)} lot sqft`],
      });
    } else {
      $("t1").textContent = "Why parcels are excluded";
      $("t2").textContent = "";
      const counts = {};
      list.forEach((r) => (counts[r.excluded] = (counts[r.excluded] || 0) + 1));
      const entries = Object.entries(counts).sort((x, y) => y[1] - x[1]), mx = Math.max(1, ...entries.map((e) => e[1]));
      c1.replaceChildren(h("ul", { class: "hbars" }, ...entries.map(([k, v]) =>
        h("li", null, h("span", { class: "lbl" }, REASONS[k] || k), h("span", { class: "track" }, h("span", { class: "fill", style: `width:${(v / mx) * 100}%` })), h("span", { class: "n" }, v)))));
      c2.replaceChildren();
    }
  }

  function renderControls() {
    const segCounts = { rental: rows.filter(inSeg.rental).length, land: rows.filter(inSeg.land).length, excluded: rows.filter(inSeg.excluded).length,
      map: rows.filter((r) => r.lat != null).length, roll: rows.filter((r) => r.p_stay != null && r.p_stay > 0).length };
    document.querySelectorAll("[data-seg]").forEach((b) => {
      const s = b.dataset.seg;
      b.setAttribute("aria-selected", s === state.seg);
      b.querySelector(".c").textContent = segCounts[s];
    });
    const rentalish = state.seg === "rental" || state.seg === "map";
    $("rentalOnly").hidden = !rentalish;
    $("typeWrap").hidden = !rentalish;
  }

  function render() {
    const a = prepare();
    const list = rows.filter(inSeg[state.seg]).filter(passes);
    renderControls();
    renderKpis(a);
    const isMap = state.seg === "map", isRoll = state.seg === "roll";
    $("mapCard").hidden = !isMap;
    $("rollCard").hidden = !isRoll;
    $("chartsSec").hidden = isMap || isRoll;
    $("tableSec").hidden = isMap || isRoll;
    if (isMap) capView.render(list);
    else if (isRoll) {
      renderRollFacts();
      rollView.render(list);
    } else {
      renderCharts(list);
      renderTable(list);
    }
    $("mval").textContent = fmtX(state.m);
  }

  function boot() {
    auction = DATA.auctions[state.ai];
    state.m = auction.assumptions.bid_multiple;
    state.target = auction.assumptions.target_cap_rate;
    $("m").value = state.m;
    $("target").value = (state.target * 100).toFixed(1);
    $("banner").hidden = !auction.synthetic;
    $("banner-text").textContent = auction.label || "Synthetic demo data";
    $("auctionName").textContent = `${auction.id} · data pulled ${auction.snapshot}`;
    const sel = $("auction");
    sel.replaceChildren(...DATA.auctions.map((x, i) => h("option", { value: i }, x.id + (x.synthetic ? " (synthetic demo)" : ""))));
    sel.hidden = DATA.auctions.length < 2;
    sel.value = state.ai;
    const locs = [...new Set(auction.rows.map((r) => r.location))].sort();
    $("loc").replaceChildren(h("option", { value: "" }, "All jurisdictions"), ...locs.map((l) => h("option", { value: l }, l)));
    const types = [...new Set(auction.rows.map((r) => r.property_type).filter(Boolean))].sort();
    $("type").replaceChildren(h("option", { value: "" }, "All types"), ...types.map((t) => h("option", { value: t }, TYPES[t] || t)));
    const A = auction.assumptions;
    $("assump").textContent = `Assumptions: ${fmtPct(A.vacancy_rate, 0)} vacancy, ${fmtPct(A.opex_pct, 0)} operating costs, ${fmtPct(A.property_tax_rate, 2)} property tax on price, ${fmtPct(A.closing_cost_pct, 0)} closing, rehab ${fmtUsd(A.rehab_per_sqft_new)}/${fmtUsd(A.rehab_per_sqft_mid)}/${fmtUsd(A.rehab_per_sqft_old)} per sqft by age (min ${fmtUsd(A.rehab_min)}). Edit config/assumptions.toml and re-run to change them.`;
    render();
  }

  /* ---------- wiring ---------- */
  document.querySelectorAll("[data-seg]").forEach((b) => b.addEventListener("click", () => { state.seg = b.dataset.seg; state.shown = 40; render(); }));
  $("auction").addEventListener("change", (e) => { state.ai = +e.target.value; state.selected = null; $("drawer").hidden = true; boot(); });
  $("m").addEventListener("input", (e) => { state.m = +e.target.value; render(); });
  $("target").addEventListener("input", (e) => { const v = parseFloat(e.target.value); if (v > 0) { state.target = v / 100; render(); } });
  const bind = (id, key, ev = "input", get = (e) => e.target.value) => $(id).addEventListener(ev, (e) => { state.f[key] = get(e); state.shown = 40; render(); });
  bind("loc", "loc"); bind("type", "type"); bind("q", "q"); bind("minCap", "minCap"); bind("maxBid", "maxBid");
  bind("noHighRehab", "noHighRehab", "change", (e) => e.target.checked);
  bind("noHighRedemption", "noHighRedemption", "change", (e) => e.target.checked);
  bind("noFallback", "noFallback", "change", (e) => e.target.checked);
  bind("onlyDefaulted", "onlyDefaulted", "change", (e) => e.target.checked);
  bind("noPulled", "noPulled", "change", (e) => e.target.checked);
  ["lyRental", "lyLand", "lyExcl", "rollMode"].forEach((id) => $(id).addEventListener("change", render));
  $("reset").addEventListener("click", () => {
    Object.assign(state.f, { loc: "", type: "", q: "", minCap: "", maxBid: "", noHighRehab: false, noHighRedemption: false, noFallback: false, onlyDefaulted: false, noPulled: false });
    $("loc").value = $("type").value = $("q").value = $("minCap").value = $("maxBid").value = "";
    ["noHighRehab", "noHighRedemption", "noFallback", "onlyDefaulted", "noPulled"].forEach((id) => ($(id).checked = false));
    state.m = auction.assumptions.bid_multiple; state.target = auction.assumptions.target_cap_rate;
    $("m").value = state.m; $("target").value = (state.target * 100).toFixed(1);
    render();
  });
  $("more").addEventListener("click", () => { state.shown += 40; render(); });
  document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("drawer").hidden) select(null); });
  let rz; addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(render, 120); });

  /* theme toggle */
  const root = document.documentElement;
  const getTheme = () => { try { return localStorage.getItem("theme"); } catch (e) { return null; } };
  const setTheme = (t) => { root.dataset.theme = t; try { localStorage.setItem("theme", t); } catch (e) { /* storage unavailable */ } };
  if (getTheme()) root.dataset.theme = getTheme();
  $("theme").addEventListener("click", () => {
    const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
    setTheme(dark ? "light" : "dark");
    if (state.seg === "map" || state.seg === "roll") render();
  });

  boot();
})();
