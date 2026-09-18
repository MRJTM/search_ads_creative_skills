#!/usr/bin/env python3
"""CLI wrapper for the carousel generate skill.

Reads a JSON payload from stdin, runs the async core function
``agent.skills_core.skill_carousel_plan`` and prints the result as
UTF-8 JSON on stdout. Works from any working directory.
"""
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def main() -> int:
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"Invalid JSON on stdin: {exc}", file=sys.stderr)
        return 2

    try:
        import asyncio

        from agent.models import CarouselPlanInput
        from agent.skills_core import skill_carousel_plan

        result = asyncio.run(skill_carousel_plan(CarouselPlanInput(**payload)))
    except TypeError as exc:
        print(f"Missing or invalid input fields: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"Skill execution failed: {exc}", file=sys.stderr)
        return 1

    sys.stdout.write(json.dumps(result.model_dump(), ensure_ascii=False) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
