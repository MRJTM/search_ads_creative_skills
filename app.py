"""Vercel entrypoint.

Deploys the FastAPI backend (webs/backend) as one serverless function that
serves the API routes (/api/*) and material images (/materials/{filename}).
The built frontend (webs/front/dist) is served as static files by Vercel and
proxied to this function via the rewrites in vercel.json, so the whole site
runs as a single Vercel project with same-origin API calls.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from webs.backend.main import app  # noqa: E402

# @vercel/python picks up either name; keep both for clarity.
handler = app
