"""OCR provider integrations for handwriting extraction."""

import base64
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    import anthropic
    ANTHROPIC_AVAILABLE = True
except ImportError:
    ANTHROPIC_AVAILABLE = False

try:
    import groq
    GROQ_AVAILABLE = True
except ImportError:
    GROQ_AVAILABLE = False


def encode_image_to_base64(image_path: Path) -> str:
    """Encode image file to base64 string."""
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode("utf-8")


def classify_annotation_type(text: str) -> str:
    """
    Classify annotation type based on extracted text content.

    Uses simple heuristics to determine if annotation is:
    - correction: Contains strikethrough or replacement indicators
    - addition: Contains insertion marks or additional content
    - question: Contains question marks or query indicators
    - expansion: Contains arrows or expansion indicators
    - deletion: Contains deletion marks
    - highlight: Simple underline or emphasis marks
    - handwritten_note: Default for general notes
    """
    text_lower = text.lower()

    # Question patterns
    if "?" in text or any(word in text_lower for word in ["why", "how", "what", "when", "where", "who"]):
        return "question"

    # Correction patterns
    if any(indicator in text_lower for indicator in ["wrong", "should be", "correct to", "fix", "error"]):
        return "correction"

    # Deletion patterns
    if any(indicator in text_lower for indicator in ["delete", "remove", "cross out", "strike"]):
        return "deletion"

    # Addition patterns
    if any(indicator in text_lower for indicator in ["add", "insert", "include", "missing"]):
        return "addition"

    # Expansion patterns
    if any(indicator in text_lower for indicator in ["expand", "elaborate", "more detail", "explain"]):
        return "expansion"

    # Highlight/emphasis patterns
    if any(indicator in text_lower for indicator in ["important", "note", "remember", "key"]):
        return "highlight"

    # Default
    return "handwritten_note"


def ocr_with_claude(
    image_path: Path,
    api_key: Optional[str] = None,
    model: str = "claude-sonnet-5-5"
) -> Tuple[str, float]:
    """
    Extract text from handwritten annotation using Claude vision API.

    Returns:
        Tuple of (extracted_text, confidence_score)
    """
    if not ANTHROPIC_AVAILABLE:
        raise ImportError("anthropic package not installed. Run: uv pip install anthropic")

    if not api_key:
        api_key = os.getenv("ANTHROPIC_API_KEY")
        if not api_key:
            raise ValueError("ANTHROPIC_API_KEY not found in environment")

    # Determine media type from file extension
    ext = image_path.suffix.lower()
    media_type_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif"
    }
    media_type = media_type_map.get(ext, "image/png")

    # Encode image
    image_data = encode_image_to_base64(image_path)

    # Create client
    client = anthropic.Anthropic(api_key=api_key)

    # Call Claude vision API
    message = client.messages.create(
        model=model,
        max_tokens=2048,  # Increased for longer handwritten notes
        output_config={"effort": "low"},  # Simple transcription: keep thinking short
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image",
                        "source": {
                            "type": "base64",
                            "media_type": media_type,
                            "data": image_data,
                        },
                    },
                    {
                        "type": "text",
                        "text": """You are an OCR system. Extract ONLY the handwritten text from this image.

CRITICAL RULES:
- Output ONLY the extracted text, nothing else
- NO descriptions like "The image shows..." or "Here is the text..."
- NO explanations, NO commentary, NO analysis
- Just the raw handwritten text exactly as written

Extract all handwritten text preserving line breaks and reading order (top to bottom, left to right).
If there is no handwritten text, output: [No text detected]

Begin extraction now:"""
                    }
                ],
            }
        ],
    )

    # Extract response
    # Read by block type: a thinking block may come first
    extracted_text = "".join(b.text for b in message.content if b.type == "text").strip()

    # Calculate confidence based on response characteristics
    confidence = 0.9  # Claude generally high confidence
    if "[No text detected]" in extracted_text or not extracted_text:
        extracted_text = ""
        confidence = 0.0
    elif len(extracted_text) < 3:
        confidence = 0.6  # Lower confidence for very short extractions

    return extracted_text, confidence


