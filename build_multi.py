# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas", "pyarrow"]
# ///
"""
Cost City — multi-window aggregation pipeline.

Reads the unmodified hackathon parquets, builds a multi-window aggregation of
priced cost-to-serve cells, writes cells_multi.json, and inlines that JSON into
cost_city.html (self-contained, no backend).

The output is time-bucketed: every priced (cloud, geo, service, sku) cell carries
per-bucket spend + potential-savings, so the widget can re-aggregate ANY [from, to]
date range live (adjust length AND slide the window back through history).

Run:  uv run --python 3.12 build_multi.py
Tweak the time resolution of the date sliders:
      uv run --python 3.12 build_multi.py --bucket-days 1    # daily (finest, ~4x bigger)
      uv run --python 3.12 build_multi.py --bucket-days 30   # monthly (coarsest, tiny)
"""

import argparse
import json
import math
import re
import sys
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
USAGE = HERE / "aiven_usage.parquet"
PRICES = HERE / "cross_cloud_list_prices.parquet"
OUT_JSON = HERE / "cells_multi.json"
HTML = HERE / "cost_city.html"

# Default day scale: monthly stops (30..540) plus the standard 365 — gives the UI
# slider smooth granularity while keeping the PRD demo days (90, 180, 365, 540) exact.
# Overridable with --windows.
WINDOWS = sorted(set(range(30, 541, 30)) | {365})
JOIN_KEYS = ["cloud", "geo_bucket", "sku_family", "unit"]
GRP_KEYS = ["geo_bucket", "sku_family", "unit"]      # cheapest-cloud comparison group
CELL_KEYS = ["cloud", "geo_bucket", "service_category", "sku_family"]
SAVINGS_CAP = 0.60
TOP_N = 250

DATA_START = "/* COST_CITY_DATA_START */"
DATA_END = "/* COST_CITY_DATA_END */"

# PRD empirical findings — used purely as a verification oracle (capped%, raw%).
ORACLE = {
    30:  (10.0, 14.1),
    90:  (13.2, 18.3),
    180: (13.2, 18.3),
    365: (13.5, 18.5),
    540: (15.1, 21.1),
}

# Friendly tab labels for the standard day scale; custom windows fall back to "<N>d".
DEFAULT_LABELS = {30: "30d", 90: "90d", 180: "180d", 365: "365d", 540: "18mo"}


def window_label(days: int) -> str:
    return DEFAULT_LABELS.get(days, f"{days}d")


def build_cheapest(prices: pd.DataFrame) -> pd.DataFrame:
    """For each (geo_bucket, sku_family, unit) group, the cheapest cloud + its p50.

    Window-independent, so computed once. Ties resolve to the first occurrence
    (economically equivalent). NaN p50 dropped defensively.
    """
    p = prices.dropna(subset=["p50_ratio"]).reset_index(drop=True)
    g = p.groupby(GRP_KEYS, sort=False)["p50_ratio"]
    cheapest_p50 = g.min().rename("cheapest_p50")
    cheapest_cloud = (
        p.loc[g.idxmin(), GRP_KEYS + ["cloud"]]
        .rename(columns={"cloud": "cheapest_cloud"})
        .reset_index(drop=True)
    )
    cheap = cheapest_cloud.merge(cheapest_p50, on=GRP_KEYS)
    return cheap


def process_window(usage: pd.DataFrame, prices: pd.DataFrame, cheap: pd.DataFrame,
                   anchor: pd.Timestamp, days: int):
    """Aggregate one trailing window to priced (cloud, geo, service, sku) cells."""
    start = anchor - pd.Timedelta(days=days)
    w = usage[(usage.status_date >= start) & (usage.status_date <= anchor) & (usage.amount > 0)]

    # Step 2: keep priced rows only (inner join drops cloud_a + unpriced sku/unit combos).
    m = w.merge(prices[JOIN_KEYS + ["p50_ratio"]], on=JOIN_KEYS, how="inner")
    # Step 3: attach the cheapest cloud + its p50 for the row's comparison group.
    m = m.merge(cheap, on=GRP_KEYS, how="left")

    # Step 4: per-row economics.
    m["synth_spend"] = m.amount * m.p50_ratio
    m["pot"] = m.amount * (m.p50_ratio - m.cheapest_p50).clip(lower=0)

    # Step 5: aggregate to cells.
    cell = (
        m.groupby(CELL_KEYS, sort=False)
        .agg(spend=("synth_spend", "sum"), pot=("pot", "sum"))
        .reset_index()
    )
    cell["sr"] = (cell.pot / cell.spend).clip(0, 1)

    # target_cloud = spend-weighted mode of the per-line cheapest cloud.
    votes = (
        m.groupby(CELL_KEYS + ["cheapest_cloud"], sort=False)["synth_spend"]
        .sum()
        .reset_index()
        .sort_values("synth_spend", ascending=False)
        .drop_duplicates(CELL_KEYS)
    )
    cell = cell.merge(votes[CELL_KEYS + ["cheapest_cloud"]], on=CELL_KEYS, how="left")
    cell = cell.rename(columns={"cheapest_cloud": "target_cloud"})
    # No move where this cell's own cloud is already the cheapest.
    cell.loc[cell.cloud == cell.target_cloud, "target_cloud"] = None

    # Step 7: window summaries over the FULL priced cell set.
    current = float(cell.spend.sum())
    suggested = float((cell.spend * (1 - cell.sr.clip(upper=SAVINGS_CAP))).sum())
    raw_pct = float(cell.pot.sum() / current) if current else 0.0
    capped_pct = float((current - suggested) / current) if current else 0.0
    summary = dict(
        current=current,
        suggested=suggested,
        raw_pct=raw_pct,
        capped_pct=capped_pct,
        start=str(start.date()),
        end=str(anchor.date()),
        n_priced_rows=int(len(m)),
        n_cells=int(len(cell)),
    )
    return cell, summary


