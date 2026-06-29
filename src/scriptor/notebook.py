"""
Panbot - Pandoc Notebook support for reMarkable integration.

This module maps reMarkable notebook pages to numbered markdown files,
enabling a seamless handwriting-to-document workflow. Features:

1. Notebook page → numbered markdown file mapping
2. Prompt extraction from notebook annotations
3. Storyboard editing (page reorder = section reorder)
4. Inline notes & questions as HTML comments in pandoc output
"""

import json
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from dataclasses import dataclass, field


@dataclass
class NotebookPage:
    """Represents a single page from a reMarkable notebook."""
    page_index: int
    rm_file: str
    has_strokes: bool = False
    stroke_count: int = 0
    extracted_text: str = ""
    ocr_confidence: float = 0.0
    annotation_type: str = "handwritten_note"
    mapped_file: str = ""


@dataclass
class NotebookMapping:
    """Maps a reMarkable notebook to a pandoc project."""
    notebook_name: str
    project_dir: str
    pages: List[NotebookPage] = field(default_factory=list)
    page_order: List[int] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "notebook_name": self.notebook_name,
            "project_dir": self.project_dir,
            "page_count": len(self.pages),
            "page_order": self.page_order,
            "pages": [
                {
                    "page_index": p.page_index,
                    "has_strokes": p.has_strokes,
                    "stroke_count": p.stroke_count,
                    "mapped_file": p.mapped_file,
                    "extracted_text": p.extracted_text[:100] if p.extracted_text else "",
                }
                for p in self.pages
            ],
        }

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: Path) -> "NotebookMapping":
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        mapping = cls(
            notebook_name=data["notebook_name"],
            project_dir=data["project_dir"],
            page_order=data.get("page_order", []),
        )
        for p in data.get("pages", []):
            mapping.pages.append(NotebookPage(
                page_index=p["page_index"],
                rm_file="",
                has_strokes=p.get("has_strokes", False),
                stroke_count=p.get("stroke_count", 0),
                mapped_file=p.get("mapped_file", ""),
                extracted_text=p.get("extracted_text", ""),
            ))
        return mapping


def detect_pandoc_notebooks(remarkable_docs: List[Dict[str, str]]) -> List[Dict[str, str]]:
    """Filter reMarkable documents that look like pandoc-compatible notebooks.

    A pandoc notebook is any notebook (not PDF) on the device. We rely on
    rmapi metadata: documents with type 'CollectionType' are folders,
    'DocumentType' are files. Notebooks have no file extension.

    Args:
        remarkable_docs: List of dicts from ``list_remarkable_documents()``
            with keys 'name', 'type', 'id', etc.

    Returns:
        List of notebook dicts suitable for pandoc mapping.
    """
    notebooks = []
    for doc in remarkable_docs:
        name = doc.get("name", "")
        # Skip PDFs and EPUBs (uploaded files, not notebooks)
        if any(name.lower().endswith(ext) for ext in (".pdf", ".epub")):
            continue
        # Skip folders
        if doc.get("type") == "CollectionType":
            continue
        notebooks.append(doc)
    return notebooks


def extract_notebook_pages(
    rmdoc_path: Path,
    output_dir: Path,
) -> List[NotebookPage]:
    """Extract all pages from a reMarkable notebook .rmdoc file.

    Args:
        rmdoc_path: Path to the .rmdoc (ZIP) file.
        output_dir: Directory to store extracted SVGs.

    Returns:
        List of NotebookPage objects, one per page.
    """
    try:
        from .rm_utils import extract_strokes_from_rm_file, generate_svg_from_strokes
    except ImportError:
        raise ImportError("rm_utils required for notebook extraction")

    pages: List[NotebookPage] = []
    if not zipfile.is_zipfile(rmdoc_path):
        print(f"⚠️  {rmdoc_path.name} is not a ZIP-based rmdoc")
        return pages

    temp_dir = output_dir / "temp_notebook"
    temp_dir.mkdir(parents=True, exist_ok=True)

    try:
        with zipfile.ZipFile(rmdoc_path, "r") as zf:
            zf.extractall(temp_dir)

        import glob as globmod
        rm_files = sorted(globmod.glob(str(temp_dir / "**" / "*.rm"), recursive=True))

        for idx, rm_file in enumerate(rm_files):
            strokes = extract_strokes_from_rm_file(rm_file)
            page = NotebookPage(
                page_index=idx,
                rm_file=rm_file,
                has_strokes=bool(strokes),
                stroke_count=len(strokes) if strokes else 0,
            )

            if strokes:
                svg_path = output_dir / f"page_{idx:02d}.svg"
                generate_svg_from_strokes(strokes, svg_path)

            pages.append(page)
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

    return pages


