import os
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

# Repo root (agent/..). Defaults below resolve against it so the backend works
# the same when launched from the repo root locally and from a Vercel function
# where the working directory may differ.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Settings:
    # TOPAPI_* is the current naming. Keep TOAPIS_* as a compatibility fallback
    # for existing local deployments and older README copies.
    TOPAPI_API_KEY: str = os.getenv("TOPAPI_API_KEY") or os.getenv("TOAPIS_API_KEY", "")
    TOPAPI_BASE_URL: str = os.getenv("TOPAPI_BASE_URL") or os.getenv(
        "TOAPIS_BASE_URL", "https://toapis.cn/v1"
    )
    TOPAPI_PROXY_URL: Optional[str] = (
        os.getenv("TOPAPI_PROXY_URL") or os.getenv("TOAPIS_PROXY_URL") or None
    )
    TOPAPI_TIMEOUT_SECONDS: float = float(os.getenv("TOPAPI_TIMEOUT_SECONDS", "90"))
    TOAPIS_API_KEY: str = TOPAPI_API_KEY
    TOAPIS_BASE_URL: str = TOPAPI_BASE_URL
    TOAPIS_PROXY_URL: Optional[str] = TOPAPI_PROXY_URL
    # Conversation agent (tool decisions + final natural replies).
    AGENT_MODEL: str = os.getenv("AGENT_MODEL", "qwen3.5-flash")
    # Vision understanding is cached per image, so a slower-but-sharper
    # vision model is affordable. deepseek-v4-flash-vision-exp measured
    # ~17.8s per image at max_tokens=3000 vs ~37.2s for glm-5.3-flash.
    VISION_MODEL: str = os.getenv("VISION_MODEL", "deepseek-v4-flash-vision-exp")
    IMAGE_MODEL: str = os.getenv("IMAGE_MODEL", "gpt-image-2")
    # Wall-clock budget for one vision analysis request (per image).
    VISION_TIMEOUT_SECONDS: float = float(os.getenv("VISION_TIMEOUT_SECONDS", "60"))
    # Vision needs a generous completion budget, otherwise descriptions get
    # truncated mid-JSON and the whole analysis is wasted.
    VISION_MAX_TOKENS: int = int(os.getenv("VISION_MAX_TOKENS", "3000"))
    # Agent turns must stay short: 256 tokens is plenty for a tool decision or
    # a 1-3 sentence chat reply, and caps the worst-case generation latency.
    AGENT_MAX_TOKENS: int = int(os.getenv("AGENT_MAX_TOKENS", "256"))
    FINAL_MAX_TOKENS: int = int(os.getenv("FINAL_MAX_TOKENS", "256"))
    # Parallel vision workers for runtime misses (offline precompute caps at 5).
    MATERIAL_ANALYSIS_WORKERS: int = int(os.getenv("MATERIAL_ANALYSIS_WORKERS", "5"))
    SUPABASE_URL: Optional[str] = os.getenv("SUPABASE_URL") or None
    SUPABASE_SECRET_KEY: Optional[str] = (
        os.getenv("SUPABASE_SECRET_KEY") or os.getenv("SUPABASE_KEY") or None
    )
    # This repo's demo-materials bucket on Supabase Storage (public read).
    SUPABASE_STORAGE_BUCKET: str = os.getenv(
        "SUPABASE_STORAGE_BUCKET", "search_ads_creative_skills"
    )
    MATERIALS_DIR: str = os.getenv(
        "MATERIALS_DIR", os.path.join(_REPO_ROOT, "materials")
    )
    CATALOG_PATH: str = os.getenv(
        "CATALOG_PATH", os.path.join(_REPO_ROOT, "materials", "catalog.json")
    )

    @property
    def has_api_key(self) -> bool:
        return bool(self.TOPAPI_API_KEY) and "your" not in self.TOPAPI_API_KEY.lower()


settings = Settings()
