# /// script
# requires-python = ">=3.11"
# dependencies = ["fastapi", "uvicorn", "httpx", "resend", "python-dotenv"]
# ///
"""
Cost City backend — customer roster + AI email drafting (Featherless) + sending (Resend).

Serves cost_city.html and the JSON endpoints the in-page panels call:
  GET  /api/config     -> which integrations are live, and where mail actually goes
  GET  /api/customers  -> the synthetic roster (customers.json)
  GET  /api/conflicts  -> live GDELT conflict overlay + per-datacenter proximity risk
  POST /api/draft      -> Featherless drafts a personalised migration email for one customer
  POST /api/send       -> Resend delivers it (ALWAYS to MAIL_TO, never the fictional address)

Keys are read from the environment / a local .env (see .env.example). Anything missing
degrades gracefully: no Featherless key -> deterministic template draft; no Resend key
-> the /api/send call reports dry-run instead of sending.

Run:  uv run --python 3.12 server.py
      uv run --python 3.12 server.py --port 8000 --reload
Then open http://127.0.0.1:8000/
"""

import argparse
import json
import os
import re
from pathlib import Path

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel

import gdelt_conflicts

HERE = Path(__file__).resolve().parent
INDEX = HERE / "index.html"            # tabbed shell: Grid (cost city) + Map + Customers
HTML = HERE / "cost_city.html"
GEO = HERE / "geo_distribution.html"
CUSTOMERS_HTML = HERE / "customers.html"
CUSTOMERS = HERE / "customers.json"

load_dotenv(HERE / ".env")

# ---- config (all overridable via env / .env) ----
FEATHERLESS_KEY = os.getenv("FEATHERLESS_API_KEY", "").strip()
FEATHERLESS_MODEL = os.getenv("FEATHERLESS_MODEL", "meta-llama/Meta-Llama-3.1-8B-Instruct").strip()
FEATHERLESS_URL = os.getenv("FEATHERLESS_BASE_URL", "https://api.featherless.ai/v1").rstrip("/")
RESEND_KEY = os.getenv("RESEND_API_KEY", "").strip()
MAIL_TO = os.getenv("MAIL_TO", "silveikka@gmail.com").strip()
MAIL_FROM = os.getenv("MAIL_FROM", "Aiven Cost City <onboarding@resend.dev>").strip()

app = FastAPI(title="Cost City — Customers & Outreach")


def load_customers() -> dict:
    if not CUSTOMERS.exists():
        raise HTTPException(503, "customers.json missing — run: python3 build_customers.py")
    return json.loads(CUSTOMERS.read_text())


def find_customer(cid: str) -> dict:
    for c in load_customers()["customers"]:
        if c["id"] == cid:
            return c
    raise HTTPException(404, f"unknown customer: {cid}")


def resource_summary(cust: dict) -> str:
    """Compact 'N× Service in Region' breakdown, biggest spend first."""
    parts = []
    for r in sorted(cust["resources"], key=lambda x: -x["monthly_spend"])[:4]:
        parts.append(f"{r['units']}× {r['service_label']} in {r['geo_label']}")
    return "; ".join(parts)


# ============================ EMAIL DRAFTING ============================

SYSTEM_PROMPT = (
    "You are a customer success manager at Aiven, a managed open-source data platform. "
    "You write short, warm, professional outreach emails that help customers cut their "
    "cloud bill by migrating workloads to a cheaper cloud. Plain text only — no markdown, "
    "no bullet symbols, no placeholders in brackets. Never use emoji or any pictographic "
    "characters anywhere, including the subject line. 110-170 words. One clear call to action."
)


# Emoji / pictographic ranges — stripped from model output as a hard guarantee, since
# smaller models ignore "no emoji" instructions (esp. in subject lines). Arrows like → are
# intentionally NOT included so "Cloud D → Cloud B" survives.
_EMOJI_RE = re.compile(
    "[\U0001F300-\U0001FAFF\U00002600-\U000026FF\U00002700-\U000027BF"
    "\U0001F1E6-\U0001F1FF\U0000FE00-\U0000FE0F\U0000200D\U00002B00-\U00002BFF]",
    flags=re.UNICODE,
)


def strip_emoji(s: str) -> str:
    """Remove emoji and tidy the whitespace/punctuation they leave behind."""
    s = _EMOJI_RE.sub("", s)
    s = re.sub(r"[ \t]{2,}", " ", s)            # collapse gaps left by removed glyphs
    s = re.sub(r"^[\s:–—-]+", "", s)            # strip leading ": " / dashes if emoji led
    return s.strip()


