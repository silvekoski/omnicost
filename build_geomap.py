# /// script
# requires-python = ">=3.11"
# dependencies = ["pandas", "pyarrow"]
# ///
"""
Geo Distribution — build the data blob for the 3D regional map.

Reads the hackathon parquets, computes priced synthetic spend per
(geo_bucket, cloud, service_category), and writes region_data.json.

Only the three priced clouds (cloud_b/c/d) survive the inner join with the
price catalog — cloud_a is unpriced and naturally drops out. Those three are
"the providers with their own regions" the map visualises.

Run:  uv run --python 3.12 build_geomap.py
"""

import json
from pathlib import Path

import pandas as pd

HERE = Path(__file__).resolve().parent
USAGE = HERE / "aiven_usage.parquet"
PRICES = HERE / "cross_cloud_list_prices.parquet"
OUT = HERE / "region_data.json"

JOIN_KEYS = ["cloud", "geo_bucket", "sku_family", "unit"]

# Coarse geo buckets → a land-centred point that represents the *region* (not a
# single city — the buckets are deliberately coarse). Anchors are kept inland so
# the tight three-box cluster sits on land instead of spilling into the sea or a
# neighbouring country. "other" is non-geographic; parked on a neutral land spot
# (the Sahara) and clearly labelled so it never masquerades as a real region.
GEOS = {
    "us-east":  {"name": "US East",   "place": "Eastern US",        "lat": 39.5,  "lon": -80.5,  "synthetic": False},
    "us-west":  {"name": "US West",   "place": "Western US",        "lat": 43.8,  "lon": -116.5, "synthetic": False},
    "eu-west":  {"name": "EU West",   "place": "Western Europe",    "lat": 48.9,  "lon": 2.4,    "synthetic": False},
    "eu-north": {"name": "EU North",  "place": "Nordics",           "lat": 62.3,  "lon": 26.5,   "synthetic": False},
    "apac":     {"name": "APAC",      "place": "Southeast Asia",    "lat": 3.8,   "lon": 101.8,  "synthetic": False},
    "other":    {"name": "Other",     "place": "Multi / unbucketed","lat": 16.0,  "lon": -4.0,   "synthetic": True},
}

# Service order = descending global spend (legend + stack order, biggest at base).
SERVICE_ORDER = [
    "database-relational",
    "streaming",
    "search",
    "database-nosql",
    "observability",
    "analytics",
]
SERVICE_COLORS = {
    "database-relational": "#7367f0",  # primary / chart-1
    "streaming":           "#eb3d63",  # chart-5
    "search":              "#ffab1d",  # chart-4
    "database-nosql":      "#00bad1",  # accent / chart-2
    "observability":       "#28c76f",  # chart-3
    "analytics":           "#2092ec",  # dark chart-2
}
CLOUD_LABELS = {"cloud_b": "Cloud B", "cloud_c": "Cloud C", "cloud_d": "Cloud D"}


def main():
    usage = pd.read_parquet(USAGE)
    prices = pd.read_parquet(PRICES)

    u = usage[usage.amount > 0]
    m = u.merge(prices[JOIN_KEYS + ["p50_ratio"]], on=JOIN_KEYS, how="inner")
    m["synth"] = m.amount * m.p50_ratio

    grp = (
        m.groupby(["geo_bucket", "cloud", "service_category"])["synth"]
        .sum()
        .reset_index()
    )

    clouds = ["cloud_b", "cloud_c", "cloud_d"]
    columns = []
    for geo in GEOS:
        for cloud in clouds:
            sub = grp[(grp.geo_bucket == geo) & (grp.cloud == cloud)]
            if sub.empty:
                continue
            services = {}
            for svc in SERVICE_ORDER:
                row = sub[sub.service_category == svc]
                val = float(row.synth.iloc[0]) if not row.empty else 0.0
                if val > 0:
                    services[svc] = round(val)
            total = round(float(sub.synth.sum()))
            if total <= 0:
                continue
            columns.append({
                "geo": geo,
                "cloud": cloud,
                "total": total,
                "services": services,
            })

    max_total = max(c["total"] for c in columns)
    payload = {
        "generated_from": "aiven_usage.parquet x cross_cloud_list_prices.parquet (priced synthetic spend)",
        "metric": "Synthetic list-price-weighted spend (amount x p50 ratio). Relative, not USD.",
        "clouds": clouds,
        "cloudLabels": CLOUD_LABELS,
        "serviceOrder": SERVICE_ORDER,
        "serviceColors": SERVICE_COLORS,
        "geos": GEOS,
        "columns": columns,
        "maxTotal": max_total,
        "grandTotal": round(sum(c["total"] for c in columns)),
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")))

    # report
    print(f"wrote {OUT.name}  ({OUT.stat().st_size/1024:.1f} KB)")
    print(f"clouds: {clouds}   geos: {list(GEOS)}")
    print(f"columns (geo x cloud): {len(columns)}   maxTotal: {max_total:,}   grandTotal: {payload['grandTotal']:,}")
    for c in sorted(columns, key=lambda x: -x["total"])[:5]:
        print(f"  {c['geo']:<8} {c['cloud']:<8} {c['total']:>10,}  {list(c['services'])}")


if __name__ == "__main__":
    main()
