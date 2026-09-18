import json
import os
import sys
import threading
import time
from collections import OrderedDict

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from agent.models import (  # noqa: E402
    CarouselPlanInput,
    CoverOptimizeInput,
    MaterialAnalysis,
    MaterialImage,
    TitleRewriteInput,
)
from agent.models import ChatRequest, CreativeState  # noqa: E402
from agent import providers  # noqa: E402
from agent import skills_core  # noqa: E402
from agent.config import settings  # noqa: E402
from agent.orchestrator import (  # noqa: E402
    AGENT_TOOLS,
    MAX_HISTORY_CHARS,
    MAX_HISTORY_MESSAGES,
    Orchestrator,
    build_system_prompt,
    sanitize_history,
)
from agent.providers import ToApisClient  # noqa: E402
import webs.backend.main as backend_main  # noqa: E402
from webs.backend.main import app, _load_catalog, _initial_state  # noqa: E402

client = TestClient(app)

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
MATERIALS_DIR = os.path.join(REPO_ROOT, "tmp", "materials")


def catalog_images():
    return [MaterialImage(**i) for i in _load_catalog()["images"]]


def _state():
    # Most unit tests exercise the runtime-miss path explicitly. Keep that
    # fixture independent from the real demo catalog, which is intentionally
    # populated with offline precomputed analyses.
    images = [img.model_copy(update={"analysis": None}) for img in catalog_images()]
    return CreativeState(
        title=_load_catalog()["product"]["original_title"],
        facts=_load_catalog()["product"]["facts"],
        images=images,
    )


def _analyzed_state(source="precomputed"):
    """State whose images carry catalog-style precomputed analysis."""
    state = _state()
    state.images = [
        img.model_copy(
            update={
                "analysis": MaterialAnalysis(
                    description=f"{img.role} asset {img.id}",
                    selling_points=[f"{img.role} look"],
                    source=source,
                )
            }
        )
        for img in state.images
    ]
    return state


# ---------------------------------------------------------------------------
# Deterministic offline skills (unchanged behavior)


def test_title_word_count_and_no_fake_promo():
    cat = _load_catalog()
    res = skills_core._run_async(
        skills_core.skill_title_rewrite(
            TitleRewriteInput(
                query="elegant ivory dress for summer",
                original_title=cat["product"]["original_title"],
                facts=cat["product"]["facts"],
            )
        )
    )
    words = res.title.split()
    assert 10 <= len(words) <= 30
    banned = {"free", "sale", "discount", "bestseller", "no.1", "cheapest"}
    assert not banned & {w.lower() for w in words}


def test_generic_title_instruction_uses_offline_fallback_safely():
    cat = _load_catalog()
    res = skills_core._run_async(
        skills_core.skill_title_rewrite(
            TitleRewriteInput(
                query="优化标题",
                original_title=cat["product"]["original_title"],
                facts=cat["product"]["facts"],
            )
        )
    )
    assert 10 <= len(res.title.split()) <= 30
    assert res.self_check["facts_only"] is True


def test_blue_query_selects_blue_image():
    res = skills_core._run_async(
        skills_core.skill_cover_optimize(
            CoverOptimizeInput(query="换成蓝色封面", images=catalog_images(), current_cover_id=1)
        )
    )
    blue = next(i for i in catalog_images() if i.id == 3)
    assert blue.colors == ["blue"]
    assert res.selected_image_id == 3


def test_carousel_3_to_5_items():
    res = skills_core._run_async(
        skills_core.skill_carousel_plan(
            CarouselPlanInput(query="plan a carousel", images=catalog_images(), current_cover_id=1)
        )
    )
    assert 3 <= len(res.items) <= 5
    assert res.overall_idea
    assert res.image_understanding
    for it in res.items:
        assert it.source_type in ("reuse", "edit", "generate")
        assert it.overlay_copy


# ---------------------------------------------------------------------------
# Safety / path tests (kept from the original suite)


def test_path_traversal_blocked():
    assert client.get("/materials/..%2f..%2f.env").status_code in (403, 404)
    assert client.get("/materials/../catalog.json").status_code in (403, 404)
    assert client.get("/materials/nonexistent.png").status_code == 404


def test_health_and_materials():
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["vision_model"] == settings.VISION_MODEL
    data = client.get("/api/materials").json()
    assert len(data["images"]) == 5
    # every catalog image documents the optional analysis slot
    assert all("analysis" in img for img in data["images"])


def _tool_call(call_id, name, arguments):
    if not isinstance(arguments, str):
        arguments = json.dumps(arguments, ensure_ascii=False)
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


class FakeAgentClient(ToApisClient):
    """Offline fake of the tool-calling agent provider; never touches the network."""

    def __init__(self, script=None, title_payload=None, parallel_result=None):
        super().__init__(api_key="fake-agent-key-for-tests")
        self.script = list(script or [])
        self.title_payload = title_payload
        self.parallel_result = parallel_result
        self.chat_calls = []
        self.parallel_calls = []

    def chat(
        self,
        messages,
        tools=None,
        model=None,
        temperature=0.3,
        timeout=None,
        tool_choice=None,
        max_tokens=None,
    ):
        self.chat_calls.append(
            {"messages": [dict(m) for m in messages], "tools": tools, "max_tokens": max_tokens}
        )
        if self.script:
            return self.script.pop(0)
        return {"role": "assistant", "content": "ok", "tool_calls": []}

    def chat_json(self, messages, model=None, temperature=0.3, max_tokens=None):
        return self.title_payload

    def analyze_materials_parallel(self, images, materials_dir=None, workers=None, timeout=None):
        self.parallel_calls.append({"images": list(images), "workers": workers})
        results = {}
        for img in images:
            if callable(self.parallel_result):
                analysis = self.parallel_result(img)
            else:
                analysis = MaterialAnalysis(
                    description=f"{img.role} asset {img.id}",
                    selling_points=[f"{img.role} look"],
                    source="parallel",
                )
            results[img.id] = analysis
        return results


