# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Synthetic customer dataset — built ON TOP of the priced cells, never editing source data.

The hackathon parquets carry no customer dimension, so this script invents a roster of
fictional companies and assigns each one a slice of the *already-priced* cost cells from
cells_multi.json (produced by build_multi.py). Every euro a customer "spends" and every
percent they could "save" traces straight back to the real cost-to-serve economics —
we only attach names to existing cells.

Source data is read-only:
  - cells_multi.json  (priced cells: spend + avoidable savings + cheapest-cloud target)
The parquets (aiven_usage.parquet, cross_cloud_list_prices.parquet) are NOT touched.

Output:
  - customers.json    (roster + per-customer resources + the migration offer)

Run:  python3 build_customers.py        (no third-party deps)
"""

import json
import random
import re
from pathlib import Path

HERE = Path(__file__).resolve().parent
CELLS = HERE / "cells_multi.json"
OUT = HERE / "customers.json"
HTML = HERE / "customers.html"          # roster gets inlined here so the page works offline

DATA_START = "/* CUSTOMERS_DATA_START */"
DATA_END = "/* CUSTOMERS_DATA_END */"


def inline_into_html(payload_json: str) -> bool:
    """Replace the marker-delimited roster blob inside customers.html. Idempotent."""
    if not HTML.exists():
        print(f"  ! {HTML.name} not found — skipped inlining (JSON written only).")
        return False
    html = HTML.read_text()
    pat = re.compile(re.escape(DATA_START) + r".*?" + re.escape(DATA_END), re.S)
    if not pat.search(html):
        print(f"  ! data markers not found in {HTML.name} — skipped inlining.")
        return False
    safe = payload_json.replace("</", "<\\/")    # don't let the JSON close the <script>
    repl = DATA_START + safe + DATA_END
    HTML.write_text(pat.sub(lambda _m: repl, html, count=1))   # func repl => no \-escape parsing
    return True

SEED = 20260524          # deterministic roster — same dataset every run
SAVINGS_CAP = 0.60       # mirror the dashboard: never promise more than 60% off

# Friendly display names for the anonymised cloud / service codes.
CLOUD_NAMES = {"cloud_b": "Cloud B", "cloud_c": "Cloud C", "cloud_d": "Cloud D"}
SERVICE_NAMES = {
    "database-relational": "PostgreSQL / MySQL",
    "database-nosql": "Cassandra / Valkey",
    "streaming": "Kafka streaming",
    "search": "OpenSearch",
    "observability": "Metrics & logs",
    "analytics": "ClickHouse analytics",
}
GEO_NAMES = {
    "us-east": "US East", "us-west": "US West", "eu-west": "EU West",
    "eu-north": "EU North", "apac": "APAC", "other": "Multi-region",
}

# Fictional roster. `focus` biases which services we sample for them (their "product"),
# `cloud` is the legacy provider they're predominantly running on today.
PROFILES = [
    ("Northwind Analytics",   "Priya Raman",     "B2B analytics SaaS",        "cloud_b", ["analytics", "database-relational"]),
    ("Helsinki Robotics",     "Aleksi Korhonen", "Industrial IoT",            "cloud_d", ["streaming", "observability"]),
    ("Meridian Fintech",      "Sofia Alvarez",   "Payments platform",         "cloud_c", ["database-relational", "database-nosql"]),
    ("Aurora Media Group",    "Tom Becker",      "Streaming media",           "cloud_b", ["streaming", "search"]),
    ("Pacific Logistics",     "Mei Lin",         "Freight & logistics",       "cloud_d", ["database-relational", "analytics"]),
    ("Lighthouse Health",     "Dr. Omar Said",   "Digital health",            "cloud_c", ["database-relational", "observability"]),
    ("Vertex Gaming",         "Jonas Vik",       "Mobile gaming",             "cloud_b", ["database-nosql", "streaming"]),
    ("Cedar Retail Cloud",    "Hanna Mäkelä",    "E-commerce platform",       "cloud_d", ["search", "database-relational"]),
    ("Quantum Insights",      "Ravi Deshpande",  "ML feature store",          "cloud_c", ["analytics", "database-nosql"]),
    ("Baltic Telecom",        "Liis Tamm",       "Telco / 5G",                "cloud_b", ["observability", "streaming"]),
    ("Solaris Energy",        "Carlos Mendez",   "Smart-grid utility",        "cloud_d", ["streaming", "analytics"]),
    ("Atlas Travel",          "Yuki Tanaka",     "Travel booking",            "cloud_c", ["search", "database-relational"]),
    ("Granite Security",      "Erik Johansson",  "Cyber SIEM",                "cloud_b", ["observability", "search"]),
    ("Riverstone Edu",        "Amara Okafor",    "EdTech platform",           "cloud_d", ["database-relational", "database-nosql"]),
]


def load_cells():
    """Aggregate cells_multi.json into flat, named, per-cell records with economics."""
    d = json.loads(CELLS.read_text())
    clouds, geos, svcs, skus = d["clouds"], d["geos"], d["services"], d["skus"]
    cells = []
    for c in d["cells"]:
        ci, gi, si, ki, tgt = c[0], c[1], c[2], c[3], c[4]
        spend = float(sum(c[6]))
        pot = float(sum(c[7]))
        if spend <= 0:
            continue
        sr = min(SAVINGS_CAP, max(0.0, pot / spend))
        cells.append({
            "cloud": clouds[ci],
            "geo": geos[gi],
            "service": svcs[si],
            "sku": skus[ki],
            "spend": spend,
            "sr": sr,
            "target": clouds[tgt] if tgt >= 0 else None,
        })
    return cells


def synth_units(spend: float) -> int:
    """Turn a relative-spend figure into a believable resource/instance count."""
    return max(1, round(spend / 4000))


def build_customer(rng, profile, pool):
    name, contact, industry, cloud, focus = profile
    # Their resources: cells on their legacy cloud, biased toward their focus services.
    on_cloud = [c for c in pool if c["cloud"] == cloud]
    focused = [c for c in on_cloud if c["service"] in focus]
    others = [c for c in on_cloud if c["service"] not in focus]
    rng.shuffle(focused)
    rng.shuffle(others)
    n = rng.randint(4, 6)
    chosen = (focused + others)[:n]
    if not chosen:                       # fallback: any cells on that cloud
        chosen = on_cloud[:n]

    resources, total_spend, avoidable = [], 0.0, 0.0
    target_avoidable = {}                # cloud -> avoidable spend routed there
    for c in chosen:
        units = synth_units(c["spend"])
        resources.append({
            "service": c["service"],
            "service_label": SERVICE_NAMES.get(c["service"], c["service"]),
            "geo": c["geo"],
            "geo_label": GEO_NAMES.get(c["geo"], c["geo"]),
            "sku": c["sku"],
            "cloud": c["cloud"],
            "cloud_label": CLOUD_NAMES.get(c["cloud"], c["cloud"]),
            "units": units,
            "monthly_spend": round(c["spend"]),
            "savings_ratio": round(c["sr"], 4),
            "target_cloud": c["target"],
            "target_cloud_label": CLOUD_NAMES.get(c["target"]) if c["target"] else None,
        })
        total_spend += c["spend"]
        avoidable += c["spend"] * c["sr"]
        if c["target"]:
            target_avoidable[c["target"]] = target_avoidable.get(c["target"], 0.0) + c["spend"] * c["sr"]

    # Recommended destination = the cloud that captures the most avoidable spend.
    # If nothing is avoidable (every resource is already on its cheapest cloud), don't
    # fabricate a move: keep them on their current cloud with no discount. Floor-to-8%
    # promos only make sense when there's real avoidable spend to discount.
    if target_avoidable and avoidable > 0:
        target_cloud = max(target_avoidable, key=target_avoidable.get)
        discount_pct = round(100 * avoidable / total_spend) if total_spend else 0
        discount_pct = max(8, min(45, discount_pct))   # believable promo band
    else:
        target_cloud = cloud           # already on the cheapest cloud — nothing to migrate
        discount_pct = 0

    slug = name.lower().replace(" ", "-").replace("/", "")
    return {
        "id": slug,
        "company": name,
        "contact": contact,
        "industry": industry,
        # Display-only address. Real sends are forced to MAIL_TO server-side.
        "email": f"{contact.split()[0].lower()}@{slug.replace('-', '')}.example",
        "current_cloud": cloud,
        "current_cloud_label": CLOUD_NAMES.get(cloud, cloud),
        "target_cloud": target_cloud,
        "target_cloud_label": CLOUD_NAMES.get(target_cloud, target_cloud),
        "discount_pct": discount_pct,
        "resource_count": sum(r["units"] for r in resources),
        "monthly_spend": round(total_spend),
        "avoidable_spend": round(avoidable),
        "resources": resources,
    }


def main():
    if not CELLS.exists():
        raise SystemExit(f"ERROR: {CELLS.name} missing — run build_multi.py first.")
    pool = load_cells()
    rng = random.Random(SEED)
    customers = [build_customer(rng, p, pool) for p in PROFILES]

    payload = {
        "generated_from": "cells_multi.json (priced cost cells) — parquets untouched",
        "note": "Relative synthetic spend units (not USD). Customers/contacts are fictional.",
        "currency_unit": "u",
        "count": len(customers),
        "customers": customers,
    }
    OUT.write_text(json.dumps(payload, indent=2))
    inlined = inline_into_html(json.dumps(payload, separators=(",", ":")))

    # report
    print(f"wrote {OUT.name}  ({OUT.stat().st_size/1024:.1f} KB)   customers: {len(customers)}"
          f"   inlined into {HTML.name}: {inlined}")
    print(f"{'company':<22} {'from':>8} {'→ to':>8} {'disc':>5} {'res':>5} {'spend':>10}")
    for c in customers:
        print(f"{c['company']:<22} {c['current_cloud']:>8} {c['target_cloud']:>8} "
              f"{c['discount_pct']:>4}% {c['resource_count']:>5} {c['monthly_spend']:>10,}")


if __name__ == "__main__":
    main()