def inline_into_html(payload_json: str) -> bool:
    """Replace the marker-delimited data blob inside cost_city.html. Idempotent."""
    if not HTML.exists():
        print(f"  ! {HTML.name} not found — skipped inlining (JSON written only).")
        return False
    html = HTML.read_text()
    pattern = re.compile(re.escape(DATA_START) + r".*?" + re.escape(DATA_END), re.S)
    if not pattern.search(html):
        raise SystemExit(
            f"ERROR: data markers not found in {HTML.name}. "
            f"Expected '{DATA_START}...{DATA_END}'."
        )
    # JSON inside an application/json script tag needs no quote escaping; just guard </script>.
    safe = payload_json.replace("</", "<\\/")
    new = pattern.sub(DATA_START + safe + DATA_END, html, count=1)
    HTML.write_text(new)
    return True


def build_buckets(usage, prices, cheap, anchor, bucket_days):
    """Aggregate the full priced history into per-(cell, time-bucket) spend + savings.

    The widget sums whatever bucket range the date sliders select, so any [from, to]
    window can be computed client-side. Buckets are bucket_days wide, the most recent
    one ending exactly at the anchor.
    """
    total_days = (anchor - usage.status_date.min()).days + 1
    n_buckets = math.ceil(total_days / bucket_days)
    span = n_buckets * bucket_days
    start = anchor - pd.Timedelta(days=span - 1)

    w = usage[(usage.status_date >= start) & (usage.status_date <= anchor) & (usage.amount > 0)]
    m = w.merge(prices[JOIN_KEYS + ["p50_ratio"]], on=JOIN_KEYS, how="inner")
    m = m.merge(cheap, on=GRP_KEYS, how="left")
    m["synth_spend"] = m.amount * m.p50_ratio
    m["pot"] = m.amount * (m.p50_ratio - m.cheapest_p50).clip(lower=0)
    doff = (anchor - m.status_date).dt.days
    m["bucket"] = (n_buckets - 1) - (doff // bucket_days)   # 0 = oldest, n_buckets-1 = most recent

    clouds = sorted(m.cloud.unique())
    geos = sorted(m.geo_bucket.unique())
    services = sorted(m.service_category.unique())
    skus = sorted(m.sku_family.unique())
    ci = {v: i for i, v in enumerate(clouds)}
    gi = {v: i for i, v in enumerate(geos)}
    si = {v: i for i, v in enumerate(services)}
    ki = {v: i for i, v in enumerate(skus)}

    # Static per-cell target = spend-weighted mode of the per-line cheapest cloud (full span).
    votes = (m.groupby(CELL_KEYS + ["cheapest_cloud"], sort=False)["synth_spend"].sum()
             .reset_index().sort_values("synth_spend", ascending=False).drop_duplicates(CELL_KEYS))
    target = votes.set_index(CELL_KEYS)["cheapest_cloud"]

    g = (m.groupby(CELL_KEYS + ["bucket"], sort=False)
         .agg(spend=("synth_spend", "sum"), pot=("pot", "sum")).reset_index())

    cells = []
    for key, sub in g.groupby(CELL_KEYS, sort=False):
        sub = sub.sort_values("bucket")
        bs, ss, ps = [], [], []
        for b, sp, po in zip(sub.bucket, sub.spend, sub.pot):
            spi = round(float(sp))
            if spi <= 0:                      # drop sub-unit bucket slices to keep the blob lean
                continue
            bs.append(int(b)); ss.append(spi); ps.append(round(float(po)))
        if not bs or sum(ss) < 1:             # drop long-tail cells (not actionable)
            continue
        cloud, geo, svc, sku = key
        tcloud = target.loc[key]
        tgt = ci[tcloud] if tcloud != cloud else -1
        cells.append([ci[cloud], gi[geo], si[svc], ki[sku], tgt, bs, ss, ps])

    def bstart(b):
        return str((anchor - pd.Timedelta(days=(n_buckets - 1 - b) * bucket_days + (bucket_days - 1))).date())

    def bend(b):
        return str((anchor - pd.Timedelta(days=(n_buckets - 1 - b) * bucket_days)).date())

    payload = {
        "clouds": clouds, "geos": geos, "services": services, "skus": skus,
        "bucket_days": bucket_days, "n_buckets": n_buckets, "anchor": str(anchor.date()),
        "bucket_starts": [bstart(b) for b in range(n_buckets)],
        "bucket_ends": [bend(b) for b in range(n_buckets)],
        "cells": cells,
    }
    return payload


def summarize_buckets(cells, b_from, b_to):
    """Browser-equivalent window summary (for verification) over bucket range [b_from, b_to]."""
    current = suggested = rawpot = 0.0
    for c in cells:
        bs, ss, ps = c[5], c[6], c[7]
        spend = pot = 0.0
        for i, b in enumerate(bs):
            if b < b_from:
                continue
            if b > b_to:
                break
            spend += ss[i]; pot += ps[i]
        if spend <= 0:
            continue
        sr = min(max(pot / spend, 0.0), 1.0)
        current += spend
        rawpot += spend * sr
        suggested += spend * (1 - min(sr, SAVINGS_CAP))
    raw = rawpot / current if current else 0.0
    capped = (current - suggested) / current if current else 0.0
    return current, suggested, raw, capped


def parse_args(argv):
    p = argparse.ArgumentParser(description="Cost City time-bucketed aggregation.")
    p.add_argument("--bucket-days", type=int, default=7,
                   help="Time-bucket size for the date sliders (default: 7=weekly; "
                        "1=daily/finest but ~4x larger; 30=monthly/coarsest).")
    p.add_argument("--windows", default=None,
                   help="Trailing day windows to print in the verification table "
                        f"(default: {','.join(map(str, WINDOWS))}).")
    args = p.parse_args(argv)
    if args.bucket_days <= 0:
        p.error("--bucket-days must be a positive integer")
    windows = (sorted({int(x) for x in args.windows.split(",") if x.strip()})
               if args.windows else list(WINDOWS))
    return windows, args.bucket_days


def main(argv=None):
    windows, bucket_days = parse_args(argv)

    usage = pd.read_parquet(USAGE)
    prices = pd.read_parquet(PRICES)
    anchor = usage.status_date.max()
    cheap = build_cheapest(prices)

    payload = build_buckets(usage, prices, cheap, anchor, bucket_days)
    payload_json = json.dumps(payload, separators=(",", ":"))
    OUT_JSON.write_text(payload_json)
    inlined = inline_into_html(payload_json)

    # ---- Verification report ----
    nb = payload["n_buckets"]
    print(f"\nanchor: {anchor.date()}   bucket_days: {bucket_days}   buckets: {nb}   "
          f"cells: {len(payload['cells'])}")
    print(f"clouds: {payload['clouds']}   geos: {payload['geos']}")
    print(f"skus ({len(payload['skus'])}): {payload['skus']}")
    print(f"history: {payload['bucket_starts'][0]} → {payload['bucket_ends'][-1]}")
    print(f"JSON: {OUT_JSON.name}  ({len(payload_json)/1024:.1f} KB)   HTML inlined: {inlined}\n")

    # (a) Economics unchanged: exact-day trailing windows vs PRD oracle.
    print("Exact-day trailing windows (proves economics) vs PRD oracle:")
    print(f"{'win':>4} {'current':>12} {'capped%':>8} {'raw%':>7}   {'orc.cap':>7} {'orc.raw':>7}  result")
    all_pass = True
    checked = 0
    for n in windows:
        _, s = process_window(usage, prices, cheap, anchor, n)
        cap, raw = 100 * s["capped_pct"], 100 * s["raw_pct"]
        if n in ORACLE:
            oc, orw = ORACLE[n]
            ok = abs(cap - oc) < 0.5
            all_pass &= ok
            checked += 1
            oc_s, orw_s, res = f"{oc:>6.1f}%", f"{orw:>6.1f}%", "PASS" if ok else "CHECK"
        else:
            oc_s, orw_s, res = f"{'—':>7}", f"{'—':>7}", "n/a"
        print(f"{n:>4} {s['current']:>12,.0f} {cap:>7.1f}% {raw:>6.1f}%   {oc_s} {orw_s}  {res}")
    print("ALL ORACLE WINDOWS PASS" if (checked and all_pass)
          else ("Some windows off — review" if checked else "no oracle windows"))

    # (b) Bucket-data integrity: browser-equivalent summation over the same data.
    cur_all, _, _, cap_all = summarize_buckets(payload["cells"], 0, nb - 1)
    b90 = max(0, nb - math.ceil(90 / bucket_days))
    cur90, _, _, cap90 = summarize_buckets(payload["cells"], b90, nb - 1)
    print("\nBucket-data check (what the browser will compute):")
    print(f"  full span   : current {cur_all:>12,.0f}   capped {100*cap_all:.1f}%")
    print(f"  trailing ~90: current {cur90:>12,.0f}   capped {100*cap90:.1f}%   "
          f"(bucket-aligned; oracle 90d ≈ 13.2%)")


if __name__ == "__main__":
    sys.exit(main())
