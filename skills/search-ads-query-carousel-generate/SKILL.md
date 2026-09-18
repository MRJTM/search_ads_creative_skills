---
name: search-ads-query-carousel-generate
description: Plan a 3-5 slide search-ads carousel by first understanding each candidate image (subject, scene, detail, completeness, aesthetics), then building a cover-hook to on-body proof to style/scene to closing-detail storyline with reuse/edit/generate actions. Use for multi-image carousel planning, not single cover picks or title rewriting.
---

# Search Ads Query Carousel Generate

## When to trigger

- The user provides a search `query`, candidate `images` (id, filename, role, colors, tags, alt) and optionally `current_cover_id`, and asks for a multi-slide carousel ad plan.
- Do NOT trigger for single cover selection or title rewriting.

## Inputs (JSON via stdin)

```json
{
  "query": "ivory lace wedding guest dress",
  "images": [
    {
      "id": 1,
      "filename": "model_front.jpg",
      "role": "model",
      "colors": ["ivory"],
      "tags": ["model", "full body"],
      "alt": "model wearing ivory lace midi dress"
    }
  ],
  "current_cover_id": 1
}
```

## Standard execution

Run from this skill directory:

```bash
python scripts/run.py
```

The script reads JSON from stdin, calls the project's async core function (`agent.skills_core.skill_carousel_plan`), and prints a UTF-8 JSON result to stdout. It works from any cwd and uses a deterministic offline planner, so no API access is required.

## Workflow (progressive disclosure)

1. **Understand every image first** — for each candidate asset, determine subject (product on model / flat lay / detail), scene, visible details, completeness (full product shown?) and aesthetic quality. Summarize this in `image_understanding` before planning slides.
2. **Plan the storyline** — design a 3–5 slide sequence progressing: **cover hook → on-body proof → style/scene variation → detail close-out**. Map each slide to the best-understood asset.
3. **Assign actions** — each slide's action must be exactly one of:
   - `reuse`: keep an existing image; must carry its real `image_id`.
   - `edit`: refine an existing image; must carry its real `image_id` plus a concrete edit `prompt`.
   - `generate`: create a new image; `image_id` must be `-1` and the `prompt` fully describes the shot. Use generation only when no existing asset can fill the slide.
4. **Write overlay copy** — each slide's `overlay_copy` is about 10 English words, consistent with the slide's image content and the search query intent.
5. **Review** — check slide count, action validity, id authenticity and overlay consistency.

## Output JSON

```json
{
  "items": [
    {
      "order": 1,
      "source_type": "reuse",
      "image_id": 2,
      "prompt": "Use cover_front.jpg as hero cover, keep clean commercial crop.",
      "overlay_copy": "Timeless Ivory Dress Made For Every Moment",
      "intent": "Hook with strongest existing asset as cover slide."
    }
  ],
  "overall_idea": "A 3-5 slide story: hook cover, model proof, colorway expansion, styling, craft details.",
  "image_understanding": "Catalog contains 6 studio assets; query focuses on ivory."
}
```

## Quality checks

- 3–5 items, ordered 1..N without gaps.
- Every `reuse`/`edit` `image_id` exists in the input `images`; every `generate` uses `-1`.
- Overlay copies ~10 English words each, matching image content and query.
- Slide 1 is a hook (usually the current/strongest cover); the sequence progresses toward a detail close-out.

## Failure and fallback

- Empty `images`: script exits non-zero with a stderr message; request materials.
- Too few usable assets: fill missing storyline slots with `generate` slides (`image_id: -1`) instead of reusing weak images.
- No explicit query intent detected: plan a generic hook → proof → detail sequence based on image understanding alone and note it in `overall_idea`.
