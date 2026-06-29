"""
Context-aware prompt generation for document modifications from annotations.

This module generates actionable prompts based on extracted handwritten annotations,
enabling AI-assisted document editing based on reviewer feedback.
"""

from typing import Dict, Optional, List
from pathlib import Path


# Prompt templates for each annotation type
PROMPT_TEMPLATES = {
    "correction": """Correction needed on page {page}:

Original context: {context}
Handwritten correction: "{annotation_text}"

Please correct the text according to the handwritten note. If the correction is unclear, provide the most reasonable interpretation based on context.""",

    "addition": """Addition requested on page {page}:

Current context: {context}
Handwritten addition: "{annotation_text}"

Please add the handwritten content to the document at the appropriate location. If the placement is ambiguous, suggest the most logical position based on context.""",

    "question": """Question raised on page {page}:

Context: {context}
Handwritten question: "{annotation_text}"

This question needs to be addressed. Please either:
1. Answer the question if it's about clarification
2. Add content that addresses the concern
3. Suggest revisions to make the content clearer""",

    "expansion": """Expansion requested on page {page}:

Current text: {context}
Handwritten expansion note: "{annotation_text}"

Please expand or elaborate on this section based on the handwritten note. Add relevant details, examples, or explanations as indicated.""",

    "deletion": """Deletion marked on page {page}:

Text to review: {context}
Handwritten deletion note: "{annotation_text}"

Please remove or significantly revise the marked content according to the handwritten note. If complete deletion isn't appropriate, suggest alternative revisions.""",

    "highlight": """Important section highlighted on page {page}:

Highlighted text: {context}
Handwritten note: "{annotation_text}"

This section has been marked as important. Consider:
1. Ensuring this content is prominent and clear
2. Adding emphasis if appropriate
3. Cross-referencing this key point elsewhere if relevant""",

    "handwritten_note": """General note on page {page}:

Context: {context}
Handwritten note: "{annotation_text}"

Please review this handwritten note and suggest appropriate modifications to the document. The note may indicate needed changes, additions, or clarifications."""
}


def generate_prompt_for_annotation(
    annotation_type: str,
    annotation_text: str,
    page_number: int,
    context: Optional[str] = None,
    document_title: Optional[str] = None
) -> str:
    """
    Generate a context-aware prompt for modifying the document based on an annotation.

    Args:
        annotation_type: Type of annotation (correction, addition, question, etc.)
        annotation_text: The extracted handwritten text
        page_number: Page number where annotation appears (1-indexed)
        context: Optional surrounding text context from the document
        document_title: Optional document title for context

    Returns:
        Generated prompt for document modification
    """
    # Get template for annotation type
    template = PROMPT_TEMPLATES.get(
        annotation_type,
        PROMPT_TEMPLATES["handwritten_note"]  # Default template
    )

    # Prepare context
    if context is None or context.strip() == "":
        context = "[Context not available - review page {page} directly]"

    # Format the prompt
    prompt = template.format(
        page=page_number,
        annotation_text=annotation_text,
        context=context
    )

    # Add document title if available
    if document_title:
        prompt = f"Document: {document_title}\n\n{prompt}"

    return prompt


def generate_batch_prompts(
    annotations: List[Dict],
    document_title: Optional[str] = None,
    group_by_page: bool = True
) -> List[Dict[str, any]]:
    """
    Generate prompts for multiple annotations.

    Args:
        annotations: List of annotation dictionaries with keys:
            - page: int
            - annotation_type: str
            - extracted_text: str
            - context: Optional[str]
        document_title: Optional document title
        group_by_page: If True, group annotations by page number

    Returns:
        List of prompt dictionaries with keys:
            - page: int
            - annotation_type: str
            - original_text: str
            - prompt: str
            - priority: int (0-2, higher = more important)
    """
    prompts = []

    for annotation in annotations:
        prompt = generate_prompt_for_annotation(
            annotation_type=annotation.get("annotation_type", "handwritten_note"),
            annotation_text=annotation.get("extracted_text", ""),
            page_number=annotation.get("page", 0),
            context=annotation.get("context"),
            document_title=document_title
        )

        # Assign priority based on annotation type
        priority = get_annotation_priority(annotation.get("annotation_type"))

        prompts.append({
            "page": annotation.get("page", 0),
            "annotation_type": annotation.get("annotation_type", "handwritten_note"),
            "original_text": annotation.get("extracted_text", ""),
            "prompt": prompt,
            "priority": priority
        })

    # Sort by page and priority if grouping
    if group_by_page:
        prompts.sort(key=lambda x: (x["page"], -x["priority"]))

    return prompts