def build_user_prompt(cust: dict) -> str:
    return (
        f"Write a migration-offer email to {cust['contact']} at {cust['company']} "
        f"({cust['industry']}).\n\n"
        f"Facts to use (do not invent numbers):\n"
        f"- They currently run {cust['resource_count']} resources on {cust['current_cloud_label']}: "
        f"{resource_summary(cust)}.\n"
        f"- We can move these to {cust['target_cloud_label']} for a "
        f"{cust['discount_pct']}% discount on the affected spend.\n"
        f"- Reassure them their resources are ALREADY copied/replicated to "
        f"{cust['target_cloud_label']}, so the switch is one click with no downtime and no data loss.\n"
        f"- Keep it consultative, not pushy. Address them by first name.\n"
        f"- Sign off as 'The Aiven Cost City team'.\n\n"
        f"Respond with ONLY a JSON object: "
        f'{{"subject": "<subject line>", "body": "<email body>"}}'
    )


def template_draft(cust: dict) -> dict:
    """Deterministic fallback used when no Featherless key is configured."""
    first = cust["contact"].split()[0]
    subject = (
        f"Cut your {cust['current_cloud_label']} bill {cust['discount_pct']}% — "
        f"move to {cust['target_cloud_label']}"
    )
    body = (
        f"Hi {first},\n\n"
        f"Looking at your {cust['company']} footprint, we see {cust['resource_count']} resources "
        f"running on {cust['current_cloud_label']} ({resource_summary(cust)}).\n\n"
        f"Those same workloads run materially cheaper on {cust['target_cloud_label']}. We'd like to "
        f"offer you a {cust['discount_pct']}% discount on the affected spend if you move them over — "
        f"and the good news is your resources are already copied to {cust['target_cloud_label']}, so "
        f"the switch is one click with no downtime and no data loss.\n\n"
        f"Would you be open to a 15-minute call this week to walk through it?\n\n"
        f"Best,\nThe Aiven Cost City team"
    )
    return {"subject": subject, "body": body, "model": "template (no LLM key)"}


def parse_llm_email(text: str, cust: dict) -> dict:
    """Lenient parse: prefer JSON, else split a 'Subject:' line from the body."""
    text = text.strip()
    # try to locate a JSON object even if the model wrapped it in prose / code fences
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(text[start:end + 1])
            subj = str(obj.get("subject", "")).strip()
            body = str(obj.get("body", "")).strip()
            if subj and body:
                return {"subject": subj, "body": body}
        except (json.JSONDecodeError, ValueError):
            pass
    # fallback: 'Subject: ...' first line, remainder is body
    lines = text.splitlines()
    if lines and lines[0].lower().startswith("subject:"):
        return {"subject": lines[0].split(":", 1)[1].strip(), "body": "\n".join(lines[1:]).strip()}
    return {
        "subject": f"Cut your {cust['current_cloud_label']} bill {cust['discount_pct']}%",
        "body": text,
    }


async def featherless_draft(cust: dict) -> dict:
    payload = {
        "model": FEATHERLESS_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": build_user_prompt(cust)},
        ],
        "temperature": 0.7,
        "max_tokens": 600,
    }
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{FEATHERLESS_URL}/chat/completions",
            headers={"Authorization": f"Bearer {FEATHERLESS_KEY}",
                     "Content-Type": "application/json"},
            json=payload,
        )
    if resp.status_code != 200:
        raise HTTPException(502, f"Featherless error {resp.status_code}: {resp.text[:300]}")
    content = resp.json()["choices"][0]["message"]["content"]
    out = parse_llm_email(content, cust)
    out["subject"] = strip_emoji(out["subject"])
    out["body"] = strip_emoji(out["body"])
    out["model"] = FEATHERLESS_MODEL
    return out


# ============================ ROUTES ============================

class DraftReq(BaseModel):
    customer_id: str


class SendReq(BaseModel):
    customer_id: str
    subject: str
    body: str


@app.get("/api/config")
def config():
    return {
        "llm": "featherless" if FEATHERLESS_KEY else "template",
        "llm_model": FEATHERLESS_MODEL if FEATHERLESS_KEY else None,
        "send_live": bool(RESEND_KEY),
        "mail_to": MAIL_TO,
        "mail_from": MAIL_FROM,
    }


