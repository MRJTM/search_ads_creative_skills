"""Creative agent: every turn is a real model call; deterministic replies are gone.

Turn flow (provider available):
1. Compact system prompt (chat policy + tool names + product facts + compact
   material summary) + bounded session history + current user message ->
   ``ToApisClient.chat(tools=AGENT_TOOLS, max_tokens=AGENT_MAX_TOKENS)``.
   Tool descriptions live only in the ``tools`` schema, not duplicated in the
   system prompt, so plain chat is not slowed down by a 55s-scale prompt.
2. No tool call -> the model content is the chit-chat reply, nothing executes.
3. Valid whitelisted tool call -> run the matching project skill, update
   CreativeState, echo the assistant ``tool_calls`` plus ``role=tool`` results,
   then ask the model once more with tools disabled for the final reply. If
   that final call fails, retry once with a shorter prompt; a further failure
   returns an explicit error that reports the real tool status (honest, never
   impersonating the model).
4. First model call failure / timeout / malformed response -> NO skill runs,
   ``detect_intent`` is not called, the creative stays untouched and the turn
   returns an explicit service error (trace: ``provider=error``). The legacy
   deterministic keyword-routing fallback was removed on purpose: a template
   must never be presented as an agent answer.

Material understanding is layered and mostly precomputed:
- catalog ``analysis`` (written by ``scripts/precompute_material_analysis.py``)
  is loaded into the per-session cache by the backend (L1, cross-session);
- the per-session cache (L2) is reused across turns and bounded by the backend;
- only images missing everywhere are analyzed once in parallel by the vision
  model (L3) and stored back into the session cache;
- traces distinguish precomputed / session_cache / parallel with hit/miss
  counts and never contain base64 payloads.
"""
import json
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from agent.config import settings
from agent.models import (
    CarouselPlanInput,
    ChatRequest,
    ChatResponse,
    CoverOptimizeInput,
    CreativeState,
    MaterialAnalysis,
    MaterialImage,
    TitleRewriteResult,
    TraceStep,
)
from agent.providers import ToApisClient
from agent import skills_core

_BANNED_TITLE_WORDS = {"free", "sale", "discount", "bestseller", "no.1", "cheapest"}

# Multi-turn context: bounded by both message count and total characters so a
# few very long turns cannot stretch the prompt (and the latency) again.
MAX_HISTORY_MESSAGES = 12
MAX_HISTORY_CHARS = 6000

# Prompt-size guards for the compact system prompt.
MAX_PROMPT_IMAGES = 8
COMPACT_ANALYSIS_CHARS = 90

# Shorter retry prompt for the final model turn.
FINAL_RETRY_TOOL_RESULT_CHARS = 600
FINAL_RETRY_USER_CHARS = 400
FINAL_RETRY_SYSTEM_PROMPT = (
    "The tool already ran. Summarize its result for the user in 1-2 short "
    "sentences, in the same language as the user. Do not invent details."
)


def _compact_tool_result_for_retry(raw: str) -> str:
    """Return a small *valid JSON* tool result for the final-turn retry.

    Cutting a JSON string at an arbitrary character made the retry payload
    malformed. Keep only what the model needs to acknowledge the action, then
    progressively shrink optional text while preserving valid JSON.
    """
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        payload = {"ok": False, "error": "invalid_tool_result"}

    compact: Dict[str, Any] = {"ok": bool(payload.get("ok"))}
    for key in (
        "tool",
        "title",
        "cover_image_id",
        "reason",
        "overall_idea",
        "error",
        "provider",
    ):
        if key in payload:
            compact[key] = payload[key]
    if isinstance(payload.get("slides"), list):
        compact["slides"] = payload["slides"][:3]

    encoded = json.dumps(compact, ensure_ascii=False)
    if len(encoded) <= FINAL_RETRY_TOOL_RESULT_CHARS:
        return encoded

    for key in ("reason", "overall_idea", "title", "error"):
        value = compact.get(key)
        if isinstance(value, str):
            compact[key] = value[:100]
    compact.pop("slides", None)
    encoded = json.dumps(compact, ensure_ascii=False)
    if len(encoded) <= FINAL_RETRY_TOOL_RESULT_CHARS:
        return encoded

    minimal = {"ok": compact["ok"]}
    for key in ("tool", "cover_image_id", "title", "error"):
        if key in compact:
            value = compact[key]
            minimal[key] = value[:80] if isinstance(value, str) else value
    return json.dumps(minimal, ensure_ascii=False)

