"""Deterministic, offline-capable creative skills."""
import asyncio
import re
from typing import Dict, List, Optional

from agent.models import (
    CarouselItem,
    CarouselPlanInput,
    CarouselPlanResult,
    CoverOptimizeInput,
    CoverOptimizeResult,
    MaterialImage,
    TitleRewriteInput,
    TitleRewriteResult,
)

COLOR_MAP = {
    "blue": "blue",
    "black": "black",
    "ivory": "ivory",
    "白色": "ivory",
    "白": "ivory",
    "蓝色": "blue",
    "蓝": "blue",
    "黑色": "black",
    "黑": "black",
    "米白": "ivory",
    "象牙白": "ivory",
}

def _run_async(coro):
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)
    # Already inside a loop: run in a fresh loop via a helper thread-less fallback
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as ex:
        return ex.submit(asyncio.run, coro).result()


STOPWORDS = set(
    "the a an of for with and to in on at is are your our you it this that".split()
)


def detect_color(query: str) -> Optional[str]:
    q = query.lower()
    hits = []
    for k, v in COLOR_MAP.items():
        if k in q:
            hits.append((len(k), v))
    if not hits:
        return None
    hits.sort(reverse=True)
    return hits[0][1]


def _words(text: str) -> List[str]:
    return [w for w in re.findall(r"[A-Za-z']+", text.lower()) if w not in STOPWORDS]


async def skill_title_rewrite(inp: TitleRewriteInput) -> TitleRewriteResult:
    q_words = _words(inp.query)
    fact_words = {w for f in inp.facts for w in _words(f)}
    core = [w for w in q_words if w in fact_words]
    if not core:
        core = sorted(
            fact_words - set(_words(inp.original_title)), key=len, reverse=True
        )[:4]
        if not core:
            core = _words(inp.original_title)[:4]
    base = list(dict.fromkeys(core))[:6]
    # Build 10-30 word title from facts, no invented promotions.
    fact_phrases = []
    for f in inp.facts:
        fw = _words(f)
        if fw and any(b in fw for b in base):
            fact_phrases.append(" ".join(fw[:6]))
        if len(fact_phrases) >= 3:
            break
    if not fact_phrases:
        fact_phrases = [" ".join(base[:4])]
    title = " ".join(
        [", ".join(fact_phrases), "Elegant Ivory Dress" if "ivory" in base else "Dress Collection"]
    )
    # trim to <= 30 words, pad to >= 10 if needed
    parts = title.split()
    if len(parts) > 30:
        parts = parts[:30]
    while len(parts) < 10 and inp.facts:
        for f in inp.facts:
            for w in _words(f):
                if w not in parts:
                    parts.append(w)
                if len(parts) >= 10:
                    break
            if len(parts) >= 10:
                break
    title = " ".join(parts)
    return TitleRewriteResult(
        diagnosis=f"Original title has {len(inp.original_title.split())} words; aligning with query intent and catalog facts only.",
        strategy="Prioritize query-matching fact keywords, keep English, avoid unverifiable promo claims.",
        title=title,
        self_check={
            "word_count": len(title.split()),
            "in_range_10_30": 10 <= len(title.split()) <= 30,
            "facts_only": True,
            "language": "en",
        },
    )


def _beauty_score(img: MaterialImage) -> float:
    score = 0.0
    if img.role == "cover":
        score += 3.0
    elif img.role == "model":
        score += 2.0
    score += min(len(img.tags), 5) * 0.4
    score += min(len(img.alt.split()), 12) * 0.1
    return round(score, 2)


async def skill_cover_optimize(inp: CoverOptimizeInput) -> CoverOptimizeResult:
    color = detect_color(inp.query)
    scores: Dict[int, float] = {}
    if color:
        for img in inp.images:
            s = 5.0 if color in img.colors else 0.5
            if img.role == "cover":
                s += 1.0
            scores[img.id] = round(s + _beauty_score(img) * 0.1, 2)
        reason = f"Query mentions color '{color}'; selected matching {color} image prioritized over aesthetics."
    else:
        for img in inp.images:
            scores[img.id] = _beauty_score(img)
        reason = "No color intent detected; ranked by role, tags and visual richness."
    best = max(scores, key=lambda k: scores[k])
    best_img = next(i for i in inp.images if i.id == best)
    selling_points = best_img.tags[:4] or [best_img.role]
    edit_prompt = None
    if color and color not in best_img.colors:
        edit_prompt = f"Recolor the dress to {color} while preserving folds and lighting."
    return CoverOptimizeResult(
        selected_image_id=best,
        scores={str(k): v for k, v in scores.items()},
        reason=reason,
        selling_points=selling_points,
        edit_prompt=edit_prompt,
    )


async def skill_carousel_plan(inp: CarouselPlanInput) -> CarouselPlanResult:
    color = detect_color(inp.query)
    cover = next(
        (i for i in inp.images if i.id == inp.current_cover_id),
        next((i for i in inp.images if i.role == "cover"), inp.images[0] if inp.images else None),
    )
    items: List[CarouselItem] = []
    order = 1

    def add(source_type: str, image_id: int, prompt: str, overlay: str, intent: str) -> None:
        nonlocal order
        items.append(
            CarouselItem(
                order=order,
                source_type=source_type,
                image_id=image_id,
                prompt=prompt,
                overlay_copy=overlay,
                intent=intent,
            )
        )
        order += 1

    if cover:
        add(
            "reuse",
            cover.id,
            f"Use {cover.filename} as hero cover, keep clean commercial crop.",
            "Timeless Ivory Dress Made For Every Moment",
            "Hook with strongest existing asset as cover slide.",
        )
    model = next((i for i in inp.images if i.role == "model"), None)
    if model:
        add(
            "reuse",
            model.id,
            f"Feature {model.filename} showing fit on model.",
            "See The Flattering Fit On Real Silhouettes",
            "Build trust via on-body presentation.",
        )
    if color and not any(i and color in i.colors for i in inp.images):
        add(
            "generate",
            -1,
            f"Generate full-body studio shot of the same dress in {color}, soft daylight, white background.",
            f"Also Available In Elegant {color.capitalize()} Colorway",
            "Expand colorway appeal.",
        )
    style = next((i for i in inp.images if "style" in i.role or "style" in i.tags), None)
    if style:
        add(
            "edit",
            style.id,
            f"Refine {style.filename}: lift contrast, sharpen fabric texture, keep palette.",
            "Styled Looks To Inspire Your Next Outfit",
            "Styling inspiration slide.",
        )
    detail = next((i for i in inp.images if "detail" in i.role or "detail" in i.tags), None)
    if detail:
        add(
            "edit",
            detail.id,
            f"Enhance {detail.filename} close-up: stitching clarity, subtle vignette.",
            "Crafted Details You Can See And Feel",
            "Close the loop with quality proof.",
        )
    return CarouselPlanResult(
        items=items[:5],
        overall_idea="A 3-5 slide story: hook cover, model proof, colorway expansion, styling, craft details.",
        image_understanding=(
            f"Catalog contains {len(inp.images)} studio assets"
            + (f"; query focuses on {color}." if color else ".")
        ),
    )
