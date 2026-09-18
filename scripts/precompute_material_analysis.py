#!/usr/bin/env python3
"""Offline precompute of material image analysis into the catalog.

Reads the product catalog, sends every image that is missing ``analysis``
(or every image with ``--force``) through the VISION_MODEL, and writes the
structured results back into the catalog **atomically** (temp file +
``os.replace``), so a crash can never leave a half-written catalog behind.

Safety / performance rules:
- at most 5 concurrent vision calls (``--workers``, clamped to 1..5);
- images that fail keep any existing valid analysis; nothing is ever
  overwritten with empty output, and on total failure the catalog is left
  untouched;
- strict path validation: only image files inside the materials directory are
  read; the in-memory preview is downscaled before upload;
- the API key and base64 payloads are never printed or logged.

Usage (from the repository root):
    python scripts/precompute_material_analysis.py                # fill misses
    python scripts/precompute_material_analysis.py --force        # re-analyze all
    python scripts/precompute_material_analysis.py --workers 3
    python scripts/precompute_material_analysis.py --catalog tmp/materials/catalog.json

Exit codes: 0 all requested images analyzed; 1 partial failures (catalog still
updated with the successes); 2 provider/key not configured; 3 no image
succeeded (catalog untouched).
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

from agent.config import settings  # noqa: E402
from agent.models import MaterialAnalysis  # noqa: E402
from agent.providers import ToApisClient, close_http_client  # noqa: E402

MAX_WORKERS = 5


def _parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Precompute per-image material analysis into the catalog "
        "with the configured VISION_MODEL (no query involved; results are "
        "reused at runtime)."
    )
    parser.add_argument(
        "--catalog",
        default=None,
        help="Catalog path (default: settings.CATALOG_PATH, resolved against the repo root).",
    )
    parser.add_argument(
        "--materials-dir",
        default=None,
        help="Directory containing the image files (default: the catalog's directory).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-analyze every image, overwriting existing valid analysis.",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=settings.MATERIAL_ANALYSIS_WORKERS,
        help="Parallel vision calls, 1..5 (default: %(default)s).",
    )
    return parser.parse_args(argv)


def _resolve_catalog_path(raw: Optional[str]) -> Path:
    if raw:
        return Path(raw).expanduser().resolve()
    configured = Path(settings.CATALOG_PATH).expanduser()
    if not configured.is_absolute():
        configured = REPO_ROOT / configured
    return configured.resolve()


def _load_catalog(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as f:
        catalog = json.load(f)
    if not isinstance(catalog, dict) or not isinstance(catalog.get("images"), list):
        raise SystemExit(f"ERROR: {path} does not look like a material catalog.")
    return catalog


def _has_valid_analysis(entry: Dict[str, Any]) -> bool:
    payload = entry.get("analysis")
    if not isinstance(payload, dict):
        return False
    try:
        return MaterialAnalysis(**payload).has_content
    except Exception:
        return False


def _analyze_one(
    client: ToApisClient,
    entry: Dict[str, Any],
    materials_dir: str,
) -> Optional[MaterialAnalysis]:
    """Analyze one catalog image entry; None on any failure. Never prints."""
    analysis = client.analyze_image(entry, materials_dir=materials_dir)
    if analysis is None or not analysis.has_content:
        return None
    analysis.source = "precompute"
    return analysis


def _write_atomic(path: Path, payload: Dict[str, Any]) -> None:
    """Write JSON atomically: temp file in the same directory + os.replace."""
    directory = path.parent
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=str(directory)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def main(argv: Optional[List[str]] = None) -> int:
    args = _parse_args(argv)
    catalog_path = _resolve_catalog_path(args.catalog)
    if not catalog_path.is_file():
        print(f"ERROR: catalog not found: {catalog_path}")
        return 2
    materials_dir = (
        Path(args.materials_dir).expanduser().resolve()
        if args.materials_dir
        else catalog_path.parent.resolve()
    )

    catalog = _load_catalog(catalog_path)
    entries: List[Dict[str, Any]] = [
        e for e in catalog["images"] if isinstance(e, dict) and e.get("filename")
    ]
    tasks = [e for e in entries if args.force or not _has_valid_analysis(e)]
    skipped = len(entries) - len(tasks)
    if not tasks:
        print(
            f"All {len(entries)} catalog images already have analysis "
            "(use --force to refresh); nothing to do."
        )
        return 0

    client = ToApisClient()
    if not client.available:
        print(
            "ERROR: TOPAPI_API_KEY is not configured; refusing to fabricate "
            "analysis content."
        )
        return 2

    workers = max(1, min(args.workers, MAX_WORKERS, len(tasks)))
    successes: List[Tuple[Dict[str, Any], MaterialAnalysis]] = []
    failures: List[str] = []
    interrupted = False
    try:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(
                    _analyze_one, client, entry, str(materials_dir)
                ): entry
                for entry in tasks
            }
            for future in as_completed(futures):
                entry = futures[future]
                label = str(entry.get("filename") or f"id={entry.get('id')}")
                try:
                    analysis = future.result()
                except Exception:
                    analysis = None
                if analysis is None:
                    failures.append(label)
                else:
                    successes.append((entry, analysis))
    except KeyboardInterrupt:
        interrupted = True
        failures.extend(
            str(e.get("filename") or f"id={e.get('id')}")
            for e in tasks
            if not any(e is s[0] for s in successes)
        )
    finally:
        close_http_client()

    if interrupted:
        print(
            f"Interrupted; catalog left unchanged ({len(successes)} analyses discarded)."
        )
        return 3

    if not successes:
        print(
            f"ERROR: no image produced a valid analysis; catalog left unchanged "
            f"({len(failures)} failed, model={settings.VISION_MODEL})."
        )
        return 3

    # Merge only the successes: failed images keep their existing analysis.
    for entry, analysis in successes:
        entry["analysis"] = analysis.model_dump(exclude_none=True)
    _write_atomic(catalog_path, catalog)

    print(
        f"Analyzed {len(successes)}/{len(tasks)} images with "
        f"{settings.VISION_MODEL} (workers={workers}, skipped={skipped}); "
        f"failed={len(failures)}."
    )
    if failures:
        print("Failed images (existing analysis kept): " + ", ".join(failures))
    print(f"Catalog updated atomically: {catalog_path}")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