SERVICE_ERROR_ZH = (
    "大模型服务当前不可用（超时或返回异常），本轮没有执行任何操作，创意保持不变。"
    "这不是模型回答，请稍后重试。"
)
SERVICE_ERROR_EN = (
    "The model service is currently unavailable (timeout or invalid response). "
    "No action was executed this turn and the creative is unchanged. "
    "This is not a model answer; please retry later."
)

TOOL_REWRITE_TITLE = "rewrite_title"
TOOL_OPTIMIZE_COVER = "optimize_cover"
TOOL_GENERATE_CAROUSEL = "generate_carousel"
ALLOWED_TOOLS = (TOOL_REWRITE_TITLE, TOOL_OPTIMIZE_COVER, TOOL_GENERATE_CAROUSEL)

AGENT_TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": TOOL_REWRITE_TITLE,
            "description": (
                "Run the project skill 'search-ads-query-title-rewrite': rewrite the "
                "product ad title aligned with the search query using catalog facts "
                "only (10-30 English words, no unverifiable promotional claims). Call "
                "only when the user explicitly asks to rewrite/optimize the title. "
                "Pass requested_title only when the user dictated the exact new title "
                "(e.g. 标题改成xxx)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query / user intent to align the title with.",
                    },
                    "requested_title": {
                        "type": "string",
                        "description": "Exact new title dictated by the user, if any.",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_OPTIMIZE_COVER,
            "description": (
                "Run the project skill 'search-ads-query-cover-optimize': select the "
                "best cover image from the existing material library by color intent, "
                "role, tags and precomputed visual analysis. Call only when the user "
                "explicitly asks to change/optimize the cover. It can only choose "
                "among existing images; it cannot generate or edit pixels."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "User intent, e.g. 换成蓝色封面 / blue cover.",
                    }
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": TOOL_GENERATE_CAROUSEL,
            "description": (
                "Run the project skill 'search-ads-query-carousel-generate': plan a "
                "3-5 slide carousel that reuses/edits existing assets or proposes "
                "generated slides, one selling point per slide with overlay copy. "
                "Call only when the user explicitly asks for a carousel plan. It "
                "returns a plan, not final rendered images."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "User intent for the carousel narrative.",
                    }
                },
                "required": ["query"],
            },
        },
    },
]


def _is_chinese(text: str) -> bool:
    return bool(re.search(r"[一-鿿]", text))


def _provider_title(
    client: ToApisClient, query: str, original_title: str, facts: List[str]
) -> Optional[TitleRewriteResult]:
    """Ask the provider for a rewritten title; validate strictly."""
    if not client.available:
        return None
    payload = client.chat_json(
        [
            {
                "role": "system",
                "content": (
                    "You rewrite e-commerce product titles. Return strict JSON with keys: "
                    "diagnosis (str), strategy (str), title (str), self_check (object). "
                    "The title must be 10-30 English words and must not contain fabricated "
                    "promotional claims (e.g. free, sale, discount, bestseller, no.1, cheapest)."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Query: {query}\nOriginal title: {original_title}\n"
                    f"Facts: {facts}"
                ),
            },
        ],
        max_tokens=settings.AGENT_MAX_TOKENS,
    )
    if not isinstance(payload, dict):
        return None
    try:
        result = TitleRewriteResult(**payload)
    except Exception:
        return None
    words = result.title.split()
    if not 10 <= len(words) <= 30:
        return None
    if _BANNED_TITLE_WORDS & {w.lower().strip(".,!?") for w in words}:
        return None
    return result