@app.get("/api/customers")
def customers():
    return load_customers()


@app.get("/api/conflicts")
async def conflicts(hours: int = gdelt_conflicts.DEFAULT_HOURS,
                    radius_km: float = gdelt_conflicts.DEFAULT_RADIUS_KM):
    """Live GDELT conflict overlay for the map: geolocated material-conflict events
    plus a per-datacenter proximity-risk summary. Cached ~20 min. Degrades to an empty
    overlay if GDELT is unreachable, so the map keeps working."""
    try:
        return await gdelt_conflicts.get_conflicts(hours=hours, radius_km=radius_km)
    except Exception as e:  # noqa: BLE001 — never let a GDELT hiccup break the map
        return JSONResponse({
            "ok": False,
            "error": f"GDELT fetch failed: {e}",
            "window_hours": hours, "radius_km": radius_km,
            "total_conflicts": 0, "plotted": 0,
            "conflicts": {"type": "FeatureCollection", "features": []},
            "datacenters": [],
        })


@app.post("/api/draft")
async def draft(req: DraftReq):
    cust = find_customer(req.customer_id)
    out = template_draft(cust) if not FEATHERLESS_KEY else await featherless_draft(cust)
    out["customer_id"] = cust["id"]
    return out


def body_to_html(cust: dict, body: str) -> str:
    esc = (body.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
           .replace("\n", "<br>"))
    return (
        '<div style="font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;'
        'font-size:15px;line-height:1.6;color:#1a1a1a;max-width:560px">'
        f"{esc}"
        '<hr style="border:none;border-top:1px solid #eee;margin:20px 0">'
        f'<p style="font-size:12px;color:#999">Demo send via Cost City — originally addressed to '
        f"{cust['contact']} &lt;{cust['email']}&gt; at {cust['company']}. "
        f"Synthetic data; spend figures are relative units.</p></div>"
    )


@app.post("/api/send")
def send(req: SendReq):
    cust = find_customer(req.customer_id)
    html = body_to_html(cust, req.body)
    if not RESEND_KEY:
        return JSONResponse({
            "status": "dry-run",
            "detail": "No RESEND_API_KEY set — email NOT sent. Showing what would be delivered.",
            "to": MAIL_TO, "from": MAIL_FROM, "subject": req.subject,
        })
    import resend
    resend.api_key = RESEND_KEY
    try:
        result = resend.Emails.send({
            "from": MAIL_FROM,
            "to": [MAIL_TO],
            "subject": req.subject,
            "html": html,
            "reply_to": MAIL_TO,
        })
    except Exception as e:  # noqa: BLE001 — surface provider error to the UI
        raise HTTPException(502, f"Resend error: {e}")
    return {"status": "sent", "id": result.get("id"), "to": MAIL_TO, "subject": req.subject}


def _serve(path: Path, hint: str) -> str:
    if not path.exists():
        raise HTTPException(503, f"{path.name} missing — {hint}")
    return path.read_text()


@app.get("/", response_class=HTMLResponse)
def index():
    # the tabbed shell that frames both the Grid (cost city) and Map views
    return _serve(INDEX, "run build_multi.py / build_geomap.py first.")


@app.get("/cost_city.html", response_class=HTMLResponse)
def cost_city():
    return _serve(HTML, "run build_multi.py first.")


@app.get("/geo_distribution.html", response_class=HTMLResponse)
def geo_distribution():
    return _serve(GEO, "run build_geomap.py first.")


@app.get("/customers.html", response_class=HTMLResponse)
def customers_page():
    return _serve(CUSTOMERS_HTML, "customers.html missing.")


def main():
    ap = argparse.ArgumentParser(description="Cost City backend")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--reload", action="store_true")
    args = ap.parse_args()

    print(f"  LLM drafting : {'Featherless (' + FEATHERLESS_MODEL + ')' if FEATHERLESS_KEY else 'TEMPLATE fallback (set FEATHERLESS_API_KEY for AI)'}")
    print(f"  Email send   : {'Resend LIVE → ' + MAIL_TO if RESEND_KEY else 'DRY-RUN (set RESEND_API_KEY to send)'}")
    print(f"  Open         : http://{args.host}:{args.port}/\n")

    import uvicorn
    uvicorn.run("server:app" if args.reload else app, host=args.host, port=args.port,
                reload=args.reload)


if __name__ == "__main__":
    main()
