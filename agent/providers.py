"""OpenAI-compatible client for ToAPIs. Never raises with the API key embedded.

Latency-oriented design:
- one process-wide, thread-safe ``httpx.Client`` (connection pool) instead of a
  fresh TLS handshake per request;
- single-image, query-independent ``analyze_image`` plus a parallel
  ``analyze_materials_parallel`` so per-image vision results can be cached and
  produced concurrently (wall-clock ~= slowest image, not the sum);
- ``max_tokens`` support on every chat entry point so long generations cannot
  stretch a turn indefinitely;
- a daemon-thread boundary keeps a hard wall-clock timeout per request.
"""
import base64
import io
import json
import queue
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import httpx
from PIL import Image

from agent.config import settings
from agent.models import MaterialAnalysis

# messages support OpenAI-compatible multimodal content parts.
ChatMessage = Dict[str, Any]

_ALLOWED_IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp"}
_MAX_MATERIAL_IMAGES = 5
_MAX_PARALLEL_WORKERS = 5
_PREVIEW_MAX_SIZE = 768

# ---------------------------------------------------------------------------
# Shared, thread-safe connection pool. httpx.Client is safe for concurrent
# requests; reusing it keeps TLS handshakes off the per-turn hot path.

_http_client_lock = threading.Lock()
_http_client: Optional[httpx.Client] = None


def get_http_client() -> httpx.Client:
    """Return the process-wide HTTP client (lazily created, thread-safe)."""
    global _http_client
    with _http_client_lock:
        if _http_client is None or _http_client.is_closed:
            _http_client = httpx.Client(
                proxy=settings.TOAPIS_PROXY_URL,
                timeout=httpx.Timeout(settings.TOPAPI_TIMEOUT_SECONDS),
                limits=httpx.Limits(
                    max_connections=16, max_keepalive_connections=8
                ),
            )
        return _http_client


def close_http_client() -> None:
    """Close the shared pool (tests / graceful shutdown). Safe to call twice."""
    global _http_client
    with _http_client_lock:
        client = _http_client
        _http_client = None
        if client is not None and not client.is_closed:
            client.close()


def _extract_json_object(text: Any) -> Optional[Dict[str, Any]]:
    """Parse a JSON object out of model output, tolerating markdown fences.

    Vision/chat models frequently wrap JSON in ```json ... ``` or prepend a
    short acknowledgement; only ``content`` is considered (reasoning_content
    is ignored by construction because callers pass content only).
    """
    if not isinstance(text, str) or not text.strip():
        return None
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", candidate)
        candidate = re.sub(r"\s*```\s*$", "", candidate)
    try:
        parsed = json.loads(candidate)
    except Exception:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            parsed = json.loads(candidate[start : end + 1])
        except Exception:
            return None
    return parsed if isinstance(parsed, dict) else None