def build_system_prompt(
    state: CreativeState,
    material_analysis: Optional[Dict[int, MaterialAnalysis]] = None,
) -> str:
    """Compact system prompt: chat policy, tool names, product facts, materials.

    Tool descriptions are intentionally NOT repeated here (they ride in the
    ``tools`` schema), plain chat must stay 1-3 sentences, and each material is
    one compact line so the prompt stays small and fast.
    """
    facts = "; ".join(state.facts[:6]) if state.facts else "(none)"
    analyses = material_analysis if isinstance(material_analysis, dict) else {}
    lines: List[str] = []
    for img in state.images[:MAX_PROMPT_IMAGES]:
        line = "- id=%s role=%s colors=%s" % (
            img.id,
            img.role,
            "/".join(img.colors) or "-",
        )
        analysis = analyses.get(img.id) or img.analysis
        if analysis is not None and analysis.has_content:
            line += " | %s" % analysis.summary_line(COMPACT_ANALYSIS_CHARS)
        lines.append(line)
    image_lines = "\n".join(lines) if lines else "- (no images)"
    cover_id = state.cover_image_id if state.cover_image_id is not None else "unset"
    return (
        "You are QueryCraft, the creative assistant for one product's search ads "
        "(title, cover image, carousel).\n"
        "Chat policy: for greetings and small talk answer naturally in 1-3 short "
        "sentences in the user's language and call no tool. For a bare greeting "
        "such as 你好/hello, reply with only a brief greeting; do not introduce "
        "your name, the product, or your capabilities unless asked.\n"
        "When a structured answer helps, use well-formed GitHub-Flavored "
        "Markdown (leave a blank line before and after lists and tables) and "
        "never output raw HTML; for brief small talk keep a single natural "
        "sentence with no forced Markdown.\n"
        "Tools (full schema sent separately): rewrite_title, optimize_cover, "
        "generate_carousel. Call at most one, and only when the user clearly asks "
        "to execute that creative task, including a short confirmation of an "
        "earlier discussion.\n"
        "Product facts (only source of truth): title=%s; facts=%s; price=%s; "
        "sku=%s; cover id=%s.\n"
        "Materials:\n%s\n"
        "Never invent product facts, prices, sizes, materials, colors or image "
        "content. When a tool runs you receive its result as a tool message; "
        "summarize briefly and concretely what changed."
        % (
            state.title,
            facts,
            state.price if state.price is not None else "unset",
            state.sku if state.sku is not None else "unset",
            cover_id,
            image_lines,
        )
    )


def sanitize_history(
    history: Optional[Sequence[Any]],
    max_messages: int = MAX_HISTORY_MESSAGES,
    max_chars: int = MAX_HISTORY_CHARS,
) -> List[Dict[str, str]]:
    """Keep only plain user/assistant turns, bounded by count AND total chars."""
    if not history:
        return []
    cleaned: List[Dict[str, str]] = []
    for item in history:
        if not isinstance(item, dict):
            continue
        role = item.get("role")
        content = item.get("content")
        if role in ("user", "assistant") and isinstance(content, str) and content.strip():
            cleaned.append({"role": role, "content": content})
    if max_messages and len(cleaned) > max_messages:
        cleaned = cleaned[-max_messages:]
    total = sum(len(m["content"]) for m in cleaned)
    while cleaned and max_chars and total > max_chars:
        dropped = cleaned.pop(0)
        total -= len(dropped["content"])
    return cleaned


# --------------------------------------------------------------- tool plumbing