COVER_SCRIPT = [
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [_tool_call("call_1", "optimize_cover", {"query": "换成蓝色封面"})],
    },
    {"role": "assistant", "content": "已把封面切换到蓝色图片 3。", "tool_calls": []},
]

CAROUSEL_SCRIPT = [
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [_tool_call("call_c", "generate_carousel", {"query": "蓝色连衣裙轮播"})],
    },
    {"role": "assistant", "content": "已生成 4 页轮播计划。", "tool_calls": []},
]


# ---------------------------------------------------------------------------
# Plain chat: real agent reply only, no vision, no creative mutation


def test_agent_plain_chat_without_tool_call():
    reply_text = "这条象牙白连衣裙的蓝色版本很适合搜索场景，你想好先做哪一步了吗？"
    fake = FakeAgentClient(script=[{"role": "assistant", "content": reply_text, "tool_calls": []}])
    state = _state()
    original_title = state.title
    resp = Orchestrator(client=fake).handle(ChatRequest(message="聊聊那条 blue cloth"), state)
    assert resp.reply == reply_text
    # plain chat must not mutate the creative at all and must not call vision
    assert resp.creative.title == original_title
    assert resp.creative.cover_image_id is None
    assert resp.creative.carousel == []
    assert fake.parallel_calls == []
    assert len(fake.chat_calls) == 1
    agent_step = next(t for t in resp.trace if t.step == "agent")
    assert agent_step.data["provider"] == "toapis"
    assert agent_step.data["tool_call"] is False
    assert agent_step.data["max_tokens"] == settings.AGENT_MAX_TOKENS
    assert not [t for t in resp.trace if t.step == "tool_call"]
    # compact system prompt still carries the product context and tool names
    first_messages = fake.chat_calls[0]["messages"]
    assert first_messages[0]["role"] == "system"
    assert state.title in first_messages[0]["content"]
    for tool_name in ("rewrite_title", "optimize_cover", "generate_carousel"):
        assert tool_name in first_messages[0]["content"]


def test_agent_optimize_cover_tool_call_two_turns():
    fake = FakeAgentClient(script=COVER_SCRIPT)
    resp = Orchestrator(client=fake).handle(ChatRequest(message="换成蓝色封面"), _state())
    assert resp.creative.cover_image_id == 3
    assert resp.reply == "已把封面切换到蓝色图片 3。"
    assert len(fake.chat_calls) == 2
    # first turn offered exactly the three whitelisted tools with a token cap
    offered = {t["function"]["name"] for t in fake.chat_calls[0]["tools"]}
    assert offered == {"rewrite_title", "optimize_cover", "generate_carousel"}
    assert fake.chat_calls[0]["max_tokens"] == settings.AGENT_MAX_TOKENS
    # second turn: tools disabled, final summary capped
    assert fake.chat_calls[1]["tools"] is None
    assert fake.chat_calls[1]["max_tokens"] == settings.FINAL_MAX_TOKENS
    followup = fake.chat_calls[1]["messages"]
    echo = next(m for m in followup if m["role"] == "assistant" and m.get("tool_calls"))
    assert echo["tool_calls"][0]["function"]["name"] == "optimize_cover"
    tool_msgs = [m for m in followup if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    payload = json.loads(tool_msgs[0]["content"])
    assert payload["ok"] is True
    assert payload["cover_image_id"] == 3
    # image understanding genuinely participates in the tool result
    assert payload["material_insights"]["3"]["summary"]
    tool_step = next(t for t in resp.trace if t.step == "tool_call")
    assert tool_step.data["tool"] == "optimize_cover"
    assert next(t for t in resp.trace if t.step == "cover_optimize").data["provider"] == "offline"


def test_agent_rewrite_title_with_requested_title():
    fake = FakeAgentClient(
        script=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _tool_call(
                        "call_t",
                        "rewrite_title",
                        {"query": "把标题改成 Ivory Dream Midi Dress", "requested_title": "Ivory Dream Midi Dress"},
                    )
                ],
            },
            {"role": "assistant", "content": "标题已更新为 Ivory Dream Midi Dress。", "tool_calls": []},
        ]
    )
    resp = Orchestrator(client=fake).handle(
        ChatRequest(message="标题改成Ivory Dream Midi Dress"), _state()
    )
    assert resp.creative.title == "Ivory Dream Midi Dress"
    title_step = next(t for t in resp.trace if t.step == "title_rewrite")
    assert title_step.data["provider"] == "direct"


def test_provider_title_adopted_when_valid():
    fake = FakeAgentClient(
        script=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _tool_call("call_p", "rewrite_title", {"query": "elegant ivory dress"})
                ],
            },
            {"role": "assistant", "content": "标题已重写完成。", "tool_calls": []},
        ],
        title_payload={
            "diagnosis": "too generic",
            "strategy": "add scene and details",
            "title": "Elegant Ivory Sleeveless Midi Dress for Summer Wedding Guest Occasions",
            "self_check": {"ok": True},
        },
    )
    resp = Orchestrator(client=fake).handle(
        ChatRequest(message="优化标题 elegant ivory dress"), _state()
    )
    title_step = next(t for t in resp.trace if t.step == "title_rewrite")
    assert title_step.data["provider"] == "toapis"
    assert resp.creative.title == "Elegant Ivory Sleeveless Midi Dress for Summer Wedding Guest Occasions"


