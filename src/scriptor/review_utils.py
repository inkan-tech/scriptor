"""
Utilities for AI-powered document review from reMarkable annotations.

This module provides the full review workflow:
- Phase 1: Per-page handwriting extraction
- Phase 1: SVG-to-PNG conversion
- Phase 2: OCR integration with Claude/GROQ
- Phase 2: Annotation classification
- JSON output generation
- OCR provider configuration
"""

import os
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import glob

# Import local modules
from .review_schema import (
    Annotation,
    AnnotationPosition,
    ReviewConfig,
    ReviewOutput,
    ReviewSummary
)

# Import OCR providers (Phase 2)
from . import ocr_providers

# Import prompt generation (Phase 3)
from . import prompt_generation

# Check for Pillow availability
try:
    from PIL import Image
    PILLOW_AVAILABLE = True
except ImportError:
    PILLOW_AVAILABLE = False

# Check for CairoSVG availability (requires system cairo library)
# Note: On macOS, you may need to set DYLD_LIBRARY_PATH=/opt/homebrew/lib
# before running Python if you get "no library called 'cairo-2' was found" errors
try:
    import cairosvg
    CAIROSVG_AVAILABLE = True
except (ImportError, OSError) as e:
    CAIROSVG_AVAILABLE = False

# Load OCR provider configuration from environment
from dotenv import load_dotenv

# Try to load from $HOME/.local/inkan-doc.env
inkan_doc_env_path = os.path.expanduser("~/.local/inkan-doc.env")
if os.path.exists(inkan_doc_env_path):
    load_dotenv(inkan_doc_env_path)


def check_review_dependencies() -> Dict[str, bool]:
    """Check if all required dependencies for review workflow are available."""
    return {
        "pillow": PILLOW_AVAILABLE,
        "cairosvg": CAIROSVG_AVAILABLE,
        "rmscene": check_rmscene_available(),
        "rmapi": check_rmapi_available(),
    }


def check_rmscene_available() -> bool:
    """Check if rmscene library is available."""
    try:
        import rmscene
        return True
    except ImportError:
        return False


def check_rmapi_available() -> bool:
    """Check if reMarkable cloud access is available."""
    from .rm_utils import check_rmapi_available as _check
    return _check()