def _normalize_tool_call(call: Any) -> Optional[Dict[str, Any]]:
    """Reduce a provider tool_call to {"id", "name", "arguments"(raw)}; None if malformed."""
    if not isinstance(call, dict):
        return None
    function = call.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    if not isinstance(name, str) or not name:
        return None
    arguments = function.get("arguments")
    if arguments is None:
        arguments = ""
    if not isinstance(arguments, (str, dict)):
        return None
    call_id = call.get("id")
    return {
        "id": call_id if isinstance(call_id, str) and call_id else "call_0",
        "name": name,
        "arguments": arguments,
    }


def _parse_tool_arguments(raw: Any) -> Optional[Dict[str, Any]]:
    """Accept a JSON string or an already-decoded dict; None when invalid."""
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except Exception:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def _select_tool_call(
    normalized: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    """Pick the first valid whitelist call; report everything skipped."""
    selected: Optional[Dict[str, Any]] = None
    skipped: List[Dict[str, Any]] = []
    for call in normalized:
        name = call["name"]
        if name not in ALLOWED_TOOLS:
            skipped.append({"id": call["id"], "name": name, "reason": "unknown tool; not executed"})
            continue
        args = _parse_tool_arguments(call["arguments"])
        if args is None:
            skipped.append({"id": call["id"], "name": name, "reason": "invalid JSON arguments; not executed"})
            continue
        query = args.get("query")
        if not isinstance(query, str) or not query.strip():
            skipped.append({"id": call["id"], "name": name, "reason": "missing required query argument; not executed"})
            continue
        if selected is None:
            selected = {"id": call["id"], "name": name, "arguments": args}
        else:
            skipped.append({"id": call["id"], "name": name, "reason": "only one tool call is executed per turn"})
    return selected, skipped


def _ensure_unique_tool_call_ids(calls: List[Dict[str, Any]]) -> None:
    """Make provider-supplied call ids unique for a valid tool-result replay.

    A few OpenAI-compatible gateways omit ids or reuse the same id for
    parallel calls.  Each assistant tool call must have exactly one matching
    ``role=tool`` response, so normalize collisions before selection/replay.
    """
    seen = set()
    for index, call in enumerate(calls):
        call_id = call.get("id")
        if not isinstance(call_id, str) or not call_id or call_id in seen:
            candidate = f"call_{index}"
            suffix = 1
            while candidate in seen:
                candidate = f"call_{index}_{suffix}"
                suffix += 1
            call["id"] = candidate
        seen.add(call["id"])


def _echo_tool_call(call: Dict[str, Any]) -> Dict[str, Any]:
    """OpenAI wire format for replaying an assistant tool_call."""
    arguments = call["arguments"]
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": call["id"],
        "type": "function",
        "function": {"name": call["name"], "arguments": arguments},
    }


def _tool_status(tool: str, state: CreativeState) -> str:
    """Honest, factual status of what a tool already changed (error paths only)."""
    if tool == TOOL_REWRITE_TITLE:
        return f"title='{state.title}'"
    if tool == TOOL_OPTIMIZE_COVER:
        return f"cover_image_id={state.cover_image_id}"
    if tool == TOOL_GENERATE_CAROUSEL:
        return f"carousel_slides={len(state.carousel)}"
    return "no_change"


def _final_failure_reply(tool: str, state: CreativeState, chinese: bool) -> str:
    """Explicit failure notice after the final model turn failed twice.

    This is deliberately an error message, never a fabricated model answer:
    it reports the real state change produced by the already-executed tool.
    """
    status = _tool_status(tool, state)
    if chinese:
        return (
            f"模型生成最终回复失败（服务超时或异常），这条消息不是模型回答。"
            f"已执行状态：{status}。请重试本轮以获取模型总结。"
        )
    return (
        "The model failed to generate the final reply (service timeout or error); "
        f"this message is not a model answer. Actual status: {status}. "
        "Please retry this turn for the model summary."
    )


# ------------------------------------------------- material analysis resolution