def test_provider_title_rejects_banned_promo_without_offline_success():
    fake = FakeAgentClient(
        script=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _tool_call("call_b", "rewrite_title", {"query": "elegant ivory dress"})
                ],
            },
            {"role": "assistant", "content": "标题已重写完成。", "tool_calls": []},
        ],
        title_payload={
            "diagnosis": "d",
            "strategy": "s",
            "title": "Best Free Sale Discount Ivory Dress Deal",
            "self_check": {},
        },
    )
    resp = Orchestrator(client=fake).handle(
        ChatRequest(message="优化标题 elegant ivory dress"), _state()
    )
    title_step = next(t for t in resp.trace if t.step == "title_rewrite")
    assert title_step.data["provider"] == "error"
    assert resp.creative.title != "Best Free Sale Discount Ivory Dress Deal"
    tool_msg = next(m for m in fake.chat_calls[1]["messages"] if m["role"] == "tool")
    assert json.loads(tool_msg["content"])["error"] == "title_provider_failed"
    # The user-facing explanation still comes from the final model turn.
    assert resp.reply == "标题已重写完成。"


# ---------------------------------------------------------------------------
# Precomputed / session-cached / parallel material analysis


def test_cover_with_precomputed_analysis_skips_vision():
    fake = FakeAgentClient(script=COVER_SCRIPT)
    resp = Orchestrator(client=fake).handle(
        ChatRequest(message="换成蓝色封面"), _analyzed_state()
    )
    # no vision call at all: catalog analysis served every image
    assert fake.parallel_calls == []
    step = next(t for t in resp.trace if t.step == "material_analysis")
    assert step.data["source"] == "precomputed"
    assert step.data["cache_hits"] == 5
    assert step.data["cache_misses"] == 0
    assert step.data["provider"] == "cache"
    assert step.data["parallel_analyzed"] == 0
    assert "base64" not in json.dumps(step.data)
    assert resp.creative.cover_image_id == 3
    # the precomputed insight still reaches the model via the tool result
    tool_msg = next(
        m for m in fake.chat_calls[1]["messages"] if m["role"] == "tool"
    )
    assert "cover asset 1" in tool_msg["content"]


def test_carousel_with_precomputed_analysis_skips_vision():
    fake = FakeAgentClient(script=CAROUSEL_SCRIPT)
    resp = Orchestrator(client=fake).handle(
        ChatRequest(message="生成轮播"), _analyzed_state()
    )
    assert fake.parallel_calls == []
    step = next(t for t in resp.trace if t.step == "material_analysis")
    assert step.data["source"] == "precomputed"
    assert step.data["cache_hits"] == 5
    assert 3 <= len(resp.creative.carousel) <= 5
    tool_msg = next(
        m for m in fake.chat_calls[1]["messages"] if m["role"] == "tool"
    )
    payload = json.loads(tool_msg["content"])
    assert payload["material_insights"]["2"]["summary"]


def test_missing_analysis_uses_parallel_provider_then_reuses():
    fake = FakeAgentClient(script=COVER_SCRIPT)
    session_cache: dict = {}
    resp = Orchestrator(client=fake).handle(
        ChatRequest(message="换成蓝色封面"),
        _state(),
        history=None,
        material_analysis_cache=session_cache,
    )
    # every catalog image lacks analysis -> one parallel vision call for all 5
    assert len(fake.parallel_calls) == 1
    assert len(fake.parallel_calls[0]["images"]) == 5
    step = next(t for t in resp.trace if t.step == "material_analysis")
    assert step.data["source"] == "parallel"
    assert step.data["cache_misses"] == 5
    assert step.data["parallel_analyzed"] == 5
    assert step.data["cache_hits"] == 0
    # results were stored in the session cache passed by the caller
    assert set(session_cache.keys()) == {1, 2, 3, 4, 5}
    assert all(a.has_content for a in session_cache.values())

    # next turn: session cache hit, no further vision call
    fake2 = FakeAgentClient(script=COVER_SCRIPT)
    resp2 = Orchestrator(client=fake2).handle(
        ChatRequest(message="再换个蓝色封面"),
        _state(),
        history=None,
        material_analysis_cache=session_cache,
    )
    assert fake2.parallel_calls == []
    step2 = next(t for t in resp2.trace if t.step == "material_analysis")
    assert step2.data["source"] == "session_cache"
    assert step2.data["cache_hits"] == 5
    assert step2.data["cache_misses"] == 0
    assert resp2.creative.cover_image_id == 3


def test_partial_cache_only_missing_images_go_parallel():
    fake = FakeAgentClient(script=COVER_SCRIPT)
    state = _analyzed_state()
    # only images 1-3 keep precomputed analysis; 4-5 must be analyzed in parallel
    state.images = state.images[:3] + _state().images[3:]
    session_cache: dict = {}
    resp = Orchestrator(client=fake).handle(
        ChatRequest(message="换成蓝色封面"),
        state,
        material_analysis_cache=session_cache,
    )
    assert len(fake.parallel_calls) == 1
    assert {img.id for img in fake.parallel_calls[0]["images"]} == {4, 5}
    step = next(t for t in resp.trace if t.step == "material_analysis")
    assert step.data["source"] == "precomputed+parallel"
    assert step.data["cache_hits"] == 3
    assert step.data["cache_misses"] == 2


