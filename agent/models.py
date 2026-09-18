from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class MaterialAnalysis(BaseModel):
    """Structured, serializable vision understanding of one material image.

    Every field is optional (default_factory/empty) so partially-successful
    vision output is still usable. Metadata is non-sensitive provenance only:
    which model produced it, when, and through which path
    ("precomputed"/"precompute" = offline script, "parallel" = runtime vision
    call, "session" = reused inside one session).
    """

    description: str = ""
    product: str = ""
    colors: List[str] = Field(default_factory=list)
    scene: str = ""
    composition: str = ""
    visible_details: List[str] = Field(default_factory=list)
    selling_points: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    model: str = ""
    analyzed_at: str = ""
    source: str = ""

    @property
    def has_content(self) -> bool:
        return bool(
            self.description
            or self.product
            or self.selling_points
            or self.visible_details
        )

    def summary_line(self, max_chars: int = 140) -> str:
        """Compact one-liner used in prompts, traces and tool results."""
        selling = " / ".join(self.selling_points[:2])
        text = " ".join(part for part in (self.description, selling) if part).strip()
        return text[:max_chars]


class MaterialImage(BaseModel):
    id: int
    filename: str
    role: str
    colors: List[str] = Field(default_factory=list)
    tags: List[str] = Field(default_factory=list)
    alt: str = ""
    # Precomputed vision understanding (offline script writes it into the
    # catalog). Optional so catalogs without analysis keep working.
    analysis: Optional[MaterialAnalysis] = None


class TitleRewriteInput(BaseModel):
    query: str
    original_title: str
    facts: List[str] = Field(default_factory=list)
    language: str = "en"


class TitleRewriteResult(BaseModel):
    diagnosis: str
    strategy: str
    title: str
    self_check: Dict[str, Any] = Field(default_factory=dict)


class CoverOptimizeInput(BaseModel):
    query: str
    images: List[MaterialImage]
    current_cover_id: Optional[int] = None


class CoverOptimizeResult(BaseModel):
    selected_image_id: int
    scores: Dict[str, float]
    reason: str
    selling_points: List[str] = Field(default_factory=list)
    edit_prompt: Optional[str] = None


class CarouselItem(BaseModel):
    order: int
    source_type: str  # reuse | edit | generate
    image_id: int  # -1 for generated
    prompt: str
    overlay_copy: str
    intent: str


class CarouselPlanInput(BaseModel):
    query: str
    images: List[MaterialImage]
    current_cover_id: Optional[int] = None


class CarouselPlanResult(BaseModel):
    items: List[CarouselItem]
    overall_idea: str
    image_understanding: str


class CreativeState(BaseModel):
    title: str
    facts: List[str] = Field(default_factory=list)
    price: Optional[str] = None
    sku: Optional[str] = None
    cover_image_id: Optional[int] = None
    images: List[MaterialImage] = Field(default_factory=list)
    carousel: List[CarouselItem] = Field(default_factory=list)


class ChatRequest(BaseModel):
    message: str
    session_id: str = "default"


class TraceStep(BaseModel):
    step: str
    detail: str = ""
    data: Dict[str, Any] = Field(default_factory=dict)


class ChatResponse(BaseModel):
    reply: str
    creative: CreativeState
    trace: List[TraceStep] = Field(default_factory=list)