def _cache_entry_for(
    img: MaterialImage, cache: Dict[int, MaterialAnalysis]
) -> Optional[MaterialAnalysis]:
    """Precomputed/session cache first; catalog analysis on the image second."""
    entry = cache.get(img.id)
    if isinstance(entry, MaterialAnalysis) and entry.has_content:
        return entry
    if img.analysis is not None and img.analysis.has_content:
        analysis = img.analysis.model_copy()
        analysis.source = "precomputed"
        cache[img.id] = analysis
        return analysis
    return None


def _material_insights(analyses: Dict[int, MaterialAnalysis]) -> Dict[str, Any]:
    """Compact per-image insights fed back into tool results / model context."""
    insights: Dict[str, Any] = {}
    for image_id, analysis in sorted(analyses.items()):
        insights[str(image_id)] = {
            "summary": analysis.summary_line(200),
            "selling_points": analysis.selling_points[:4],
            "risks": analysis.risks[:3],
        }
    return insights


def _resolve_material_analyses(
    client: ToApisClient,
    images: List[MaterialImage],
    cache: Optional[Dict[int, MaterialAnalysis]],
    trace: List[TraceStep],
) -> Dict[int, MaterialAnalysis]:
    """Layered image understanding: cache hit -> parallel vision for misses only.

    Appends one ``material_analysis`` trace step distinguishing precomputed /
    session_cache / parallel with hit/miss counts. No base64 ever enters it.
    """
    cache = cache if isinstance(cache, dict) else {}
    usable = [img for img in images if isinstance(img, MaterialImage)]
    if not usable:
        return {}

    found: Dict[int, MaterialAnalysis] = {}
    missing: List[MaterialImage] = []
    for img in usable:
        entry = _cache_entry_for(img, cache)
        if entry is None:
            missing.append(img)
        else:
            found[img.id] = entry

    hits_before_parallel = len(found)
    precomputed_hits = sum(
        1 for a in found.values() if a.source == "precomputed"
    )
    session_hits = hits_before_parallel - precomputed_hits

    parallel_ok_ids: List[int] = []
    parallel_attempted = False
    if missing and client.available:
        parallel_attempted = True
        try:
            parallel_results = client.analyze_materials_parallel(missing) or {}
        except Exception:
            parallel_results = {}
        for image_id, analysis in parallel_results.items():
            if isinstance(analysis, MaterialAnalysis) and analysis.has_content:
                cache[image_id] = analysis
                found[image_id] = analysis
                parallel_ok_ids.append(image_id)

    sources: List[str] = []
    if precomputed_hits:
        sources.append("precomputed")
    if session_hits:
        sources.append("session_cache")
    if parallel_ok_ids:
        sources.append("parallel")
    if not sources:
        sources.append("none")

    trace.append(
        TraceStep(
            step="material_analysis",
            detail="hits=%d/%d misses=%d source=%s"
            % (len(found), len(usable), len(missing), "+".join(sources)),
            data={
                "provider": "toapis" if parallel_attempted else "cache",
                "source": "+".join(sources),
                "cache_hits": hits_before_parallel,
                "cache_misses": len(missing),
                "parallel_analyzed": len(parallel_ok_ids),
                "parallel_failures": len(missing) - len(parallel_ok_ids),
                "image_count": len(usable),
                "image_summaries": {
                    str(image_id): analysis.summary_line(120)
                    for image_id, analysis in sorted(found.items())
                },
            },
        )
    )
    return found