def test_backend_catalog_analysis_no_vision_call(tmp_path, monkeypatch):
    # catalog with precomputed analysis: runtime never calls the vision provider
    catalog = _load_catalog()
    for img in catalog["images"]:
        img["analysis"] = {
            "description": f"{img['role']} precomputed look",
            "selling_points": [f"{img['role']} appeal"],
            "source": "precompute",
            "model": "vision-test",
        }
    catalog_path = tmp_path / "catalog.json"
    catalog_path.write_text(json.dumps(catalog, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(settings, "CATALOG_PATH", str(catalog_path))
    monkeypatch.setattr(backend_main, "_material_caches", OrderedDict())
    monkeypatch.setattr(backend_main, "_sessions", OrderedDict())
    fake = FakeAgentClient(script=COVER_SCRIPT)
    monkeypatch.setattr(backend_main, "orchestrator", Orchestrator(client=fake))

    r = client.post(
        "/api/agent/chat", json={"message": "换成蓝色封面", "session_id": "t-precat"}
    )
    assert r.status_code == 200
    body = r.json()
    assert fake.parallel_calls == []
    assert body["creative"]["cover_image_id"] == 3
    step = next(t for t in body["trace"] if t["step"] == "material_analysis")
    assert step["data"]["source"] == "precomputed"
    assert step["data"]["cache_hits"] == 5
    # the catalog analysis was seeded into the session cache
    seeded = backend_main._material_caches["t-precat"]
    assert set(seeded.keys()) == {1, 2, 3, 4, 5}
    assert all(a.source == "precomputed" for a in seeded.values())
    assert "base64" not in json.dumps(body["trace"])


def test_backend_missing_analysis_parallel_once_then_cached(monkeypatch):
    catalog = _load_catalog()
    for image in catalog["images"]:
        image["analysis"] = None
    monkeypatch.setattr(backend_main, "_load_catalog", lambda: catalog)
    monkeypatch.setattr(backend_main, "_material_caches", OrderedDict())
    monkeypatch.setattr(backend_main, "_sessions", OrderedDict())
    monkeypatch.setattr(backend_main, "_histories", {})
    fake = FakeAgentClient(
        script=COVER_SCRIPT + COVER_SCRIPT
    )
    monkeypatch.setattr(backend_main, "orchestrator", Orchestrator(client=fake))
    sid = "t-session-cache"
    r1 = client.post("/api/agent/chat", json={"message": "换成蓝色封面", "session_id": sid})
    assert r1.status_code == 200
    assert len(fake.parallel_calls) == 1
    assert len(fake.parallel_calls[0]["images"]) == 5
    cache = backend_main._material_caches[sid]
    assert len(cache) == 5

    r2 = client.post("/api/agent/chat", json={"message": "再换一次蓝色封面", "session_id": sid})
    assert r2.status_code == 200
    # session cache served the second turn: no additional vision call
    assert len(fake.parallel_calls) == 1
    step = next(
        t for t in r2.json()["trace"] if t["step"] == "material_analysis"
    )
    assert step["data"]["source"] == "session_cache"
    assert step["data"]["cache_hits"] == 5


def test_material_cache_entries_are_bounded():
    cache = {i: MaterialAnalysis(description=f"d{i}") for i in range(100)}
    backend_main._bound_material_cache(cache)
    assert len(cache) == backend_main.MAX_SESSION_MATERIAL_ANALYSES
    # oldest entries were dropped first
    assert 0 not in cache and 99 in cache


def test_session_store_is_bounded(monkeypatch):
    monkeypatch.setattr(backend_main, "_sessions", OrderedDict())
    monkeypatch.setattr(backend_main, "_histories", {})
    monkeypatch.setattr(backend_main, "_material_caches", OrderedDict())
    monkeypatch.setattr(backend_main, "_session_locks", {})
    monkeypatch.setattr(backend_main, "_session_lock_users", {})
    monkeypatch.setattr(backend_main, "orchestrator", Orchestrator(client=FakeAgentClient()))
    total = backend_main.MAX_SESSIONS + 5
    for i in range(total):
        r = client.post("/api/agent/chat", json={"message": f"hi {i}", "session_id": f"bound-{i}"})
        assert r.status_code == 200
    assert len(backend_main._sessions) == backend_main.MAX_SESSIONS
    assert len(backend_main._material_caches) == backend_main.MAX_SESSIONS
    assert len(backend_main._session_locks) == backend_main.MAX_SESSIONS
    assert "bound-0" not in backend_main._sessions
    assert f"bound-{total - 1}" in backend_main._sessions


def test_same_session_guard_serializes_and_missing_history_is_not_resurrected(monkeypatch):
    monkeypatch.setattr(backend_main, "_sessions", OrderedDict())
    monkeypatch.setattr(backend_main, "_histories", {})
    monkeypatch.setattr(backend_main, "_session_locks", {})
    monkeypatch.setattr(backend_main, "_session_lock_users", {})
    active = 0
    peak = 0
    state_lock = threading.Lock()

    def guarded_work():
        nonlocal active, peak
        with backend_main._session_guard("same-session"):
            with state_lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.03)
            with state_lock:
                active -= 1

    threads = [threading.Thread(target=guarded_work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert peak == 1

    backend_main._record_history("missing-session", "hello", "hi")
    assert "missing-session" not in backend_main._histories


# ---------------------------------------------------------------------------
# No-fake-answer policy: explicit service errors instead of template replies


def test_first_turn_provider_failure_returns_service_error():
    fake = FakeAgentClient(script=[None])  # first model call fails
    state = _state()
    original_title = state.title
    resp = Orchestrator(client=fake).handle(ChatRequest(message="换成蓝色封面"), state)
    # nothing executed, creative untouched
    assert resp.creative.cover_image_id is None
    assert resp.creative.title == original_title
    assert resp.creative.carousel == []
    assert not [
        t
        for t in resp.trace
        if t.step in ("tool_call", "cover_optimize", "carousel_plan", "material_analysis", "title_rewrite")
    ]
    assert len(fake.chat_calls) == 1
    # explicit error, never a fabricated agent answer
    assert "不是模型回答" in resp.reply
    agent_step = next(t for t in resp.trace if t.step == "agent")
    assert agent_step.data["provider"] == "error"
    final_step = next(t for t in resp.trace if t.step == "final_reply")
    assert final_step.data["provider"] == "error"
    assert final_step.data["service_error"] is True


def test_first_turn_empty_reply_is_treated_as_failure():
    fake = FakeAgentClient(script=[{"role": "assistant", "content": "", "tool_calls": []}])
    resp = Orchestrator(client=fake).handle(ChatRequest(message="你好"), _state())
    assert "不是模型回答" in resp.reply
    assert next(t for t in resp.trace if t.step == "agent").data["provider"] == "error"
    assert not [t for t in resp.trace if t.step == "tool_call"]


def test_provider_unavailable_returns_service_error_without_skills(monkeypatch):
    monkeypatch.setattr(backend_main, "orchestrator", Orchestrator(client=ToApisClient(api_key="")))
    r = client.post("/api/agent/chat", json={"message": "换成蓝色封面", "session_id": "t-nokey"})
    assert r.status_code == 200
    body = r.json()
    # initial cover from catalog, no skill touched anything
    assert body["creative"]["cover_image_id"] == 1
    assert not [t for t in body["trace"] if t["step"] in ("tool_call", "cover_optimize")]
    assert "不是模型回答" in body["reply"]
    assert any(
        t["data"].get("provider") == "error" for t in body["trace"] if t["step"] == "final_reply"
    )


def test_final_turn_failure_reports_error_not_local_summary():
    fake = FakeAgentClient(
        script=[
            COVER_SCRIPT[0],
            None,  # final natural-language turn fails
            None,  # short-prompt retry fails too
        ]
    )
    resp = Orchestrator(client=fake).handle(ChatRequest(message="换成蓝色封面"), _state())
    # the tool honestly executed; only the model summary failed
    assert resp.creative.cover_image_id == 3
    assert len(fake.chat_calls) == 3  # decision + final + one retry
    final_step = next(t for t in resp.trace if t.step == "final_reply")
    assert final_step.data["provider"] == "error"
    # explicit failure notice with the real status, not the old local summary
    assert "不是模型回答" in resp.reply
    assert "cover_image_id=3" in resp.reply
    assert not resp.reply.startswith("Cover switched to image 3")


def test_final_turn_retry_with_shorter_prompt_succeeds():
    fake = FakeAgentClient(
        script=[
            COVER_SCRIPT[0],
            None,  # full final prompt fails
            {"role": "assistant", "content": "已切换为蓝色封面。", "tool_calls": []},
        ]
    )
    resp = Orchestrator(client=fake).handle(ChatRequest(message="换成蓝色封面"), _state())
    assert resp.reply == "已切换为蓝色封面。"
    assert len(fake.chat_calls) == 3
    retry_call = fake.chat_calls[2]
    assert retry_call["tools"] is None
    assert retry_call["max_tokens"] == settings.FINAL_MAX_TOKENS
    retry_messages = retry_call["messages"]
    assert retry_messages[0]["role"] == "system"
    tool_msgs = [m for m in retry_messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    # the retry prompt carries a truncated tool result, not the full replay
    assert len(tool_msgs[0]["content"]) <= 600
    assert json.loads(tool_msgs[0]["content"])["ok"] is True
    final_step = next(t for t in resp.trace if t.step == "final_reply")
    assert final_step.data["provider"] == "toapis"
    assert final_step.data["retried_short_prompt"] is True


def test_agent_unknown_tool_not_executed():
    fake = FakeAgentClient(
        script=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [_tool_call("call_x", "delete_all_materials", {"query": "all"})],
            }
        ]
    )
    state = _state()
    original_title = state.title
    resp = Orchestrator(client=fake).handle(ChatRequest(message="删除所有素材"), state)
    # no second provider turn and no tool_call step: nothing executed
    assert len(fake.chat_calls) == 1
    assert fake.parallel_calls == []
    assert not [t for t in resp.trace if t.step == "tool_call"]
    assert resp.creative.title == original_title
    assert resp.creative.cover_image_id is None
    assert resp.creative.carousel == []
    reject = next(t for t in resp.trace if t.step == "agent")
    assert reject.data["provider"] == "toapis"
    assert any("unknown tool" in s["reason"] for s in reject.data["rejected_tool_calls"])
    # explicit service error, no fabricated offline answer
    assert "不是模型回答" in resp.reply
    assert next(t for t in resp.trace if t.step == "final_reply").data["provider"] == "error"


def test_agent_malformed_tool_arguments_not_executed():
    fake = FakeAgentClient(
        script=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_b",
                        "type": "function",
                        "function": {"name": "optimize_cover", "arguments": "not-json{"},
                    }
                ],
            }
        ]
    )
    resp = Orchestrator(client=fake).handle(ChatRequest(message="帮我把封面处理一下"), _state())
    assert len(fake.chat_calls) == 1
    assert not [t for t in resp.trace if t.step == "tool_call"]
    assert resp.creative.cover_image_id is None
    reject = next(t for t in resp.trace if t.step == "agent")
    assert any("invalid JSON arguments" in s["reason"] for s in reject.data["rejected_tool_calls"])
    assert "不是模型回答" in resp.reply


