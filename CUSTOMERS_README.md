# Customers & AI outreach — Cost City add-on

Adds a customer dimension to the Cost City dashboard: a synthetic roster of companies,
AI-drafted cross-cloud **migration offer** emails, and one-click sending via Resend.

Everything is built **on top of** the existing priced cost cells — the source parquets
(`aiven_usage.parquet`, `cross_cloud_list_prices.parquet`) and `cells_multi.json` are
**never modified**.

## What got added

| File | Role |
|------|------|
| `build_customers.py` | Generates `customers.json` from `cells_multi.json` (read-only). 14 fictional companies, each assigned real priced cells as their "resources" + a computed migration offer (cheapest-cloud target, discount %, avoidable spend). Deterministic (seeded). |
| `customers.json` | The generated dataset. Re-runnable; safe to delete and rebuild. |
| `server.py` | FastAPI backend. Serves `cost_city.html` and the `/api/*` endpoints; holds the API keys; drafts via Featherless and sends via Resend. |
| `.env.example` | Config template — copy to `.env` and add your keys. |
| `cost_city.html` | Gained a **Customers** panel (bottom-left) + an AI-email modal. Talks to the backend; if no backend is reachable (opened as a static file) the panel just stays hidden and the 3D view is unchanged. |

## Run it

```bash
# 1. (only if you regenerated the cells) rebuild the customer dataset
python3 build_customers.py

# 2. add your keys
cp .env.example .env        # then edit .env

# 3. start the backend + dashboard
uv run --python 3.12 server.py        # http://127.0.0.1:8000/
```

Open the URL, click a customer in the **Customers** panel → review their resources →
**Draft email with AI** → edit if you like → **Send**.

## Keys & graceful degradation (nothing hard-fails)

| Missing | Behaviour |
|---------|-----------|
| `FEATHERLESS_API_KEY` | Drafting falls back to a deterministic template (same talking points, no LLM). |
| `RESEND_API_KEY` | Send becomes a **dry-run** that reports what *would* be delivered. |

- **All mail goes to `MAIL_TO` (default `silveikka@gmail.com`)** regardless of the
  customer's fictional address — enforced server-side in `server.py`.
- Featherless is OpenAI-compatible; set any catalog model via `FEATHERLESS_MODEL`.
- Resend's shared `onboarding@resend.dev` sender only delivers to the address that owns
  your Resend account, which is fine since that's your own inbox. For a custom `MAIL_FROM`,
  verify a domain in Resend first.

## The email

The draft follows the brief: *"We see you have N resources on `cloud_x` — interested in
moving them to `cloud_y`? We can give you Z% discount, and your resources are already
copied to `cloud_y`."* The numbers (resource count, current/target cloud, discount %) are
the customer's real computed economics, not invented by the model.
