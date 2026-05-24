<p align="center">
  <img src="banner.svg" alt="Omnicost: cloud cost-to-serve, mapped across clouds, regions, and conflict risk." width="860">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.11%2B-7367f0?style=flat-square" alt="Python 3.11+">
  <img src="https://img.shields.io/badge/FastAPI-backend-7367f0?style=flat-square" alt="FastAPI">
  <img src="https://img.shields.io/badge/MapLibre%20GL-3D%20map-00bad1?style=flat-square" alt="MapLibre GL">
  <img src="https://img.shields.io/badge/three.js-grid%20city-00bad1?style=flat-square" alt="three.js">
  <img src="https://img.shields.io/badge/GDELT%202.0-conflict%20feed-eb3d63?style=flat-square" alt="GDELT 2.0">
</p>

**Omnicost** turns cloud usage into a cost-to-serve story across providers, regions, and time. It shows where spend lands, what it would cost on a cheaper cloud, which customers sit on the expensive footprint, and how close live geopolitical conflict is to each datacenter region.

Built for the Aiven hackathon. All spend figures are synthetic, relative units derived from the provided parquet datasets.

## Views

Omnicost is a tabbed single page (`index.html`) that frames three apps in iframes:

| View | File | What it shows |
|------|------|---------------|
| **Grid** | `cost_city.html` | A 3D "cost city" (three.js). Each building is a workload; height is spend. A draggable date window and 30 to 180 day presets scrub the timeline live. The summary panel reports current spend, suggested spend on the cheapest cloud, and the saving. |
| **Map** | `geo_distribution.html` | A 3D MapLibre globe. Per region, three extruded boxes compare priced synthetic spend across the three priced clouds, broken down by service. Overlaid on top: live conflict points and a per region risk panel (see below). |
| **Customers** | `customers.html` | A synthetic customer roster on the expensive footprint, with an AI drafted migration email per customer and a one click send. |

## Conflict proximity (GDELT)

The map answers a question a cloud buyer actually has: is my data sitting near instability?

- Pulls CAMEO coded material conflict events from the **GDELT 2.0** event exports (the public REST endpoints are deprecated or rate limited, so Omnicost reads the raw 15 minute export files directly).
- Plots each event, colored by severity and sized by media coverage, and draws a warning radius ring around regions with conflict nearby.
- A left side panel ranks every datacenter region by risk (High, Elevated, Low, Clear), with the distance to the nearest major conflict and a count within the radius.
- GDELT miscodes a lot of soft news (sports, entertainment, history), so two defenses keep the map credible: a source URL noise filter, and a **Major only** default that plots only widely reported, geopolitical scale events (roughly 20 or more articles). An **All events** toggle reveals the full feed.
- Time window (6h, 24h, 72h) and radius are adjustable in the legend. Results are cached server side for about 20 minutes.

## Quickstart

Dependencies are declared inline in each script, so [uv](https://docs.astral.sh/uv/) runs everything with no setup:

```bash
# start the backend (serves the UI and the JSON APIs on http://127.0.0.1:8000)
uv run --python 3.12 server.py

# optional flags
uv run --python 3.12 server.py --port 8000 --reload
```

Then open http://127.0.0.1:8000/ and switch between Grid, Map, and Customers.

The map and customers panels call the backend, so use the server rather than opening the HTML files from disk. Without any API keys the app still runs: email drafting falls back to a template, sending becomes a dry run, and the conflict overlay simply pauses if GDELT is unreachable.

## Rebuilding the data

The views read precomputed JSON blobs. Regenerate them from the parquet datasets when needed:

```bash
uv run --python 3.12 build_geomap.py     # region_data.json   (map: priced spend per region)
uv run --python 3.12 build_multi.py       # cells_multi.json    (grid: cost city buildings)
uv run --python 3.12 build_customers.py   # customers.json      (customer roster)
uv run --python 3.12 gdelt_conflicts.py   # standalone sanity check of the conflict feed
```

## API

The backend (`server.py`, FastAPI) exposes:

| Method | Route | Purpose |
|--------|-------|---------|
| GET | `/api/config` | Which integrations are live, and where mail is delivered |
| GET | `/api/customers` | The synthetic customer roster |
| GET | `/api/conflicts?hours=&radius_km=` | Conflict GeoJSON plus per region proximity risk |
| POST | `/api/draft` | Draft a migration email for one customer (Featherless, or a template fallback) |
| POST | `/api/send` | Deliver the email (Resend), always to the configured inbox |

## Configuration

Copy `.env.example` to `.env` and fill in what you have. Everything is optional and degrades gracefully.

| Variable | Used for |
|----------|----------|
| `RESEND_API_KEY` | Sending the outreach email. Empty means dry run. |
| `MAIL_TO`, `MAIL_FROM` | Delivery inbox and sender for the demo. |
| `FEATHERLESS_API_KEY` | AI email drafting. Empty falls back to a deterministic template. |
| `FEATHERLESS_MODEL`, `FEATHERLESS_BASE_URL` | Model id and OpenAI compatible base URL. |

## Data sources

- `aiven_usage.parquet`: synthetic per workload cloud usage (the hackathon dataset).
- `cross_cloud_list_prices.parquet`: cross cloud list price ratios used to price synthetic spend.
- [GDELT 2.0](https://www.gdeltproject.org/) event exports: live, public, no key required, refreshed every 15 minutes.

## Project layout

```
index.html            Tabbed shell (Grid / Map / Customers)
cost_city.html        Grid view (3D cost city, three.js)
geo_distribution.html Map view (MapLibre) + GDELT conflict overlay
customers.html        Customer roster + AI outreach UI
server.py             FastAPI backend (UI + JSON APIs)
gdelt_conflicts.py    GDELT fetch, parse, severity scoring, proximity risk
build_geomap.py       Builds region_data.json
build_multi.py        Builds cells_multi.json (cost city)
build_customers.py    Builds customers.json
banner.svg            This README's banner
```

## Notes

- All spend numbers are synthetic and relative, not USD. They are list price weighted ratios, useful for comparison, not billing.
- Conflict data is real and live from GDELT. Automated event coding is imperfect, which is why the map defaults to major conflicts only.
- Map tiles are MapTiler and OpenStreetMap, attributed in the map's info control.