def test_agent_multiple_tool_calls_executes_only_first():
    fake = FakeAgentClient(
        script=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _tool_call("call_m1", "optimize_cover", {"query": "换成蓝色封面"}),
                    _tool_call("call_m2", "generate_carousel", {"query": "蓝色连衣裙轮播"}),
                ],
            },
            {
                "role": "assistant",
                "content": "封面已切换为图片 3；轮播计划本次未执行，需要的话再说一声。",
                "tool_calls": [],
            },
        ]
    )
    resp = Orchestrator(client=fake).handle(
        ChatRequest(message="换个蓝色封面，顺便做个轮播"), _state()
    )
    assert resp.creative.cover_image_id == 3
    assert resp.creative.carousel == []  # second call was skipped
    tool_step = next(t for t in resp.trace if t.step == "tool_call")
    assert len(tool_step.data["skipped_tool_calls"]) == 1
    assert tool_step.data["skipped_tool_calls"][0]["name"] == "generate_carousel"
    tool_msgs = [m for m in fake.chat_calls[1]["messages"] if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert json.loads(tool_msgs[0]["content"])["ok"] is True
    assert json.loads(tool_msgs[1]["content"])["ok"] is False


def test_agent_duplicate_tool_call_ids_are_normalized():
    fake = FakeAgentClient(
        script=[
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    _tool_call("call_0", "optimize_cover", {"query": "换成蓝色封面"}),
                    _tool_call("call_0", "generate_carousel", {"query": "蓝色连衣裙轮播"}),
                ],
            },
            {"role": "assistant", "content": "已切换蓝色封面。", "tool_calls": []},
        ]
    )
    resp = Orchestrator(client=fake).handle(ChatRequest(message="换成蓝色封面"), _state())
    assert resp.creative.cover_image_id == 3
    followup = fake.chat_calls[1]["messages"]
    echoed = next(m for m in followup if m["role"] == "assistant")["tool_calls"]
    tool_messages = [m for m in followup if m["role"] == "tool"]
    echoed_ids = [call["id"] for call in echoed]
    result_ids = [message["tool_call_id"] for message in tool_messages]
    assert len(set(echoed_ids)) == 2
    assert result_ids == echoed_ids
    assert json.loads(tool_messages[0]["content"])["ok"] is True
    assert json.loads(tool_messages[1]["content"])["ok"] is False


