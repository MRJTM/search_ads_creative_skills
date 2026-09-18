import json
import os
import threading
import time
from collections import OrderedDict
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional, Tuple

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse

from agent import providers
from agent.config import settings
from agent.models import (
    ChatRequest,
    ChatResponse,
    CreativeState,
    MaterialAnalysis,
    MaterialImage,
)
from agent.orchestrator import MAX_HISTORY_MESSAGES, Orchestrator

app = FastAPI(title="Creative Agent Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "http://127.0.0.1:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = Orchestrator()

# Bounded session stores. Sessions are LRU-evicted beyond MAX_SESSIONS so the
# dictionaries can never grow without limit.
MAX_SESSIONS = 100
# Upper bound for one session's material-analysis cache (catalog images plus a
# generous margin for runtime-analyzed additions).
MAX_SESSION_MATERIAL_ANALYSES = 64
# Per-message history cap; replay is bounded again in the orchestrator.
MAX_HISTORY_MESSAGE_CHARS = 2000

_sessions: "OrderedDict[str, CreativeState]" = OrderedDict()
_histories: Dict[str, List[Dict[str, str]]] = {}
_material_caches: "OrderedDict[str, Dict[int, MaterialAnalysis]]" = OrderedDict()
_store_lock = threading.RLock()
# One gate per live session. Requests for different sessions remain concurrent,
# while turns for the same session cannot interleave state/history/cache writes.
_session_locks: Dict[str, threading.Lock] = {}
_session_lock_users: Dict[str, int] = {}


def _supabase_public_url(filename: str) -> str:
    """Public URL of one object in the materials bucket on Supabase Storage."""
    base = (settings.SUPABASE_URL or "").rstrip("/")
    return (
        f"{base}/storage/v1/object/public/"
        f"{settings.SUPABASE_STORAGE_BUCKET}/{filename}"
    )


# Remote catalog cache: material assets live in Supabase Storage when
# SUPABASE_URL is configured (Vercel has no local materials directory), so the
# catalog is fetched from the bucket's public URL and reused for a short TTL.
_REMOTE_CATALOG_TTL_SECONDS = 300.0
_catalog_cache: Optional[Tuple[float, dict]] = None


def _load_catalog() -> dict:
    global _catalog_cache
    if settings.SUPABASE_URL:
        now = time.time()
        if _catalog_cache and now - _catalog_cache[0] < _REMOTE_CATALOG_TTL_SECONDS:
            return _catalog_cache[1]
        try:
            resp = providers.get_http_client().get(
                _supabase_public_url("catalog.json"), timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception:
            data = None
        if data is not None:
            _catalog_cache = (now, data)
            return data
        if _catalog_cache:
            # Serve the last good copy rather than failing the request.
            return _catalog_cache[1]
        # No remote copy yet (and none cached): fall through to the local file
        # so local development without uploaded assets keeps working.
    with open(settings.CATALOG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def _catalog_analyses(catalog: dict) -> Dict[int, MaterialAnalysis]:
    """Material analyses precomputed offline into the catalog (cache level L1)."""
    analyses: Dict[int, MaterialAnalysis] = {}
    for raw in catalog.get("images", []):
        if not isinstance(raw, dict):
            continue
        image_id = raw.get("id")
        payload = raw.get("analysis")
        if not isinstance(image_id, int) or not isinstance(payload, dict):
            continue
        try:
            analysis = MaterialAnalysis(**payload)
        except Exception:
            continue
        if not analysis.has_content:
            continue
        # Anything loaded from the catalog was produced offline; normalize the
        # provenance so traces can distinguish it from runtime session entries.
        analysis.source = "precomputed"
        analyses[image_id] = analysis
    return analyses


def _initial_state(catalog: dict) -> CreativeState:
    prod = catalog["product"]
    return CreativeState(
        title=prod["original_title"],
        facts=prod["facts"],
        price=prod["price"],
        sku=prod["sku"],
        cover_image_id=catalog.get("initial_cover_image_id"),
        images=[MaterialImage(**i) for i in catalog["images"]],
    )


def _evict_old_sessions() -> None:
    """Keep at most MAX_SESSIONS sessions (LRU across all three stores)."""
    while len(_sessions) > MAX_SESSIONS:
        # Never evict a session while a model/tool turn is in flight. A burst
        # may temporarily exceed the bound until one active request finishes.
        oldest = next(
            (
                session_id
                for session_id in _sessions
                if _session_lock_users.get(session_id, 0) == 0
            ),
            None,
        )
        if oldest is None:
            break
        _sessions.pop(oldest, None)
        _histories.pop(oldest, None)
        _material_caches.pop(oldest, None)
        _session_locks.pop(oldest, None)


@contextmanager
def _session_guard(session_id: str) -> Iterator[None]:
    """Serialize one session without blocking unrelated sessions."""
    with _store_lock:
        lock = _session_locks.setdefault(session_id, threading.Lock())
        _session_lock_users[session_id] = _session_lock_users.get(session_id, 0) + 1
    lock.acquire()
    try:
        yield
    finally:
        lock.release()
        with _store_lock:
            remaining = _session_lock_users.get(session_id, 1) - 1
            if remaining > 0:
                _session_lock_users[session_id] = remaining
            else:
                _session_lock_users.pop(session_id, None)
                if session_id not in _sessions:
                    _session_locks.pop(session_id, None)
            _evict_old_sessions()


def _touch_session(session_id: str) -> None:
    # Only the OrderedDict stores participate in LRU ordering.
    for mapping in (_sessions, _material_caches):
        if session_id in mapping:
            mapping.move_to_end(session_id)


def _get_session(session_id: str) -> CreativeState:
    if session_id not in _sessions:
        _sessions[session_id] = _initial_state(_load_catalog())
    _sessions.move_to_end(session_id)
    _evict_old_sessions()
    return _sessions[session_id]


def _get_material_cache(session_id: str) -> Dict[int, MaterialAnalysis]:
    """Per-session material analysis cache, seeded from the catalog (L1 -> L2)."""
    cache = _material_caches.get(session_id)
    if cache is None:
        cache = _catalog_analyses(_load_catalog())
        _material_caches[session_id] = cache
    else:
        _material_caches.move_to_end(session_id)
    _evict_old_sessions()
    return cache


def _bound_material_cache(cache: Dict[int, MaterialAnalysis]) -> None:
    """Keep one session's cache bounded (drop the oldest entries first)."""
    while len(cache) > MAX_SESSION_MATERIAL_ANALYSES:
        cache.pop(next(iter(cache)))


@app.on_event("shutdown")
def _close_http_pool() -> None:
    providers.close_http_client()


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "model": settings.AGENT_MODEL,
        "vision_model": settings.VISION_MODEL,
    }


@app.get("/api/materials")
def materials() -> dict:
    return _load_catalog()


@app.get("/api/skills")
def skills() -> List[dict]:
    return [
        {
            "id": "search-ads-query-title-rewrite",
            "name": "Title Rewrite",
            "description": "Rewrite the product title aligned with query intent, facts only, 10-30 English words.",
        },
        {
            "id": "search-ads-query-cover-optimize",
            "name": "Cover Optimize",
            "description": "Select the best cover image by color intent, role, tags and aesthetics.",
        },
        {
            "id": "search-ads-query-carousel-generate",
            "name": "Carousel Plan",
            "description": "Plan a 3-5 slide carousel with reuse/edit/generate actions and overlay copy.",
        },
    ]


def _record_history(session_id: str, user_message: str, assistant_reply: str) -> None:
    """Append one turn, capped per message and bounded to MAX_HISTORY_MESSAGES."""
    if session_id not in _sessions:
        # A response for an already-evicted generation must not resurrect only
        # its history without the matching creative state.
        return
    turns = _histories.setdefault(session_id, [])
    turns.append(
        {"role": "user", "content": user_message[:MAX_HISTORY_MESSAGE_CHARS]}
    )
    turns.append(
        {"role": "assistant", "content": assistant_reply[:MAX_HISTORY_MESSAGE_CHARS]}
    )
    if len(turns) > MAX_HISTORY_MESSAGES:
        del turns[: len(turns) - MAX_HISTORY_MESSAGES]
    _touch_session(session_id)


@app.post("/api/agent/chat", response_model=ChatResponse)
def chat(req: ChatRequest) -> ChatResponse:
    with _session_guard(req.session_id):
        with _store_lock:
            state = _get_session(req.session_id)
            # Replay the bounded session history so short follow-ups such as
            # "好的，帮我做一下" still resolve against the earlier discussion.
            history = list(_histories.get(req.session_id, []))
            # Session material-analysis cache: precomputed entries from the catalog
            # plus everything the vision model analyzed in earlier turns.
            material_cache = _get_material_cache(req.session_id)
        resp = orchestrator.handle(
            req,
            state,
            history=history,
            material_analysis_cache=material_cache,
        )
        with _store_lock:
            _record_history(req.session_id, req.message, resp.reply)
            cache = _material_caches.get(req.session_id)
            if cache is not None:
                _bound_material_cache(cache)
            _touch_session(req.session_id)
        return resp


@app.get("/materials/{filename}")
def material_file(filename: str):
    catalog = _load_catalog()
    allowed = {img["filename"] for img in catalog["images"]}
    # strict whitelist check; rejects traversal, absolute paths, etc.
    if filename not in allowed or "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=404, detail="Not found")
    if settings.SUPABASE_URL:
        # Assets live in Supabase Storage; send the browser straight to the
        # bucket's public URL (cached so repeat views skip this round-trip).
        return RedirectResponse(
            _supabase_public_url(filename),
            status_code=302,
            headers={"Cache-Control": "public, max-age=86400"},
        )
    path = os.path.abspath(os.path.join(settings.MATERIALS_DIR, filename))
    materials_root = os.path.abspath(settings.MATERIALS_DIR)
    if not path.startswith(materials_root + os.sep) or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="Not found")
    return FileResponse(path)
