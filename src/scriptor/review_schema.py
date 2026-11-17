"""
Pydantic schemas for the AI-powered review workflow.

This module defines the data structures for storing and validating
annotation reviews from reMarkable handwritten notes.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional
from pydantic import BaseModel, Field


class AnnotationPosition(BaseModel):
    """Position information for an annotation on a page."""

    x: float = Field(..., description="X coordinate of annotation")
    y: float = Field(..., description="Y coordinate of annotation")


class Annotation(BaseModel):
    """Single annotation extracted from reMarkable document."""

    page: int = Field(..., description="Page number (1-indexed)")
    annotation_type: Literal["handwritten_note", "correction", "question", "expansion", "deletion", "highlight"] = Field(
        ...,
        description="Type of annotation"
    )
    position: AnnotationPosition = Field(..., description="Position on page")
    extracted_text: str = Field("", description="OCR-extracted text from annotation")
    confidence: float = Field(0.0, ge=0.0, le=1.0, description="OCR confidence score")
    image_path: str = Field("", description="Path to annotation image (PNG)")
    prompt_generated: str = Field("", description="Generated prompt for document modification")

    # Optional metadata
    ocr_provider: Optional[str] = Field(None, description="OCR provider used (claude/groq)")
    ocr_model: Optional[str] = Field(None, description="Specific model used for OCR")
    timestamp_extracted: Optional[datetime] = Field(
        default_factory=datetime.now,
        description="When this annotation was extracted"
    )


class ReviewSummary(BaseModel):
    """Summary statistics for a review session."""

    total_annotations: int = Field(0, description="Total number of annotations found")
    by_type: Dict[str, int] = Field(default_factory=dict, description="Counts by annotation type")
    average_confidence: float = Field(0.0, ge=0.0, le=1.0, description="Average OCR confidence")
    pages_with_annotations: List[int] = Field(default_factory=list, description="Page numbers with annotations")
    ocr_provider_used: Optional[str] = Field(None, description="OCR provider used")


class ReviewOutput(BaseModel):
    """Complete review output for a reMarkable document."""

    document: str = Field(..., description="Original document filename (PDF)")
    remarkable_file: str = Field(..., description="reMarkable document filename (.rmdoc)")
    extracted_at: datetime = Field(default_factory=datetime.now, description="Extraction timestamp")
    ocr_provider: Literal["claude", "groq"] = Field(..., description="OCR provider used")
    total_pages: int = Field(..., description="Total pages in document")
    annotations: List[Annotation] = Field(default_factory=list, description="All annotations found")
    summary: ReviewSummary = Field(default_factory=ReviewSummary, description="Summary statistics")

    # Configuration used
    config_used: Optional[Dict[str, Any]] = Field(
        None,
        description="Configuration parameters used for this review"
    )

    def save_to_file(self, output_path: Path) -> None:
        """Save review output to JSON file."""
        import json

        output_path.parent.mkdir(parents=True, exist_ok=True)

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(
                self.model_dump(mode='json'),
                f,
                indent=2,
                ensure_ascii=False,
                default=str
            )

    @classmethod
    def from_file(cls, input_path: Path) -> "ReviewOutput":
        """Load review output from JSON file."""
        import json

        with open(input_path, 'r', encoding='utf-8') as f:
            data = json.load(f)

        return cls(**data)

    def generate_markdown_report(self) -> str:
        """Generate a human-readable markdown report."""
        lines = [
            f"# Review Report: {self.document}",
            "",
            f"**Extracted:** {self.extracted_at.isoformat()}",
            f"**OCR Provider:** {self.ocr_provider}",
            f"**Total Pages:** {self.total_pages}",
            "",
            "## Summary",
            "",
            f"- **Total Annotations:** {self.summary.total_annotations}",
            f"- **Average Confidence:** {self.summary.average_confidence:.2%}",
            f"- **Pages with Annotations:** {', '.join(map(str, sorted(self.summary.pages_with_annotations)))}",
            "",
            "### Annotations by Type",
            ""
        ]

        for ann_type, count in sorted(self.summary.by_type.items()):
            lines.append(f"- **{ann_type}:** {count}")

        lines.extend([
            "",
            "## Annotations",
            ""
        ])

        # Group annotations by page
        annotations_by_page: Dict[int, List[Annotation]] = {}
        for ann in self.annotations:
            if ann.page not in annotations_by_page:
                annotations_by_page[ann.page] = []
            annotations_by_page[ann.page].append(ann)

        for page_num in sorted(annotations_by_page.keys()):
            lines.append(f"### Page {page_num}")
            lines.append("")

            for i, ann in enumerate(annotations_by_page[page_num], 1):
                lines.extend([
                    f"#### Annotation {i} ({ann.annotation_type})",
                    "",
                    f"**Position:** ({ann.position.x:.0f}, {ann.position.y:.0f})",
                    f"**Confidence:** {ann.confidence:.2%}",
                    "",
                    f"**Extracted Text:**",
                    f"> {ann.extracted_text}",
                    "",
                    f"**Suggested Action:**",
                    f"> {ann.prompt_generated}",
                    "",
                    f"![Annotation Image]({ann.image_path})",
                    "",
                    "---",
                    ""
                ])

        return "\n".join(lines)


class ReviewConfig(BaseModel):
    """Configuration for the review workflow."""

    ocr_provider: Literal["claude", "groq"] = Field("claude", description="OCR provider to use")
    confidence_threshold: float = Field(0.85, ge=0.0, le=1.0, description="Minimum confidence for accepting OCR")
    auto_classify_annotations: bool = Field(True, description="Automatically classify annotation types")
    generate_prompts: bool = Field(True, description="Generate modification prompts")
    output_format: Literal["json", "markdown", "both"] = Field("json", description="Output format")

    # Annotation type detection
    annotation_types: List[str] = Field(
        default=[
            "correction",
            "addition",
            "question",
            "expansion",
            "deletion",
            "highlight"
        ],
        description="Annotation types to detect"
    )

    # OCR-specific settings
    claude_model: str = Field("claude-sonnet-5-5", description="Claude model for vision OCR")
    groq_model: str = Field("meta-llama/llama-4-scout-17b-16e-instruct", description="GROQ model for vision OCR")

    # Image processing
    image_dpi: int = Field(300, ge=72, le=600, description="DPI for PNG conversion")
    image_quality: int = Field(95, ge=1, le=100, description="JPEG quality (if applicable)")