def ocr_with_groq(
    image_path: Path,
    api_key: Optional[str] = None,
    model: str = "meta-llama/llama-4-scout-17b-16e-instruct"
) -> Tuple[str, float]:
    """
    Extract text from handwritten annotation using GROQ vision API.

    Returns:
        Tuple of (extracted_text, confidence_score)
    """
    if not GROQ_AVAILABLE:
        raise ImportError("groq package not installed. Run: uv pip install groq")

    if not api_key:
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            raise ValueError("GROQ_API_KEY not found in environment")

    # Encode image
    image_data = encode_image_to_base64(image_path)

    # Determine media type
    ext = image_path.suffix.lower()
    media_type_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp"
    }
    media_type = media_type_map.get(ext, "image/png")

    # Create client
    client = groq.Groq(api_key=api_key)

    # Call GROQ vision API
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "user",
                "content": [
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:{media_type};base64,{image_data}"
                        }
                    },
                    {
                        "type": "text",
                        "text": """You are an OCR system. Extract ONLY the handwritten text from this image.

CRITICAL RULES:
- Output ONLY the extracted text, nothing else
- NO descriptions like "The image shows..." or "Here is the text..."
- NO explanations, NO commentary, NO analysis
- Just the raw handwritten text exactly as written

Extract all handwritten text preserving line breaks and reading order (top to bottom, left to right).
If there is no handwritten text, output: [No text detected]

Begin extraction now:"""
                    }
                ]
            }
        ],
        temperature=0.0,  # Deterministic output for OCR
        max_tokens=2048,  # Increased for longer handwritten notes
    )

    # Extract response
    extracted_text = completion.choices[0].message.content.strip()

    # Calculate confidence based on response characteristics
    confidence = 0.85  # GROQ baseline confidence
    if "[No text detected]" in extracted_text or not extracted_text:
        extracted_text = ""
        confidence = 0.0
    elif len(extracted_text) < 3:
        confidence = 0.6

    return extracted_text, confidence


def ocr_with_visual_context(
    annotation_path: Path,
    page_image_path: Optional[Path] = None,
    api_key: Optional[str] = None,
    model: str = "meta-llama/llama-4-scout-17b-16e-instruct",
) -> Tuple[str, float]:
    """Understand handwritten annotations with full visual context.

    Unlike basic OCR, this understands arrows, circles, highlights,
    and spatial relationships between annotations and document content.

    Args:
        annotation_path: PNG of the annotation strokes
        page_image_path: Optional PNG of the PDF page with annotation overlaid
        api_key: GROQ API key
        model: Vision model to use

    Returns:
        Tuple of (instruction_text, confidence)
    """
    if not GROQ_AVAILABLE:
        return ocr_with_groq(annotation_path, api_key, model)

    if not api_key:
        api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return ocr_with_groq(annotation_path)

    client = groq.Groq(api_key=api_key)

    content: list = []

    # Always include the annotation image
    annot_b64 = encode_image_to_base64(annotation_path)
    content.append({"type": "text", "text": "ANNOTATION (handwritten strokes):"})
    content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{annot_b64}"}})

    # Include page context if available
    if page_image_path and page_image_path.exists():
        page_b64 = encode_image_to_base64(page_image_path)
        content.append({"type": "text", "text": "PDF PAGE with annotation overlaid:"})
        content.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{page_b64}"}})

    content.append({"type": "text", "text": """Analyze this handwritten annotation on a PDF document.

Look for:
- Handwritten TEXT (what does it say?)
- ARROWS (what do they point to?)
- CIRCLES or HIGHLIGHTS (what do they emphasize?)
- The RELATIONSHIP between text and drawings

Provide a single clear INSTRUCTION that captures exactly what the user wants.
If there's an arrow pointing to something, mention WHAT it points to.
Output ONLY the instruction, nothing else."""})

    completion = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": content}],
        temperature=0.0,
        max_tokens=256,
    )

    text = completion.choices[0].message.content.strip()
    confidence = 0.8 if page_image_path else 0.7

    if not text or "[No" in text:
        return "", 0.0

    return text, confidence


def groq_text_correct(
    raw_ocr_text: str,
    api_key: Optional[str] = None,
    model: str = "llama-3.3-70b-versatile",
) -> Tuple[str, float]:
    """Use GROQ text model to correct OCR output and score accuracy.

    Sends the raw OCR text to a fast text model to find the closest
    real phrase in the language. Returns corrected text and a similarity
    score (0.0 = gibberish, 1.0 = perfect).

    Returns:
        Tuple of (corrected_text, accuracy_score)
    """
    if not GROQ_AVAILABLE:
        return raw_ocr_text, 0.5

    if not api_key:
        api_key = os.getenv("GROQ_API_KEY")
    if not api_key:
        return raw_ocr_text, 0.5

    if not raw_ocr_text or raw_ocr_text.strip() == "[No text detected]":
        return "", 0.0

    client = groq.Groq(api_key=api_key)
    completion = client.chat.completions.create(
        model=model,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are an OCR post-processor for handwritten notes from a reMarkable tablet.\n"
                    "The input is raw OCR output that may have misread letters or words.\n\n"
                    "Your job:\n"
                    "1. Figure out what the person ACTUALLY wrote. Each line is a separate phrase or sentence.\n"
                    "2. Fix misread characters (e.g. 'Andunitiang' → 'handwriting', 'Veroguitian' → 'recognition')\n"
                    "3. Preserve the original line breaks and structure\n"
                    "4. Score: how much did you have to guess? (1.0 = text was already correct, 0.3 = heavily guessed)\n\n"
                    "The text could be in any language (English, French, etc). It may be instructions, notes, or questions.\n\n"
                    "Respond in EXACTLY this format:\n"
                    "CORRECTED: <the corrected text with line breaks preserved>\n"
                    "SCORE: <0.0 to 1.0>"
                ),
            },
            {
                "role": "user",
                "content": f"Raw OCR text:\n{raw_ocr_text}",
            },
        ],
        temperature=0.0,
        max_tokens=1024,
    )

    response = completion.choices[0].message.content.strip()

    # Parse response — CORRECTED: may span multiple lines until SCORE:
    corrected = raw_ocr_text
    score = 0.5
    lines = response.split("\n")
    corrected_lines = []
    in_corrected = False
    for line in lines:
        stripped = line.strip()
        if stripped.upper().startswith("CORRECTED:"):
            in_corrected = True
            first_part = stripped.split(":", 1)[1].strip()
            if first_part:
                corrected_lines.append(first_part)
        elif stripped.upper().startswith("SCORE:"):
            in_corrected = False
            try:
                score = float(stripped.split(":", 1)[1].strip())
                score = max(0.0, min(1.0, score))
            except ValueError:
                pass
        elif in_corrected and stripped:
            corrected_lines.append(stripped)

    if corrected_lines:
        corrected = "\n".join(corrected_lines)

    return corrected, score