def test_agent_skill_exception_reported_to_model(monkeypatch):
    fake = FakeAgentClient(
        script=[
            COVER_SCRIPT[0],
            {"role": "assistant", "content": "封面选择服务暂时出错，素材未改动。", "tool_calls": []},
        ]
    )

    async def broken_cover(_inp):
        raise RuntimeError("skill failed")

    monkeypatch.setattr(skills_core, "skill_cover_optimize", broken_cover)
    resp = Orchestrator(client=fake).handle(ChatRequest(message="换成蓝色封面"), _state())
    assert resp.creative.cover_image_id is None
    # the failure was reported to the model, which produced the final reply
    assert resp.reply == "封面选择服务暂时出错，素材未改动。"
    tool_msg = next(m for m in fake.chat_calls[1]["messages"] if m["role"] == "tool")
    assert json.loads(tool_msg["content"])["ok"] is False
    assert json.loads(tool_msg["content"])["error"] == "skill_execution_failed"


# ---------------------------------------------------------------------------
# Session history


def test_session_history_replayed_and_confirmation_triggers_tool(monkeypatch):
    fake = FakeAgentClient(
        script=[
            {
                "role": "assistant",
                "content": "这条 blue cloth 版本的连衣裙很适合做蓝色系创意。",
                "tool_calls": [],
            },
            COVER_SCRIPT[0],
            {"role": "assistant", "content": "好的，已切换为蓝色封面（图片 3）。", "tool_calls": []},
        ]
    )
    monkeypatch.setattr(backend_main, "orchestrator", Orchestrator(client=fake))
    sid = "t-history"
    r1 = client.post(
        "/api/agent/chat", json={"message": "我想聊聊那条 blue cloth 连衣裙", "session_id": sid}
    )
    assert r1.status_code == 200
    r2 = client.post(
        "/api/agent/chat", json={"message": "好的，帮我做一下", "session_id": sid}
    )
    assert r2.status_code == 200
    assert r2.json()["creative"]["cover_image_id"] == 3
    # the second request's provider messages contain the previous user/assistant turn
    second_turn_messages = fake.chat_calls[1]["messages"]
    assert [m["role"] for m in second_turn_messages] == ["system", "user", "assistant", "user"]
    assert second_turn_messages[1]["content"] == "我想聊聊那条 blue cloth 连衣裙"
    assert second_turn_messages[2]["content"] == r1.json()["reply"]
    assert second_turn_messages[3]["content"] == "好的，帮我做一下"


def test_history_is_bounded_by_messages(monkeypatch):
    fake = FakeAgentClient()
    monkeypatch.setattr(backend_main, "orchestrator", Orchestrator(client=fake))
    sid = "t-history-cap"
    for i in range(7):
        r = client.post("/api/agent/chat", json={"message": f"msg {i}", "session_id": sid})
        assert r.status_code == 200
    turns = backend_main._histories[sid]
    assert len(turns) == MAX_HISTORY_MESSAGES
    last_messages = fake.chat_calls[-1]["messages"]
    # system + bounded history + current user message
    assert len(last_messages) == 1 + MAX_HISTORY_MESSAGES + 1
    assert last_messages[0]["role"] == "system"
    assert last_messages[-1]["role"] == "user"