def map_pages_to_markdown(
    pages: List[NotebookPage],
    project_dir: Path,
    section_names: Optional[List[str]] = None,
) -> NotebookMapping:
    """Map notebook pages to numbered markdown files.

    Each page becomes a file like ``00-intro.md``, ``01-chapter.md``, etc.
    If ``section_names`` is provided, those names are used; otherwise
    generic names are generated.

    Args:
        pages: Extracted notebook pages.
        project_dir: Target pandoc project directory.
        section_names: Optional list of section names (one per page).

    Returns:
        NotebookMapping with page-to-file assignments.
    """
    mapping = NotebookMapping(
        notebook_name="",
        project_dir=str(project_dir),
        page_order=list(range(len(pages))),
    )

    for i, page in enumerate(pages):
        if section_names and i < len(section_names):
            name = _slugify(section_names[i])
        else:
            name = f"section-{i}" if i > 0 else "intro"

        filename = f"{i:02d}-{name}.md"
        page.mapped_file = filename
        mapping.pages.append(page)

    return mapping


def sync_notebook_to_project(
    mapping: NotebookMapping,
    project_dir: Path,
    ocr_texts: Optional[Dict[int, str]] = None,
    overwrite: bool = False,
) -> List[Path]:
    """Write or update markdown files from notebook mapping.

    Args:
        mapping: The notebook-to-project mapping.
        project_dir: Target project directory.
        ocr_texts: Optional dict of page_index → OCR'd text content.
        overwrite: Whether to overwrite existing files.

    Returns:
        List of created/updated file paths.
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    written: List[Path] = []

    for page in mapping.pages:
        if not page.mapped_file:
            continue

        filepath = project_dir / page.mapped_file
        if filepath.exists() and not overwrite:
            continue

        # Use OCR text if available, otherwise placeholder
        content = ""
        if ocr_texts and page.page_index in ocr_texts:
            content = ocr_texts[page.page_index]
        elif page.extracted_text:
            content = page.extracted_text
        else:
            section_name = page.mapped_file.replace(".md", "").split("-", 1)[-1]
            section_title = section_name.replace("-", " ").title()
            content = f"# {section_title}\n\n<!-- Content from reMarkable page {page.page_index + 1} -->\n"

        filepath.write_text(content, encoding="utf-8")
        written.append(filepath)

    return written


# --- Feature 2: Prompt extraction ---


@dataclass
class ExtractedPrompt:
    """A prompt extracted from reMarkable annotations."""
    page: int
    text: str
    prompt_type: str  # "review", "instruction", "guideline", "general"
    confidence: float = 0.0


PROMPT_KEYWORDS = {
    "review": ["review", "check", "verify", "validate", "revise"],
    "instruction": ["do", "must", "should", "always", "never", "ensure", "make sure"],
    "guideline": ["style", "tone", "format", "template", "structure", "use"],
}


def classify_prompt_type(text: str) -> str:
    """Classify prompt text into a prompt type."""
    lower = text.lower()
    for ptype, keywords in PROMPT_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return ptype
    return "general"


def extract_prompts_from_annotations(
    annotations: List[Dict[str, Any]],
) -> List[ExtractedPrompt]:
    """Extract prompt-type annotations from review output.

    Filters annotations that look like writing instructions or review
    prompts rather than content corrections.

    Args:
        annotations: List of annotation dicts (from review workflow).

    Returns:
        List of ExtractedPrompt objects.
    """
    prompts: List[ExtractedPrompt] = []
    for ann in annotations:
        text = ann.get("extracted_text", "").strip()
        if not text:
            continue

        ann_type = ann.get("annotation_type", "handwritten_note")
        # Prompts are typically handwritten_notes or highlights that
        # contain instructional language rather than corrections
        if ann_type in ("correction", "deletion"):
            continue

        prompt_type = classify_prompt_type(text)
        prompts.append(ExtractedPrompt(
            page=ann.get("page", 0),
            text=text,
            prompt_type=prompt_type,
            confidence=ann.get("confidence", 0.0),
        ))

    return prompts


def save_prompts(prompts: List[ExtractedPrompt], output_path: Path) -> None:
    """Save extracted prompts to a JSON file."""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    data = [
        {
            "page": p.page,
            "text": p.text,
            "prompt_type": p.prompt_type,
            "confidence": p.confidence,
        }
        for p in prompts
    ]
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_prompts(path: Path) -> List[ExtractedPrompt]:
    """Load prompts from a JSON file."""
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return [
        ExtractedPrompt(
            page=p["page"],
            text=p["text"],
            prompt_type=p["prompt_type"],
            confidence=p.get("confidence", 0.0),
        )
        for p in data
    ]


# --- Feature 3: Storyboard editing ---


def reorder_sections(
    project_dir: Path,
    new_order: List[int],
) -> List[Tuple[str, str]]:
    """Reorder numbered markdown files to match new page order.

    Args:
        project_dir: Project directory containing numbered markdown files.
        new_order: List of current indices in desired order.
            E.g. [2, 0, 1] means: current file #2 becomes first, #0 becomes second, etc.

    Returns:
        List of (old_name, new_name) tuples for the renames performed.
    """
    # Discover existing numbered files
    existing = sorted(project_dir.glob("[0-9][0-9]-*.md"))
    if not existing:
        existing = sorted(project_dir.glob("[0-9]-*.md"))

    if not existing:
        raise FileNotFoundError("No numbered markdown files found")

    if len(new_order) != len(existing):
        raise ValueError(
            f"Order has {len(new_order)} entries but found {len(existing)} files"
        )

    if set(new_order) != set(range(len(existing))):
        raise ValueError("Order must be a permutation of file indices")

    # Build rename plan: move to temp names first to avoid collisions
    temp_dir = project_dir / ".reorder_tmp"
    temp_dir.mkdir(exist_ok=True)

    renames: List[Tuple[str, str]] = []

    try:
        # Phase 1: move all files to temp with new numbering
        for new_idx, old_idx in enumerate(new_order):
            old_path = existing[old_idx]
            # Preserve the name part after the number prefix
            name_part = re.sub(r"^\d+-", "", old_path.name)
            new_name = f"{new_idx:02d}-{name_part}"
            temp_path = temp_dir / new_name
            shutil.move(str(old_path), str(temp_path))
            renames.append((old_path.name, new_name))

        # Phase 2: move from temp back to project dir
        for tmp_file in temp_dir.iterdir():
            shutil.move(str(tmp_file), str(project_dir / tmp_file.name))
    finally:
        if temp_dir.exists():
            shutil.rmtree(temp_dir)

    return renames


def generate_document_from_storyboard(
    project_dir: Path,
    mapping: NotebookMapping,
) -> List[Path]:
    """Regenerate document structure from storyboard page order.

    Uses the mapping's ``page_order`` to reorder files if they differ
    from the current numbering.

    Args:
        project_dir: Project directory.
        mapping: NotebookMapping with desired page_order.

    Returns:
        List of current file paths in new order.
    """
    current_files = sorted(project_dir.glob("[0-9][0-9]-*.md"))
    if not current_files:
        current_files = sorted(project_dir.glob("[0-9]-*.md"))

    if not current_files:
        return []

    # Check if reorder is needed
    current_order = list(range(len(current_files)))
    desired = mapping.page_order[:len(current_files)]

    if desired and desired != current_order:
        reorder_sections(project_dir, desired)

    return sorted(project_dir.glob("[0-9][0-9]-*.md"))


# --- Feature 4: Inline notes & questions ---


@dataclass
class InlineAnnotation:
    """An annotation to embed as HTML comment in markdown."""
    page: int
    text: str
    annotation_type: str  # "note" or "question"
    section_file: str = ""


def annotations_to_inline_comments(
    annotations: List[Dict[str, Any]],
    mapping: Optional[NotebookMapping] = None,
) -> List[InlineAnnotation]:
    """Convert review annotations to inline HTML comments.

    Args:
        annotations: List of annotation dicts from review workflow.
        mapping: Optional notebook mapping to resolve page → file.

    Returns:
        List of InlineAnnotation objects ready for injection.
    """
    inline: List[InlineAnnotation] = []
    for ann in annotations:
        text = ann.get("extracted_text", "").strip()
        if not text:
            continue

        ann_type = ann.get("annotation_type", "handwritten_note")
        page = ann.get("page", 0)

        # Map annotation types to note/question
        if ann_type == "question" or "?" in text:
            comment_type = "question"
        else:
            comment_type = "note"

        section_file = ""
        if mapping and page <= len(mapping.pages):
            idx = page - 1  # pages are 1-indexed
            if 0 <= idx < len(mapping.pages):
                section_file = mapping.pages[idx].mapped_file

        inline.append(InlineAnnotation(
            page=page,
            text=text,
            annotation_type=comment_type,
            section_file=section_file,
        ))

    return inline


def inject_comments_into_markdown(
    project_dir: Path,
    inline_annotations: List[InlineAnnotation],
) -> Dict[str, int]:
    """Inject HTML comments into markdown source files.

    Appends comments at the end of the corresponding section file.
    If no section_file is set, comments go to the file matching the
    page number by index.

    Args:
        project_dir: Project directory with markdown files.
        inline_annotations: Annotations to inject.

    Returns:
        Dict of filename → number of comments injected.
    """
    md_files = sorted(project_dir.glob("[0-9][0-9]-*.md"))
    if not md_files:
        md_files = sorted(project_dir.glob("[0-9]-*.md"))

    injected: Dict[str, int] = {}

    for ann in inline_annotations:
        # Resolve target file
        target = None
        if ann.section_file:
            target = project_dir / ann.section_file
            if not target.exists():
                target = None

        if target is None and md_files:
            idx = ann.page - 1  # 1-indexed page → 0-indexed list
            if 0 <= idx < len(md_files):
                target = md_files[idx]

        if target is None:
            continue

        # Build comment
        tag = "QUESTION" if ann.annotation_type == "question" else "NOTE"
        comment = f"\n<!-- {tag}: {ann.text} -->\n"

        # Check if this exact comment already exists
        content = target.read_text(encoding="utf-8")
        if comment.strip() in content:
            continue

        # Append to file
        with open(target, "a", encoding="utf-8") as f:
            f.write(comment)

        fname = target.name
        injected[fname] = injected.get(fname, 0) + 1

    return injected


def extract_inline_comments(filepath: Path) -> List[InlineAnnotation]:
    """Extract existing inline NOTE/QUESTION comments from a markdown file.

    Args:
        filepath: Path to markdown file.

    Returns:
        List of InlineAnnotation objects found in the file.
    """
    content = filepath.read_text(encoding="utf-8")
    pattern = r"<!--\s*(NOTE|QUESTION):\s*(.+?)\s*-->"
    matches = re.findall(pattern, content)

    annotations = []
    for tag, text in matches:
        annotations.append(InlineAnnotation(
            page=0,
            text=text,
            annotation_type=tag.lower(),
            section_file=filepath.name,
        ))

    return annotations


def filter_display_comments(
    project_dir: Path,
    comment_type: Optional[str] = None,
) -> List[InlineAnnotation]:
    """Collect all inline comments from project files.

    Args:
        project_dir: Project directory.
        comment_type: Optional filter: "note", "question", or None for all.

    Returns:
        List of all InlineAnnotation objects found.
    """
    md_files = sorted(project_dir.glob("[0-9][0-9]-*.md"))
    if not md_files:
        md_files = sorted(project_dir.glob("[0-9]-*.md"))

    all_comments: List[InlineAnnotation] = []
    for f in md_files:
        comments = extract_inline_comments(f)
        if comment_type:
            comments = [c for c in comments if c.annotation_type == comment_type]
        all_comments.extend(comments)

    return all_comments


# --- Utilities ---


def _slugify(text: str) -> str:
    """Convert text to a slug suitable for filenames."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[-\s]+", "-", text)
    return text.strip("-")
