# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx"]
# ///
"""
Bake a static conflicts.json snapshot.

The map's conflict overlay is normally live (it calls /api/conflicts, which fetches
GDELT server-side). On a static host like Vercel there is no backend, so that endpoint
404s. This script writes a point-in-time snapshot that the map falls back to, exactly
like region_data.json and the inlined customer roster: the conflict zones still show,
they are just frozen at build time.

Run before deploying:  uv run --python 3.12 build_conflicts.py
Then commit conflicts.json so the static host serves it.
"""

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import gdelt_conflicts as g

HERE = Path(__file__).resolve().parent
OUT = HERE / "conflicts.json"


def main():
    data = asyncio.run(g.get_conflicts(hours=g.DEFAULT_HOURS, radius_km=g.DEFAULT_RADIUS_KM, force=True))
    data["snapshot"] = True
    data["built_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    OUT.write_text(json.dumps(data, separators=(",", ":")))
    print(f"wrote {OUT.name}  ({OUT.stat().st_size/1024:.0f} KB)")
    print(f"  window {data['window_hours']}h · radius {data['radius_km']} km · "
          f"{data['total_conflicts']} events, {data['plotted']} plotted · slot {data.get('latest_slot')}")


if __name__ == "__main__":
    main()