def _as_str(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _as_str_list(value: Any, limit: int = 8) -> List[str]:
    if isinstance(value, str):
        items = [value]
    elif isinstance(value, (list, tuple)):
        items = [v for v in value if isinstance(v, str)]
    else:
        items = []
    cleaned = [v.strip() for v in items if v and v.strip()]
    return cleaned[:limit]


def _analysis_from_payload(payload: Dict[str, Any]) -> Optional[MaterialAnalysis]:
    """Coerce loose model JSON into MaterialAnalysis; None when unusable."""
    try:
        analysis = MaterialAnalysis(
            description=_as_str(payload.get("description")),
            product=_as_str(payload.get("product")),
            colors=_as_str_list(payload.get("colors")),
            scene=_as_str(payload.get("scene")),
            composition=_as_str(payload.get("composition")),
            visible_details=_as_str_list(payload.get("visible_details")),
            selling_points=_as_str_list(payload.get("selling_points")),
            risks=_as_str_list(payload.get("risks")),
            model=settings.VISION_MODEL,
            analyzed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            source="parallel",
        )
    except Exception:
        return None
    if not analysis.description:
        return None
    return analysis


def _image_identity(image: Any) -> Tuple[Optional[Any], Optional[str]]:
    """Extract (id, filename) from a MaterialImage or a plain dict."""
    filename = getattr(image, "filename", None)
    image_id = getattr(image, "id", None)
    if isinstance(image, dict):
        filename = filename if filename is not None else image.get("filename")
        image_id = image_id if image_id is not None else image.get("id")
    return image_id, filename if isinstance(filename, str) else None


def resolve_material_path(
    filename: Optional[str], materials_dir: Optional[str] = None
) -> Optional[Path]:
    """Strict path check: the file must live inside materials_dir."""
    if not filename:
        return None
    base_dir = Path(materials_dir or settings.MATERIALS_DIR).resolve()
    try:
        path = (base_dir / filename).resolve()
    except Exception:
        return None
    if base_dir not in path.parents and path != base_dir:
        return None
    if path.suffix.lower() not in _ALLOWED_IMAGE_TYPES:
        return None
    return path


def _image_data_url(path: Path, max_size: int = _PREVIEW_MAX_SIZE) -> Optional[str]:
    """In-memory downscaled JPEG data URL. The source file is never modified.

    The returned string must never be logged or put into a trace.
    """
    try:
        with Image.open(path) as source:
            preview = source.convert("RGB")
            preview.thumbnail((max_size, max_size))
            buffer = io.BytesIO()
            preview.save(buffer, format="JPEG", quality=82, optimize=True)
            data = buffer.getvalue()
    except Exception:
        return None
    return "data:%s;base64,%s" % (
        "image/jpeg",
        base64.b64encode(data).decode("ascii"),
    )


def _sanitize_assistant_message(message: Dict[str, Any]) -> ChatMessage:
    """Reduce a provider assistant message to the safe OpenAI wire subset."""
    content = message.get("content")
    sanitized: ChatMessage = {
        "role": "assistant",
        "content": content if isinstance(content, str) else "",
    }
    tool_calls: List[Dict[str, Any]] = []
    raw_calls = message.get("tool_calls")
    if isinstance(raw_calls, list):
        for call in raw_calls:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if not isinstance(function, dict):
                continue
            name = function.get("name")
            arguments = function.get("arguments", "")
            if not isinstance(name, str) or not name:
                continue
            if not isinstance(arguments, str):
                arguments = json.dumps(arguments, ensure_ascii=False)
            call_id = call.get("id")
            tool_calls.append(
                {
                    "id": call_id if isinstance(call_id, str) and call_id else "call_0",
                    "type": "function",
                    "function": {"name": name, "arguments": arguments},
                }
            )
    sanitized["tool_calls"] = tool_calls
    return sanitized


# Query-independent so results are reusable across every turn and query.
_VISION_SYSTEM_PROMPT = (
    "You are an e-commerce ad-creative image analyst. Analyze the given product "
    "material image and return STRICT JSON only (no markdown fences, no extra "
    "text) with exactly these keys: description (1-2 English sentences about what "
    "the image shows), product (the product depicted), colors (array of dominant "
    "color words), scene (setting/background), composition (framing, angle, "
    "crop), visible_details (array of notable visual details), selling_points "
    "(array of ad-relevant selling points actually visible), risks (array of "
    "visual weaknesses for ad use, may be empty). Never invent anything that is "
    "not visible."
)
_VISION_USER_PROMPT = (
    "Analyze this product material image for search-ad creative use. "
    "Respond with the JSON object only."
)


class ProviderError(Exception):
    pass


class ToApisClient:
    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        timeout: Optional[float] = None,
    ) -> None:
        self._api_key = api_key if api_key is not None else settings.TOAPIS_API_KEY
        self._base_url = (base_url or settings.TOAPIS_BASE_URL).rstrip("/")
        self._timeout = timeout if timeout is not None else settings.TOPAPI_TIMEOUT_SECONDS

    @property
    def available(self) -> bool:
        return bool(self._api_key) and "your" not in self._api_key.lower()

    def close(self) -> None:
        """Release the shared connection pool.

        All ToApisClient instances share one pool, so this affects every client
        in the process; intended for tests and graceful shutdown only.
        """
        close_http_client()

    def _post_chat_message(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.3,
        timeout: Optional[float] = None,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        response_format: Optional[Dict[str, Any]] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """POST /chat/completions and return the raw assistant message dict.

        Returns None on any failure (network, timeout, malformed body).
        Never raises, so the API key can never leak through an exception.
        """
        payload: Dict[str, Any] = {
            "model": model or settings.AGENT_MODEL,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None and max_tokens > 0:
            payload["max_tokens"] = max_tokens
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice or "auto"
        elif tool_choice:
            payload["tool_choice"] = tool_choice
        if response_format is not None:
            payload["response_format"] = response_format
        request_timeout = timeout if timeout is not None else self._timeout
        result_queue: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=1)

        def request() -> None:
            try:
                # Shared, pooled client: no fresh TLS handshake per turn.
                resp = get_http_client().post(
                    f"{self._base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                    json=payload,
                    timeout=httpx.Timeout(
                        request_timeout,
                        connect=min(3.0, request_timeout),
                        pool=min(3.0, request_timeout),
                    ),
                )
                resp.raise_for_status()
                message = resp.json()["choices"][0]["message"]
                result_queue.put(message if isinstance(message, dict) else None)
            except Exception:
                result_queue.put(None)

        # Some local gateways can hang below httpx's socket timeout (for
        # example during proxy/DNS negotiation). A daemon boundary guarantees
        # the caller gets an answer within the requested wall-clock time.
        worker = threading.Thread(target=request, daemon=True)
        worker.start()
        worker.join(request_timeout)
        if worker.is_alive():
            return None
        try:
            return result_queue.get_nowait()
        except queue.Empty:
            return None

    def chat_json(
        self,
        messages: List[ChatMessage],
        model: Optional[str] = None,
        temperature: float = 0.3,
        max_tokens: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """Return parsed JSON dict, or None on any failure (caller falls back)."""
        if not self.available:
            return None
        message = self._post_chat_message(
            messages,
            model=model,
            temperature=temperature,
            response_format={"type": "json_object"},
            max_tokens=max_tokens,
        )
        parsed = _extract_json_object(
            message.get("content") if isinstance(message, dict) else None
        )
        return parsed

    def chat(
        self,
        messages: List[ChatMessage],
        tools: Optional[List[Dict[str, Any]]] = None,
        model: Optional[str] = None,
        temperature: float = 0.3,
        timeout: Optional[float] = None,
        tool_choice: Optional[str] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[ChatMessage]:
        """OpenAI-compatible chat completion with native tool calling.

        Sends ``messages`` plus optional ``tools`` (omit ``tools`` to disable
        tool calling, e.g. for the final natural-language turn) and returns the
        sanitized assistant message::

            {"role": "assistant", "content": str, "tool_calls": [
                {"id": str, "type": "function",
                 "function": {"name": str, "arguments": str}}]}

        Returns None on any failure (unavailable, timeout, malformed response).
        Never raises and never leaks the API key or provider-internal fields.
        """
        if not self.available:
            return None
        message = self._post_chat_message(
            messages,
            model=model,
            temperature=temperature,
            timeout=timeout,
            tools=tools,
            tool_choice=tool_choice,
            max_tokens=max_tokens,
        )
        if not isinstance(message, dict):
            return None
        return _sanitize_assistant_message(message)

    # ------------------------------------------------------- vision analysis
    def analyze_image(
        self,
        image: Any,
        materials_dir: Optional[str] = None,
        timeout: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> Optional[MaterialAnalysis]:
        """Analyze one material image; query-independent so results cache well.

        Returns a MaterialAnalysis or None on any failure (unavailable, bad
        path, timeout, unparseable output). Never raises, never leaks the API
        key; the data URL stays inside the request payload and is never logged.
        """
        if not self.available:
            return None
        _image_id, filename = _image_identity(image)
        path = resolve_material_path(filename, materials_dir)
        if path is None or not path.is_file():
            return None
        data_url = _image_data_url(path)
        if not data_url:
            return None
        messages: List[ChatMessage] = [
            {"role": "system", "content": _VISION_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": _VISION_USER_PROMPT},
                    {"type": "image_url", "image_url": {"url": data_url}},
                ],
            },
        ]
        message = self._post_chat_message(
            messages,
            model=settings.VISION_MODEL,
            temperature=0.1,
            timeout=timeout if timeout is not None else settings.VISION_TIMEOUT_SECONDS,
            response_format={"type": "json_object"},
            # Vision output must not be truncated mid-JSON.
            max_tokens=max_tokens or settings.VISION_MAX_TOKENS,
        )
        content = message.get("content") if isinstance(message, dict) else None
        parsed = _extract_json_object(content)
        if parsed is None:
            return None
        return _analysis_from_payload(parsed)

    def analyze_materials_parallel(
        self,
        images: Sequence[Any],
        materials_dir: Optional[str] = None,
        workers: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> Dict[int, MaterialAnalysis]:
        """Analyze up to 5 missing images concurrently.

        Returns {image_id: MaterialAnalysis} containing only the successes so
        callers can cache exactly what worked. Never raises.
        """
        usable: List[Any] = []
        for image in list(images)[:_MAX_MATERIAL_IMAGES]:
            image_id, filename = _image_identity(image)
            if image_id is None or not filename:
                continue
            usable.append(image)
        if not usable:
            return {}
        if workers is None:
            workers = settings.MATERIAL_ANALYSIS_WORKERS
        workers = max(1, min(int(workers), _MAX_PARALLEL_WORKERS, len(usable)))
        results: Dict[int, MaterialAnalysis] = {}
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {}
            for image in usable:
                image_id, _filename = _image_identity(image)
                futures[
                    executor.submit(
                        self.analyze_image, image, materials_dir=materials_dir, timeout=timeout
                    )
                ] = image_id
            for future, image_id in futures.items():
                try:
                    analysis = future.result()
                except Exception:
                    analysis = None
                if isinstance(analysis, MaterialAnalysis) and analysis.has_content:
                    results[image_id] = analysis
        return results

    def analyze_materials(
        self,
        query: str,
        images: Sequence[Any],
        materials_dir: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        """Legacy batch analysis kept for backward compatibility.

        ``query`` is intentionally ignored: per-image analysis is
        query-independent so identical requests hit the caller's cache.
        Returns {"overall": str, "images": {image_id: {...}}} or None.
        """
        results = self.analyze_materials_parallel(images, materials_dir=materials_dir)
        if not results:
            return None
        images_payload: Dict[str, Any] = {}
        descriptions: List[str] = []
        for image_id, analysis in sorted(results.items()):
            summary = analysis.summary_line(160)
            descriptions.append(f"{image_id}: {summary}")
            images_payload[str(image_id)] = {
                "subject": analysis.product or summary,
                "scene": analysis.scene,
                "details": "; ".join(analysis.visible_details[:3]),
                "completeness": "",
                "aesthetics": analysis.composition,
                "summary": summary,
            }
        return {"overall": " | ".join(descriptions)[:800], "images": images_payload}

    def generate_image(
        self,
        prompt: str,
        model: Optional[str] = None,
        size: str = "1024x1024",
    ) -> Dict[str, Any]:
        """Return structured result: {"ok": bool, ...}. Never leaks the key."""
        if not self.available:
            return {"ok": False, "error": "image_provider_unavailable"}
        payload = {"model": model or settings.IMAGE_MODEL, "prompt": prompt, "size": size}
        try:
            resp = get_http_client().post(
                f"{self._base_url}/images/generations",
                headers={"Authorization": f"Bearer {self._api_key}"},
                json=payload,
                timeout=120.0,
            )
            resp.raise_for_status()
            data = resp.json()
            items = data.get("data") or []
            if not items:
                return {"ok": False, "error": "empty_image_response"}
            return {"ok": True, "image": items[0]}
        except Exception as exc:
            return {"ok": False, "error": ProviderError.__name__, "detail": str(exc)[:200]}