def get_ocr_provider_config() -> Dict[str, Optional[str]]:
    """Get OCR provider configuration from environment."""
    return {
        "ocr_provider": os.getenv("OCR_PROVIDER", "claude"),
        "anthropic_api_key": os.getenv("ANTHROPIC_API_KEY"),
        "groq_api_key": os.getenv("GROQ_API_KEY"),
        "groq_model": os.getenv("GROQ_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
    }


def validate_ocr_config(config: ReviewConfig) -> Tuple[bool, Optional[str]]:
    """Validate that OCR provider is properly configured.

    Returns:
        Tuple of (is_valid, error_message)
    """
    env_config = get_ocr_provider_config()

    if config.ocr_provider == "claude":
        if not env_config["anthropic_api_key"]:
            return False, "ANTHROPIC_API_KEY not set in environment"
    elif config.ocr_provider == "groq":
        if not env_config["groq_api_key"]:
            return False, "GROQ_API_KEY not set in environment"
    else:
        return False, f"Unknown OCR provider: {config.ocr_provider}"

    return True, None


def convert_svg_to_png(svg_path: Path, png_path: Path, dpi: int = 300) -> bool:
    """Convert SVG file to PNG for OCR processing.

    Args:
        svg_path: Path to input SVG file
        png_path: Path to output PNG file
        dpi: DPI for output image

    Returns:
        True if conversion successful, False otherwise
    """
    if not CAIROSVG_AVAILABLE or not PILLOW_AVAILABLE:
        print(f"⚠️  SVG-to-PNG conversion requires pillow and cairosvg libraries")
        return False

    try:
        # Convert SVG to PNG using cairosvg
        png_path.parent.mkdir(parents=True, exist_ok=True)

        # Calculate scale for desired DPI (default SVG is usually 96 DPI)
        scale = dpi / 96.0

        cairosvg.svg2png(
            url=str(svg_path),
            write_to=str(png_path),
            scale=scale
        )

        # Verify the output
        if png_path.exists() and png_path.stat().st_size > 0:
            return True
        else:
            print(f"❌ PNG conversion failed: output file not created")
            return False

    except Exception as e:
        print(f"❌ Error converting SVG to PNG: {e}")
        return False


def extract_page_annotations(
    rmdoc_path: Path,
    output_dir: Path,
    config: ReviewConfig
) -> List[Dict[str, Any]]:
    """Extract annotations per page from reMarkable document.

    This is a Phase 1 scaffold - extracts handwriting per page and prepares
    for OCR processing.

    Args:
        rmdoc_path: Path to .rmdoc file
        output_dir: Directory to save extracted annotations
        config: Review configuration

    Returns:
        List of page annotation data (structure to be populated in Phase 2)
    """
    if not check_rmscene_available():
        raise ImportError("rmscene library is required for annotation extraction")

    output_dir.mkdir(parents=True, exist_ok=True)
    page_annotations = []

    try:
        # Check if file is a ZIP file (newer reMarkable format)
        if zipfile.is_zipfile(rmdoc_path):
            page_annotations = _extract_from_zip_rmdoc(rmdoc_path, output_dir, config)
        else:
            # Handle older .rm file format
            page_annotations = _extract_from_legacy_rm(rmdoc_path, output_dir, config)

    except Exception as e:
        print(f"❌ Error extracting annotations: {e}")
        raise

    return page_annotations


def _extract_from_zip_rmdoc(
    rmdoc_path: Path,
    output_dir: Path,
    config: ReviewConfig
) -> List[Dict[str, Any]]:
    """Extract annotations from ZIP-based .rmdoc file."""
    from .rm_utils import extract_strokes_from_rm_file, generate_svg_from_strokes

    page_annotations = []
    temp_dir = output_dir / "temp_extraction"
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(rmdoc_path, 'r') as zip_ref:
            # Extract all files
            zip_ref.extractall(temp_dir)

            # Find all .rm files (one per page)
            rm_files = sorted(glob.glob(str(temp_dir / "**" / "*.rm"), recursive=True))

            for page_num, rm_file in enumerate(rm_files, start=1):
                rm_path = Path(rm_file)
                page_base = f"{rmdoc_path.stem}_page{page_num}"

                # Extract strokes for this page
                strokes = extract_strokes_from_rm_file(rm_file)

                if strokes:
                    # Generate SVG
                    svg_path = output_dir / f"{page_base}_annotation.svg"
                    generate_svg_from_strokes(strokes, svg_path)

                    # Convert SVG to PNG for OCR
                    png_path = output_dir / f"{page_base}_annotation.png"
                    conversion_success = convert_svg_to_png(svg_path, png_path, config.image_dpi)

                    page_data = {
                        "page": page_num,
                        "rm_file": str(rm_path),
                        "svg_path": str(svg_path),
                        "png_path": str(png_path) if conversion_success else None,
                        "stroke_count": len(strokes),
                        "ready_for_ocr": conversion_success
                    }

                    page_annotations.append(page_data)
                    print(f"✅ Page {page_num}: Extracted {len(strokes)} stroke groups")

                    if conversion_success:
                        print(f"   → PNG created: {png_path.name}")
                else:
                    print(f"⭕ Page {page_num}: No annotations found")

    finally:
        # Clean up temp directory
        import shutil
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

    return page_annotations


def _extract_from_legacy_rm(
    rm_path: Path,
    output_dir: Path,
    config: ReviewConfig
) -> List[Dict[str, Any]]:
    """Extract annotations from legacy .rm file format."""
    # Legacy format handling - to be implemented if needed
    # For now, return empty list with warning
    print(f"⚠️  Legacy .rm format not yet supported for review workflow")
    return []


def create_review_output(
    document_name: str,
    rmdoc_file: str,
    page_annotations: List[Dict[str, Any]],
    config: ReviewConfig,
    enable_ocr: bool = True
) -> ReviewOutput:
    """Create ReviewOutput object from extracted page annotations.

    Phase 2: Now includes actual OCR processing for each annotation.

    Args:
        document_name: Original document name
        rmdoc_file: reMarkable document filename
        page_annotations: List of page annotation data
        config: Review configuration
        enable_ocr: Whether to run OCR (set False for Phase 1 behavior)

    Returns:
        ReviewOutput object with OCR results
    """
    annotations: List[Annotation] = []
    pages_with_annotations = []
    by_type: Dict[str, int] = {}
    total_confidence = 0.0
    confidence_count = 0

    # Process each page with OCR
    for page_data in page_annotations:
        if not page_data.get("ready_for_ocr"):
            continue

        page_num = page_data["page"]
        pages_with_annotations.append(page_num)

        # Skip OCR if disabled (Phase 1 mode)
        if not enable_ocr:
            continue

        # Determine image path (prefer PNG, fallback to SVG)
        image_path_str = page_data.get("png_path") or page_data.get("svg_path")
        if not image_path_str:
            print(f"⚠️  Page {page_num}: No image available for OCR")
            continue

        image_path = Path(image_path_str)
        if not image_path.exists():
            print(f"⚠️  Page {page_num}: Image not found: {image_path}")
            continue

        # Prepare OCR config
        ocr_config = {
            "claude_api_key": os.getenv("ANTHROPIC_API_KEY"),
            "claude_model": config.claude_model,
            "groq_api_key": os.getenv("GROQ_API_KEY"),
            "groq_model": config.groq_model
        }

        # Default position (full page annotation for now)
        position = AnnotationPosition(x=0.0, y=0.0)

        try:
            # Process annotation with OCR
            print(f"🔍 Page {page_num}: Running OCR with {config.ocr_provider}...")
            annotation_data = ocr_providers.process_annotation_with_ocr(
                image_path=image_path,
                page_number=page_num,
                position=position.model_dump(),
                ocr_provider=config.ocr_provider,
                config=ocr_config,
                auto_classify=config.auto_classify_annotations
            )

            # Filter by confidence threshold
            if annotation_data["confidence"] < config.confidence_threshold:
                print(f"   ⚠️  Low confidence ({annotation_data['confidence']:.2f}), skipping")
                continue

            # Generate prompt if enabled (Phase 3)
            prompt_generated = ""
            if config.generate_prompts and annotation_data["extracted_text"]:
                prompt_generated = prompt_generation.generate_prompt_for_annotation(
                    annotation_type=annotation_data["annotation_type"],
                    annotation_text=annotation_data["extracted_text"],
                    page_number=annotation_data["page"],
                    context=None,  # TODO: Extract context from document
                    document_title=document_name
                )

            # Create Annotation object
            annotation = Annotation(
                page=annotation_data["page"],
                annotation_type=annotation_data["annotation_type"],  # type: ignore
                position=AnnotationPosition(**annotation_data["position"]),
                extracted_text=annotation_data["extracted_text"],
                confidence=annotation_data["confidence"],
                image_path=annotation_data["image_path"],
                prompt_generated=prompt_generated
            )

            annotations.append(annotation)

            # Update statistics
            by_type[annotation.annotation_type] = by_type.get(annotation.annotation_type, 0) + 1
            total_confidence += annotation.confidence
            confidence_count += 1

            print(f"   ✅ Extracted: '{annotation_data['extracted_text'][:50]}...' "
                  f"[{annotation.annotation_type}, confidence: {annotation.confidence:.2f}]")

        except Exception as e:
            print(f"   ❌ OCR failed for page {page_num}: {e}")
            continue

    # Calculate average confidence
    average_confidence = total_confidence / confidence_count if confidence_count > 0 else 0.0

    summary = ReviewSummary(
        total_annotations=len(annotations),
        by_type=by_type,
        average_confidence=average_confidence,
        pages_with_annotations=sorted(pages_with_annotations),
        ocr_provider_used=config.ocr_provider
    )

    review_output = ReviewOutput(
        document=document_name,
        remarkable_file=rmdoc_file,
        ocr_provider=config.ocr_provider,  # type: ignore
        total_pages=len(page_annotations),
        annotations=annotations,
        summary=summary,
        config_used=config.model_dump()
    )

    return review_output


def load_review_config(
    config_path: Optional[Path] = None,
    **overrides
) -> ReviewConfig:
    """Load review configuration from file or create default.

    Args:
        config_path: Optional path to configuration file
        **overrides: Configuration overrides

    Returns:
        ReviewConfig instance
    """
    config_data = {}

    # Load from file if provided
    if config_path and config_path.exists():
        import yaml
        with open(config_path, 'r') as f:
            data = yaml.safe_load(f)
            if data and "review" in data:
                config_data = data["review"]

    # Apply environment variable overrides
    env_config = get_ocr_provider_config()
    if env_config["ocr_provider"]:
        config_data["ocr_provider"] = env_config["ocr_provider"]

    # Apply explicit overrides
    config_data.update(overrides)

    return ReviewConfig(**config_data)