def test_sanitize_history_bounded_by_messages_and_chars():
    # message-count bound keeps only the newest turns
    long_history = []
    for i in range(30):
        long_history.append({"role": "user", "content": f"u{i}"})
        long_history.append({"role": "assistant", "content": f"a{i}"})
    cleaned = sanitize_history(long_history)
    assert len(cleaned) == MAX_HISTORY_MESSAGES
    assert cleaned[-1]["content"] == "a29"

    # character bound drops whole oldest messages until under the cap
    fat_history = [
        {"role": "user", "content": "x" * 4000},
        {"role": "assistant", "content": "y" * 3000},
        {"role": "user", "content": "keep me"},
    ]
    cleaned2 = sanitize_history(fat_history)
    total = sum(len(m["content"]) for m in cleaned2)
    assert total <= MAX_HISTORY_CHARS
    assert cleaned2[-1]["content"] == "keep me"
    assert cleaned2[0]["content"].startswith("y")

    # non-chat roles and empty turns are dropped entirely
    noisy = [
        {"role": "system", "content": "sys"},
        {"role": "tool", "content": "tool"},
        {"role": "user", "content": "   "},
        {"role": "assistant", "content": "hi"},
    ]
    assert sanitize_history(noisy) == [{"role": "assistant", "content": "hi"}]


# ---------------------------------------------------------------------------
# Compact system prompt


def test_system_prompt_compact_but_informative():
    state = _state()
    prompt = build_system_prompt(state)
    assert state.title in prompt
    for img in state.images:
        assert f"id={img.id}" in prompt
        assert img.role in prompt
    for tool_name in ("rewrite_title", "optimize_cover", "generate_carousel"):
        assert tool_name in prompt
    # small talk must stay short; tool schemas are not duplicated in the prompt
    assert "1-3" in prompt
    assert "10-30 English words" not in prompt
    assert len(prompt) < 2200

    # precomputed analysis contributes a compact per-image line
    analyzed = _analyzed_state()
    prompt2 = build_system_prompt(analyzed, material_analysis={})
    assert "cover asset 1" in prompt2
    assert len(prompt2) < 2600


def test_agent_tools_schema_untouched():
    # the schema must stay: the model needs it to call tools autonomously
    names = {t["function"]["name"] for t in AGENT_TOOLS}
    assert names == {"rewrite_title", "optimize_cover", "generate_carousel"}
    for tool in AGENT_TOOLS:
        assert tool["function"]["parameters"]["type"] == "object"
        assert "query" in tool["function"]["parameters"]["properties"]


# ---------------------------------------------------------------------------
# Provider-level transport: shared pool, max_tokens, sanitization, JSON fences


class _FakeHttpResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


class _FakeHttpClient:
    """Duck-typed stand-in for the shared httpx.Client (records every POST)."""

    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {"url": url, "headers": headers, "payload": json, "timeout": timeout}
        )
        return _FakeHttpResponse(self.payload)


def test_shared_http_client_is_reused_and_closable():
    providers.close_http_client()
    first = providers.get_http_client()
    try:
        assert providers.get_http_client() is first
    finally:
        providers.close_http_client()
    second = providers.get_http_client()
    try:
        assert second is not first
    finally:
        providers.close_http_client()


def test_provider_chat_payload_contains_max_tokens_and_tools(monkeypatch):
    fake_http = _FakeHttpClient(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "c1",
                                "type": "function",
                                "function": {
                                    "name": "optimize_cover",
                                    "arguments": '{"query":"blue"}',
                                },
                            }
                        ],
                        "server_internal": "do-not-leak",
                    }
                }
            ]
        }
    )
    monkeypatch.setattr(providers, "get_http_client", lambda: fake_http)
    provider = ToApisClient(api_key="test-key")
    tools = [
        {
            "type": "function",
            "function": {
                "name": "optimize_cover",
                "parameters": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                },
            },
        }
    ]
    message = provider.chat(
        [{"role": "user", "content": "hi"}], tools=tools, max_tokens=512
    )
    assert message["role"] == "assistant"
    assert message["tool_calls"][0]["function"]["name"] == "optimize_cover"
    assert message["tool_calls"][0]["function"]["arguments"] == '{"query":"blue"}'
    assert "server_internal" not in message
    captured = fake_http.calls[-1]
    assert captured["payload"]["max_tokens"] == 512
    assert captured["payload"]["tools"] == tools
    assert captured["payload"]["tool_choice"] == "auto"
    assert captured["headers"]["Authorization"] == "Bearer test-key"
    assert "test-key" not in json.dumps(message)

    # without tools the payload disables tool calling entirely
    provider.chat([{"role": "user", "content": "final"}], max_tokens=128)
    assert "tools" not in fake_http.calls[-1]["payload"]
    assert "tool_choice" not in fake_http.calls[-1]["payload"]
    assert fake_http.calls[-1]["payload"]["max_tokens"] == 128

    # chat_json also forwards max_tokens and requests JSON output
    provider.chat_json([{"role": "user", "content": "json please"}], max_tokens=64)
    assert fake_http.calls[-1]["payload"]["max_tokens"] == 64
    assert fake_http.calls[-1]["payload"]["response_format"] == {"type": "json_object"}


def test_provider_chat_returns_none_on_malformed_or_error(monkeypatch):
    fake_http = _FakeHttpClient({"nope": 1})
    monkeypatch.setattr(providers, "get_http_client", lambda: fake_http)
    provider = ToApisClient(api_key="test-key")
    assert provider.chat([{"role": "user", "content": "hi"}]) is None
    assert provider.chat_json([{"role": "user", "content": "hi"}]) is None

    class _RaisingHttp:
        def post(self, *args, **kwargs):
            raise RuntimeError("boom")

    monkeypatch.setattr(providers, "get_http_client", lambda: _RaisingHttp())
    assert provider.chat([{"role": "user", "content": "hi"}]) is None
    # unavailable client short-circuits without any request
    assert ToApisClient(api_key="").chat([{"role": "user", "content": "hi"}]) is None


