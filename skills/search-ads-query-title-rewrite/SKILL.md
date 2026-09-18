---
name: search-ads-query-title-rewrite
description: Rewrite an existing product ad title by condensing and front-loading only verifiable facts from the search query and fact list; output diagnosis, strategy, rewritten title and self-check. Use when the task is title/copy rewriting driven by a search query, not image selection or carousel planning.
---

# Search Ads Query Title Rewrite

## When to trigger

- The user provides a search `query`, an `original_title`, optional `facts` (catalog-verified attributes), and a `language`, and asks for an improved ad title.
- Do NOT trigger for cover image selection or carousel planning (use the dedicated cover/carousel skills).

## Inputs (JSON via stdin)

```json
{
  "query": "ivory lace wedding guest dress",
  "original_title": "Elegant Beautiful Dress For Women Summer New Arrival",
  "facts": ["Ivory lace midi dress", "Lined bodice", "Dry clean only"],
  "language": "en"
}
```

## Standard execution

Run from this skill directory:

```bash
python scripts/run.py
```

The script reads JSON from stdin, calls the project's async core function (`agent.skills_core.skill_title_rewrite`), and prints a UTF-8 JSON result to stdout. It works from any working directory and requires no network or API — it uses a deterministic offline fallback.

## Workflow (progressive disclosure)

1. **Diagnose defects** — compare `query` intent against `original_title`: generic filler words, missing query keywords, vague claims, buried key information.
2. **Choose strategy** — decide which verifiable keywords from `query`/`facts` to front-load; decide target language (`language` field; if Chinese, keep Chinese and favor concise phrasing).
3. **Generate** — produce the new title by condensing and reordering existing information. Information density and front-loading only; never enrich with new claims.
4. **Red-line self-check** — verify every constraint below before returning.

## Constraints (red lines)

- Target **10–30 English words**; for Chinese keep the original language and stay concise.
- Only use information verifiable in `facts` or `original_title`.
- Forbidden additions: discounts, sales volume, "best/No.1" superlatives, or any fact not present in the inputs.
- No invented promotions, guarantees, or shipping/price claims.

## Output JSON

```json
{
  "diagnosis": "what was wrong with the original title",
  "strategy": "how the rewrite front-loads query-matching facts",
  "title": "the rewritten title",
  "self_check": {
    "word_count": 18,
    "in_range_10_30": true,
    "facts_only": true,
    "language": "en"
  }
}
```

## Quality checks

- Word count within range (English) / concise (Chinese).
- Every keyword traceable to `facts`/`original_title`/`query`.
- Query intent keywords appear early in the title.

## Failure and fallback

- Missing `query` or `original_title`: script exits non-zero with a stderr message; agent should ask the user for these fields.
- Empty `facts`: fall back to information present in `original_title` only; still no invention.
- If word count cannot reach 10 with verifiable words, return the best condensation and set `in_range_10_30: false` in `self_check` rather than inventing filler.
