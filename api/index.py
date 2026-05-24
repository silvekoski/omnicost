"""Vercel serverless entrypoint for the email API.

Vercel has no always-on backend, so the rest of Omnicost is served as static
files from the CDN (see vercel.json). The three endpoints that genuinely need a
server — /api/config, /api/draft (Featherless), /api/send (Resend) — are routed
here and handled by reusing the FastAPI app from server.py. The @vercel/python
runtime detects the module-level `app` (ASGI) and serves it, passing the full
original request path, so server.py's existing /api/* routes match unchanged.

Notably NOT routed here: /api/conflicts. It stays a 404 on Vercel so the map
falls back to the baked conflicts.json snapshot, exactly as designed.

API keys (RESEND_API_KEY, FEATHERLESS_API_KEY, MAIL_TO, ...) are read from the
environment — set them as Project → Environment Variables in the Vercel
dashboard, never client-side.
"""

import os
import sys

# server.py lives at the repo root, one level up from this api/ directory.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from server import app  # noqa: E402  (re-exported so Vercel finds the ASGI app)

__all__ = ["app"]