def test_extract_json_object_tolerates_fences_and_prose():
    assert providers._extract_json_object('```json\n{"a": 1}\n```') == {"a": 1}
    assert providers._extract_json_object('```\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    assert providers._extract_json_object('Sure! {"a": 1} hope that helps') == {"a": 1}
    assert providers._extract_json_object('{"a": 1}') == {"a": 1}
    assert providers._extract_json_object("no json at all") is None
    assert providers._extract_json_object("```json\n[1, 2]\n```") is None  # not an object
    assert providers._extract_json_object(None) is None


def test_analyze_image_parses_content_only_and_ignores_reasoning(monkeypatch):
    vision_json = json.dumps(
        {
            "description": "Ivory midi dress on a clean studio background",
            "product": "ivory midi dress",
            "colors": ["ivory"],
            "scene": "studio",
            "composition": "centered front view",
            "visible_details": ["waist seam", "hidden back zip"],
            "selling_points": ["flattering A-line silhouette"],
            "risks": ["plain background"],
        },
        ensure_ascii=False,
    )
    fake_http = _FakeHttpClient(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        # fenced JSON plus a reasoning field that must be ignored
                        "content": f"```json\n{vision_json}\n```",
                        "reasoning_content": "let me think step by step ...",
                    }
                }
            ]
        }
    )
    monkeypatch.setattr(providers, "get_http_client", lambda: fake_http)
    provider = ToApisClient(api_key="test-key")
    img = catalog_images()[0]
    analysis = provider.analyze_image(img, materials_dir=MATERIALS_DIR)
    assert analysis is not None
    assert analysis.description.startswith("Ivory midi dress")
    assert analysis.colors == ["ivory"]
    assert analysis.selling_points == ["flattering A-line silhouette"]
    assert analysis.source == "parallel"
    assert analysis.model == settings.VISION_MODEL
    sent = fake_http.calls[0]["payload"]
    assert sent["model"] == settings.VISION_MODEL
    assert sent["max_tokens"] == settings.VISION_MAX_TOKENS
    # the image was attached as a data URL and the prompt is query-independent
    assert "base64," in json.dumps(sent)
    user_parts = sent["messages"][1]["content"]
    assert any(p["type"] == "image_url" for p in user_parts)
    assert "query" not in json.dumps(sent["messages"]).lower()


def test_analyze_image_rejects_bad_paths_and_payloads(monkeypatch):
    fake_http = _FakeHttpClient(
        {"choices": [{"message": {"role": "assistant", "content": "not json"}}]}
    )
    monkeypatch.setattr(providers, "get_http_client", lambda: fake_http)
    provider = ToApisClient(api_key="test-key")
    # traversal filename never reaches the provider
    evil = MaterialImage(
        id=99, filename="../.env", role="cover", colors=[], tags=[], alt=""
    )
    assert provider.analyze_image(evil, materials_dir=MATERIALS_DIR) is None
    assert fake_http.calls == []
    # missing file
    ghost = MaterialImage(
        id=98, filename="does-not-exist.png", role="cover", colors=[], tags=[], alt=""
    )
    assert provider.analyze_image(ghost, materials_dir=MATERIALS_DIR) is None
    # unparseable vision output
    assert provider.analyze_image(catalog_images()[0], materials_dir=MATERIALS_DIR) is None


def test_analyze_materials_parallel_is_concurrent_and_keyed_by_id(monkeypatch):
    provider = ToApisClient(api_key="test-key")
    lock = threading.Lock()
    state = {"active": 0, "peak": 0}

    def fake_analyze(img, materials_dir=None, timeout=None):
        with lock:
            state["active"] += 1
            state["peak"] = max(state["peak"], state["active"])
        time.sleep(0.05)
        with lock:
            state["active"] -= 1
        return MaterialAnalysis(description=f"img{img.id}", source="parallel")

    monkeypatch.setattr(provider, "analyze_image", fake_analyze)
    images = catalog_images()
    results = provider.analyze_materials_parallel(images, workers=4)
    assert set(results.keys()) == {img.id for img in images}
    assert all(a.has_content for a in results.values())
    # requests actually overlapped
    assert state["peak"] > 1
    # failed images are simply absent, never exceptions
    def failing(img, materials_dir=None, timeout=None):
        if img.id % 2:
            raise RuntimeError("vision failed")
        return MaterialAnalysis(description=f"ok{img.id}")

    monkeypatch.setattr(provider, "analyze_image", failing)
    partial = provider.analyze_materials_parallel(images, workers=2)
    assert set(partial.keys()) == {img.id for img in images if img.id % 2 == 0}


def test_legacy_analyze_materials_still_returns_batch_shape(monkeypatch):
    provider = ToApisClient(api_key="test-key")

    def fake_parallel(images, materials_dir=None, workers=None, timeout=None):
        return {
            1: MaterialAnalysis(description="studio cover shot", scene="studio"),
            2: MaterialAnalysis(description="on-model full body"),
        }

    monkeypatch.setattr(provider, "analyze_materials_parallel", fake_parallel)
    result = provider.analyze_materials(query="ignored", images=catalog_images())
    assert result["overall"]
    assert result["images"]["1"]["summary"] == "studio cover shot"
    assert result["images"]["2"]["scene"] == ""