def ocr_smart(
    image_path: Path,
    accuracy_threshold: float = 0.6,
) -> Tuple[str, float, str]:
    """Smart OCR: GROQ vision → text correction → Claude vision fallback.

    1. Run GROQ vision OCR (fast, cheap)
    2. Run GROQ text correction to score accuracy
    3. If accuracy < threshold, fall back to Claude vision (slower, better)

    Returns:
        Tuple of (final_text, confidence, provider_used)
    """
    # Step 1: GROQ vision OCR
    groq_key = os.getenv("GROQ_API_KEY")
    if not groq_key or not GROQ_AVAILABLE:
        # No GROQ — try Claude directly
        if ANTHROPIC_AVAILABLE and os.getenv("ANTHROPIC_API_KEY"):
            text, conf = ocr_with_claude(image_path)
            return text, conf, "claude"
        return "", 0.0, "none"

    raw_text, raw_conf = ocr_with_groq(image_path)
    if not raw_text:
        return "", 0.0, "groq"

    # Step 2: Text correction and accuracy scoring
    corrected, accuracy = groq_text_correct(raw_text)

    if accuracy >= accuracy_threshold:
        return corrected, accuracy, "groq+corrected"

    # Step 3: Low accuracy — fall back to Claude vision
    anthropic_key = os.getenv("ANTHROPIC_API_KEY")
    if ANTHROPIC_AVAILABLE and anthropic_key:
        claude_text, claude_conf = ocr_with_claude(image_path)
        if claude_text:
            return claude_text, claude_conf, "claude-fallback"

    # Claude not available — return corrected GROQ result anyway
    return corrected, accuracy, "groq+corrected(low)"


def process_annotation_with_ocr(
    image_path: Path,
    page_number: int,
    position: Dict[str, float],
    ocr_provider: str = "claude",
    config: Optional[Dict[str, str]] = None,
    auto_classify: bool = True
) -> Dict[str, Any]:
    """
    Process a single annotation image with OCR.

    Args:
        image_path: Path to annotation image (PNG or other format)
        page_number: Page number in document (1-indexed)
        position: Dict with x, y coordinates
        ocr_provider: "claude" or "groq"
        config: Optional config dict with api_key and model settings
        auto_classify: Whether to auto-classify annotation type

    Returns:
        Dict with annotation data including extracted text
    """
    config = config or {}

    # Perform OCR
    if ocr_provider.lower() == "claude":
        api_key = config.get("claude_api_key") or os.getenv("ANTHROPIC_API_KEY")
        model = config.get("claude_model", "claude-sonnet-5-5")
        extracted_text, confidence = ocr_with_claude(image_path, api_key, model)
    elif ocr_provider.lower() == "groq":
        api_key = config.get("groq_api_key") or os.getenv("GROQ_API_KEY")
        model = config.get("groq_model", "llama-3.2-90b-vision-preview")
        extracted_text, confidence = ocr_with_groq(image_path, api_key, model)
    else:
        raise ValueError(f"Unknown OCR provider: {ocr_provider}")

    # Classify annotation type if enabled and text was extracted
    if auto_classify and extracted_text:
        annotation_type = classify_annotation_type(extracted_text)
    else:
        annotation_type = "handwritten_note"

    return {
        "page": page_number,
        "annotation_type": annotation_type,
        "position": position,
        "extracted_text": extracted_text,
        "confidence": confidence,
        "image_path": str(image_path),
        "prompt_generated": ""  # Phase 3
    }
