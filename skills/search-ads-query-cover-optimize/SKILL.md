---
name: search-ads-query-cover-optimize
description: Pick or lightly edit the best existing product image as the ad cover by matching explicit query intent (color, style, SKU) first, then click appeal; returns selected image id, scores, reason, selling points and optional edit prompt. Use for single cover selection, not multi-image carousels or text rewriting.
---

# Search Ads Query Cover Optimize

## When to trigger

- The user provides a search `query`, a list of candidate `images` (id, filename, role, colors, tags, alt), and optionally `current_cover_id`, and asks which image should be the ad cover.
- Do NOT trigger for rewriting titles or planning multi-slide carousels.

## Inputs (JSON via stdin)

```json
{
  "query": "blue evening gown size 10",
  "images": [
    {
      "id": 1,
      "filename": "cover_front.jpg",
      "role": "cover",
      "colors": ["ivory"],
      "tags": ["studio", "front"],
      "alt": "full length studio shot"
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

The script reads JSON from stdin, calls the project's async core function (`agent.skills_core.skill_cover_optimize`), and prints a UTF-8 JSON result to stdout. It runs from any working directory with no API access — a deterministic offline scorer is used.

## Workflow (progressive disclosure)

1. **Parse query intent** — detect explicit attributes such as color, style, or SKU reference in `query`.
2. **Match before beauty** — when the query states a clear intent (e.g. a color), rank query/SKU-matching images above generic aesthetics and above the current default cover.
3. **Score all candidates** — assign each image a score combining intent match, role (cover/model weight), tags and visual richness.
4. **Decide reuse vs edit** — prefer selecting an existing asset. Only when no existing image matches the stated intent, provide an `edit_prompt` describing the minimal edit (e.g. recolor) that keeps lighting and folds natural.
5. **Balance** — the final pick must satisfy both click appeal and landing-page consistency with the query.

## Output JSON

```json
{
  "selected_image_id": 3,
  "scores": {"1": 3.7, "3": 5.9},
  "reason": "Query mentions color 'blue'; selected matching blue image prioritized over aesthetics.",
  "selling_points": ["blue", "model shot", "studio"],
  "edit_prompt": null
}
```

`edit_prompt` is `null` when a suitable existing image is reused unchanged; otherwise it contains a concrete editing instruction.

## Quality checks

- If the query names a color/style, the selected image matches it or an `edit_prompt` is supplied.
- The current cover is not kept merely because it is current — only if it still wins.
- `selling_points` come from the selected image's actual tags/attributes.

## Failure and fallback

- Empty `images` list: script exits non-zero with a stderr message; ask the user for materials.
- No query intent detected: fall back to ranking by role, tags and visual richness, stated in `reason`.
- Intent matches nothing and editing is not acceptable: return the best-scoring image and explain the mismatch in `reason` instead of inventing assets.