class Orchestrator:
    def __init__(self, client: Optional[ToApisClient] = None) -> None:
        self.client = client or ToApisClient()

    # ------------------------------------------------------------------ entry
    def handle(
        self,
        req: ChatRequest,
        state: CreativeState,
        history: Optional[List[Dict[str, str]]] = None,
        material_analysis_cache: Optional[Dict[int, MaterialAnalysis]] = None,
    ) -> ChatResponse:
        """Serve one turn.

        ``history`` and ``material_analysis_cache`` are optional for backward
        compatibility. The cache is mutated in place so parallel vision results
        survive into later turns of the same session. When the first model call
        fails, no skill runs and an explicit service error is returned.
        """
        trace: List[TraceStep] = []
        prior_turns = sanitize_history(history)
        cache = (
            material_analysis_cache
            if isinstance(material_analysis_cache, dict)
            else {}
        )
        reply: Optional[str] = None
        try:
            reply = self._agent_turn(req, state, prior_turns, cache, trace)
        except Exception:
            trace.append(
                TraceStep(
                    step="agent_error",
                    detail="unexpected agent turn failure",
                    data={"provider": "error"},
                )
            )
        if reply is not None:
            return ChatResponse(reply=reply, creative=state, trace=trace)

        # No usable model reply: explicit service error. No deterministic
        # routing, no skill execution, no fabricated agent answer.
        if not any(
            s.step in ("agent", "agent_error") and s.data.get("provider") == "error"
            for s in trace
        ):
            trace.append(
                TraceStep(
                    step="agent",
                    detail="model turn produced no usable reply",
                    data={"provider": "error", "reason": "unusable model output"},
                )
            )
        trace.append(
            TraceStep(
                step="final_reply",
                detail="service error",
                data={"provider": "error", "service_error": True},
            )
        )
        return ChatResponse(
            reply=SERVICE_ERROR_ZH if _is_chinese(req.message) else SERVICE_ERROR_EN,
            creative=state,
            trace=trace,
        )

    # ------------------------------------------------------------ agent loop
    def _chat_final(self, messages: List[Dict[str, Any]]) -> Optional[str]:
        """Final natural-language turn (tools disabled, capped length)."""
        try:
            final = self.client.chat(messages, max_tokens=settings.FINAL_MAX_TOKENS)
        except Exception:
            return None
        if not isinstance(final, dict):
            return None
        content = final.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        return None

    def _agent_turn(
        self,
        req: ChatRequest,
        state: CreativeState,
        history: List[Dict[str, str]],
        cache: Dict[int, MaterialAnalysis],
        trace: List[TraceStep],
    ) -> Optional[str]:
        """One provider-driven turn. None => caller returns a service error."""
        messages: List[Dict[str, Any]] = [
            {
                "role": "system",
                "content": build_system_prompt(state, material_analysis=cache),
            }
        ]
        messages.extend(history)
        messages.append({"role": "user", "content": req.message})

        try:
            assistant = self.client.chat(
                messages, tools=AGENT_TOOLS, max_tokens=settings.AGENT_MAX_TOKENS
            )
        except Exception:
            assistant = None
        if not isinstance(assistant, dict):
            trace.append(
                TraceStep(
                    step="agent",
                    detail="model call failed or timed out",
                    data={
                        "provider": "error",
                        "model": settings.AGENT_MODEL,
                        "tool_call": False,
                    },
                )
            )
            return None
        content = assistant.get("content")
        content = content if isinstance(content, str) else ""
        raw_calls = assistant.get("tool_calls")
        raw_calls = raw_calls if isinstance(raw_calls, list) else []

        if not raw_calls:
            if content.strip():
                trace.append(
                    TraceStep(
                        step="agent",
                        detail="provider chat reply",
                        data={
                            "provider": "toapis",
                            "model": settings.AGENT_MODEL,
                            "tool_call": False,
                            "history_messages": len(history),
                            "max_tokens": settings.AGENT_MAX_TOKENS,
                        },
                    )
                )
                reply = content.strip()
                trace.append(
                    TraceStep(
                        step="final_reply",
                        detail=reply[:200],
                        data={"provider": "toapis"},
                    )
                )
                return reply
            trace.append(
                TraceStep(
                    step="agent",
                    detail="model reply was empty (malformed)",
                    data={"provider": "error", "model": settings.AGENT_MODEL},
                )
            )
            return None

        normalized = [c for c in (_normalize_tool_call(c) for c in raw_calls) if c]
        _ensure_unique_tool_call_ids(normalized)
        selected, skipped = _select_tool_call(normalized)
        if selected is None:
            trace.append(
                TraceStep(
                    step="agent",
                    detail="tool_call rejected; nothing executed",
                    data={
                        "provider": "toapis",
                        "model": settings.AGENT_MODEL,
                        "tool_call": True,
                        "rejected_tool_calls": skipped,
                    },
                )
            )
            return None  # nothing executed; caller returns an explicit error

        query = str(selected["arguments"].get("query", "")).strip()
        trace.append(
            TraceStep(
                step="agent",
                detail=f"tool_call {selected['name']}",
                data={
                    "provider": "toapis",
                    "model": settings.AGENT_MODEL,
                    "tool_call": True,
                    "history_messages": len(history),
                    "max_tokens": settings.AGENT_MAX_TOKENS,
                },
            )
        )
        trace.append(
            TraceStep(
                step="tool_call",
                detail=f"{selected['name']}(query={query})",
                data={
                    "tool": selected["name"],
                    "call_id": selected["id"],
                    "arguments": selected["arguments"],
                    "skipped_tool_calls": skipped,
                },
            )
        )
        result_payload = self._execute_tool(selected, state, trace, cache)

        # Echo the assistant tool_calls plus role=tool results, then ask for the
        # final natural-language reply with tools disabled (single follow-up).
        messages.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": [_echo_tool_call(c) for c in normalized],
            }
        )
        tool_contents: List[Tuple[str, str]] = []
        skipped_ids = {info["id"] for info in skipped}
        for call in normalized:
            if call["id"] == selected["id"]:
                tool_content = json.dumps(result_payload, ensure_ascii=False)
            elif call["id"] in skipped_ids:
                tool_content = json.dumps(
                    {
                        "ok": False,
                        "error": "not_executed",
                        "reason": "only the first valid whitelist tool call is executed per turn",
                    },
                    ensure_ascii=False,
                )
            else:
                continue
            messages.append(
                {"role": "tool", "tool_call_id": call["id"], "content": tool_content}
            )
            tool_contents.append((call["id"], tool_content))

        final = self._chat_final(messages)
        if final is not None:
            trace.append(
                TraceStep(
                    step="final_reply",
                    detail=final[:200],
                    data={"provider": "toapis"},
                )
            )
            return final

        # One retry with a much shorter prompt (bounded tool result, no replayed
        # history). If it still fails we report an explicit, honest error.
        retry_messages: List[Dict[str, Any]] = [
            {"role": "system", "content": FINAL_RETRY_SYSTEM_PROMPT},
            {"role": "user", "content": req.message[:FINAL_RETRY_USER_CHARS]},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [_echo_tool_call(c) for c in normalized],
            },
        ]
        for call_id, tool_content in tool_contents:
            retry_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call_id,
                    "content": _compact_tool_result_for_retry(tool_content),
                }
            )
        final = self._chat_final(retry_messages)
        if final is not None:
            trace.append(
                TraceStep(
                    step="final_reply",
                    detail=final[:200],
                    data={"provider": "toapis", "retried_short_prompt": True},
                )
            )
            return final

        chinese = _is_chinese(req.message)
        reply = _final_failure_reply(selected["name"], state, chinese)
        trace.append(
            TraceStep(
                step="final_reply",
                detail=reply[:200],
                data={
                    "provider": "error",
                    "reason": "final model turn failed after one short retry",
                },
            )
        )
        return reply

    # ---------------------------------------------------------- skill router
    def _execute_tool(
        self,
        selected: Dict[str, Any],
        state: CreativeState,
        trace: List[TraceStep],
        cache: Optional[Dict[int, MaterialAnalysis]] = None,
    ) -> Dict[str, Any]:
        """Run the whitelisted skill, mutate CreativeState, return tool payload.

        Skill failures are reported to the model as ``ok:false`` tool results
        instead of raising; the final reply always comes from the model (or is
        an explicit error).
        """
        name = selected["name"]
        args = selected["arguments"]
        query = str(args.get("query", "")).strip()
        cache = cache if isinstance(cache, dict) else {}

        if name == TOOL_REWRITE_TITLE:
            requested = args.get("requested_title")
            requested = requested.strip() if isinstance(requested, str) else ""
            if requested:
                state.title = requested
                trace.append(
                    TraceStep(
                        step="title_rewrite",
                        detail=requested,
                        data={"provider": "direct", "tool": name},
                    )
                )
                return {
                    "ok": True,
                    "tool": name,
                    "title": state.title,
                    "provider": "direct",
                    "note": "Title set verbatim from the user's requested title.",
                }
            result: Optional[TitleRewriteResult] = None
            if self.client.available:
                result = _provider_title(self.client, query, state.title, state.facts)
            if result is None:
                trace.append(
                    TraceStep(
                        step="title_rewrite",
                        detail="provider failed or returned an invalid title",
                        data={"provider": "error", "tool": name},
                    )
                )
                return {"ok": False, "tool": name, "error": "title_provider_failed"}
            provider = "toapis"
            trace.append(
                TraceStep(
                    step="title_rewrite",
                    detail=result.title,
                    data={"provider": provider, "tool": name, "self_check": result.self_check},
                )
            )
            state.title = result.title
            return {
                "ok": True,
                "tool": name,
                "title": state.title,
                "provider": provider,
                "self_check": result.self_check,
            }

        if name == TOOL_OPTIMIZE_COVER:
            analyses = _resolve_material_analyses(self.client, state.images, cache, trace)
            try:
                result_cover = skills_core._run_async(
                    skills_core.skill_cover_optimize(
                        CoverOptimizeInput(
                            query=query,
                            images=state.images,
                            current_cover_id=state.cover_image_id,
                        )
                    )
                )
            except Exception as exc:
                trace.append(
                    TraceStep(
                        step="cover_optimize",
                        detail="skill failed",
                        data={"provider": "error", "error": str(exc)[:120]},
                    )
                )
                return {"ok": False, "tool": name, "error": "skill_execution_failed"}
            state.cover_image_id = result_cover.selected_image_id
            trace.append(
                TraceStep(
                    step="cover_optimize",
                    detail=result_cover.reason,
                    data={"provider": "offline", "scores": result_cover.scores},
                )
            )
            return {
                "ok": True,
                "tool": name,
                "cover_image_id": result_cover.selected_image_id,
                "reason": result_cover.reason,
                "selling_points": result_cover.selling_points,
                "material_insights": _material_insights(analyses),
                "provider": "offline",
            }

        if name == TOOL_GENERATE_CAROUSEL:
            analyses = _resolve_material_analyses(self.client, state.images, cache, trace)
            try:
                result_plan = skills_core._run_async(
                    skills_core.skill_carousel_plan(
                        CarouselPlanInput(
                            query=query,
                            images=state.images,
                            current_cover_id=state.cover_image_id,
                        )
                    )
                )
            except Exception as exc:
                trace.append(
                    TraceStep(
                        step="carousel_plan",
                        detail="skill failed",
                        data={"provider": "error", "error": str(exc)[:120]},
                    )
                )
                return {"ok": False, "tool": name, "error": "skill_execution_failed"}
            state.carousel = result_plan.items
            trace.append(
                TraceStep(
                    step="carousel_plan",
                    detail=result_plan.overall_idea,
                    data={"provider": "offline"},
                )
            )
            return {
                "ok": True,
                "tool": name,
                "slides": [
                    {
                        "order": it.order,
                        "source_type": it.source_type,
                        "image_id": it.image_id,
                        "overlay_copy": it.overlay_copy,
                    }
                    for it in result_plan.items
                ],
                "overall_idea": result_plan.overall_idea,
                "material_insights": _material_insights(analyses),
                "provider": "offline",
            }

        # Defensive: selection already whitelists, never execute unknown tools.
        return {"ok": False, "tool": name, "error": "unknown_tool_not_executed"}