def get_annotation_priority(annotation_type: str) -> int:
    """
    Get priority level for annotation type.

    Returns:
        0: Low priority (highlight, general note)
        1: Medium priority (addition, expansion, question)
        2: High priority (correction, deletion)
    """
    high_priority = {"correction", "deletion"}
    medium_priority = {"addition", "expansion", "question"}

    if annotation_type in high_priority:
        return 2
    elif annotation_type in medium_priority:
        return 1
    else:
        return 0


def generate_summary_prompt(
    prompts: List[Dict],
    document_title: str
) -> str:
    """
    Generate a summary prompt for all modifications.

    Args:
        prompts: List of generated prompts from generate_batch_prompts
        document_title: Title of the document being reviewed

    Returns:
        Summary prompt for batch processing
    """
    if not prompts:
        return f"No modifications needed for '{document_title}'."

    # Count by type
    type_counts = {}
    for p in prompts:
        ann_type = p.get("annotation_type", "unknown")
        type_counts[ann_type] = type_counts.get(ann_type, 0) + 1

    # Build summary
    summary_lines = [
        f"# Review Summary for: {document_title}",
        "",
        f"Total annotations: {len(prompts)}",
        "",
        "## Annotations by Type:",
    ]

    for ann_type, count in sorted(type_counts.items(), key=lambda x: -x[1]):
        summary_lines.append(f"- {ann_type}: {count}")

    summary_lines.extend([
        "",
        "## Modifications Needed:",
        "",
        "The following changes have been identified through handwritten annotations.",
        "Review each prompt below and apply the suggested modifications.",
        ""
    ])

    # Add high priority items first
    high_priority = [p for p in prompts if p.get("priority", 0) == 2]
    if high_priority:
        summary_lines.extend([
            "### High Priority (Corrections & Deletions):",
            ""
        ])
        for i, p in enumerate(high_priority, 1):
            summary_lines.append(f"{i}. Page {p['page']}: {p['annotation_type']}")

    # Add medium priority
    medium_priority = [p for p in prompts if p.get("priority", 0) == 1]
    if medium_priority:
        summary_lines.extend([
            "",
            "### Medium Priority (Additions, Expansions, Questions):",
            ""
        ])
        for i, p in enumerate(medium_priority, 1):
            summary_lines.append(f"{i}. Page {p['page']}: {p['annotation_type']}")

    # Add low priority
    low_priority = [p for p in prompts if p.get("priority", 0) == 0]
    if low_priority:
        summary_lines.extend([
            "",
            "### Low Priority (Highlights & Notes):",
            ""
        ])
        for i, p in enumerate(low_priority, 1):
            summary_lines.append(f"{i}. Page {p['page']}: {p['annotation_type']}")

    return "\n".join(summary_lines)


def export_prompts_to_markdown(
    prompts: List[Dict],
    document_title: str,
    output_path: Path
) -> None:
    """
    Export generated prompts to a markdown file for review.

    Args:
        prompts: List of generated prompts
        document_title: Title of the document
        output_path: Path to save markdown file
    """
    lines = [
        f"# Modification Prompts: {document_title}",
        "",
        generate_summary_prompt(prompts, document_title),
        "",
        "---",
        "",
        "## Detailed Prompts",
        ""
    ]

    for i, prompt_data in enumerate(prompts, 1):
        lines.extend([
            f"### {i}. Page {prompt_data['page']} - {prompt_data['annotation_type'].title()}",
            "",
            f"**Priority:** {'🔴 High' if prompt_data['priority'] == 2 else '🟡 Medium' if prompt_data['priority'] == 1 else '🟢 Low'}",
            "",
            prompt_data['prompt'],
            "",
            "---",
            ""
        ])

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', encoding='utf-8') as f:
        f.write("\n".join(lines))
