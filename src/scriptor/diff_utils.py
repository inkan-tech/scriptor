"""
Diff utilities for building tracked-change PDFs.

This module handles the workflow for generating a PDF that shows differences
between the current document state and a previous git commit, using latexdiff
(preferred) or pandiff (if available).
"""

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import List, Optional, Tuple

from .config import ProjectConfig


def check_pandiff_available() -> bool:
    """Check if pandiff is installed and available."""
    return shutil.which("pandiff") is not None


def check_latexdiff_available() -> bool:
    """Check if latexdiff is installed and available."""
    return shutil.which("latexdiff") is not None


def check_git_available(project_dir: Path) -> bool:
    """Check if git is available and the directory is a git repository."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def get_git_file_content(filepath: Path, git_ref: str, project_dir: Path) -> Optional[str]:
    """
    Get the content of a file at a specific git ref.

    Args:
        filepath: Absolute path to the file
        git_ref: Git reference (e.g., 'HEAD~1', 'abc123')
        project_dir: Root of the git repository

    Returns:
        File content as string, or None if the file did not exist at that ref
    """
    # Compute path relative to git repo root (not project_dir which may be a subdir)
    try:
        repo_root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
        repo_root = Path(repo_root_result.stdout.strip())
    except subprocess.CalledProcessError:
        repo_root = project_dir

    try:
        rel_path = filepath.relative_to(repo_root)
    except ValueError:
        rel_path = filepath

    try:
        result = subprocess.run(
            ["git", "show", f"{git_ref}:{rel_path}"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        # File did not exist at that ref (new file)
        return None


def _convert_md_to_tex(
    md_content: str,
    output_tex: Path,
    config: ProjectConfig,
    project_dir: Path,
) -> None:
    """
    Convert markdown content to a LaTeX file using pandoc.

    Uses the same template, filters, and resource path as the main build so
    that images and diagrams are resolved correctly and the preamble matches.

    Args:
        md_content: Markdown source text
        output_tex: Path to write the .tex output
        config: Project configuration (for template and metadata)
        project_dir: Project directory (for cwd and resource resolution)
    """
    from .pandoc_utils import _get_bundled_logo_path, _frontmatter_defines_key

    cmd = [
        "pandoc",
        "--from", "markdown",
        "--to", "latex",
        "--standalone",
        "--resource-path", str(project_dir),
        "-M", f"title={config.title}",
        "-M", f"author={config.author}",
        "-M", f"date={config.date}",
        "-o", str(output_tex),
    ]

    # Use project LaTeX template (same as main build)
    template_path = config.get_template_path("latex", str(project_dir))
    if template_path and template_path.exists():
        cmd.extend(["--template", str(template_path)])

    # Inject logo metadata if not overridden in frontmatter
    source_files = config.get_source_files(str(project_dir))
    logo_path = _get_bundled_logo_path()
    if logo_path and logo_path.exists():
        if not _frontmatter_defines_key(source_files, "header_center_logo_path"):
            cmd.extend(["-M", f"header_center_logo_path={logo_path}"])
        if not _frontmatter_defines_key(source_files, "titlepage_logo_path"):
            cmd.extend(["-M", f"titlepage_logo_path={logo_path}"])

    # Diagram filters (same as main build)
    if shutil.which("pandoc-plantuml"):
        cmd.extend(["-F", "pandoc-plantuml"])
    if shutil.which("mermaid-filter"):
        cmd.extend(["-F", "mermaid-filter"])

    # Lua filter
    lua_filter = project_dir / "templates" / "meta-vars.lua"
    if lua_filter.exists():
        cmd.extend(["--lua-filter", str(lua_filter)])

    # Write content to a temp file to avoid shell escaping issues
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    ) as tmp_md:
        tmp_md.write(md_content)
        tmp_md_path = tmp_md.name

    try:
        cmd.insert(1, tmp_md_path)
        subprocess.run(
            cmd,
            cwd=project_dir,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(
            f"pandoc failed converting markdown to LaTeX:\n{e.stderr}"
        )
    finally:
        Path(tmp_md_path).unlink(missing_ok=True)


def _run_latexdiff(old_tex: Path, new_tex: Path, diff_tex: Path, bw: bool = False) -> None:
    """Run latexdiff on two .tex files and write result.

    Args:
        bw: If True, use UNDERLINE markup (underline/strikethrough) instead of
            color markup — suitable for e-ink displays like reMarkable.
    """
    cmd = ["latexdiff"]
    if bw:
        cmd += ["--type=UNDERLINE"]
    cmd += [str(old_tex), str(new_tex)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        diff_tex.write_text(result.stdout, encoding="utf-8")
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"latexdiff failed:\n{e.stderr}")


def _compile_tex_to_pdf(tex_file: Path, output_pdf: Path) -> None:
    """
    Compile a .tex file to PDF using xelatex.

    Runs xelatex twice to resolve references, in the tex file's parent directory.
    """
    tex_dir = tex_file.parent

    for _pass in range(2):
        try:
            subprocess.run(
                [
                    "xelatex",
                    "-interaction=nonstopmode",
                    "-output-directory", str(tex_dir),
                    str(tex_file),
                ],
                cwd=tex_dir,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            # xelatex often returns non-zero even on success if there are warnings;
            # check if PDF was produced before raising
            pdf_candidate = tex_dir / (tex_file.stem + ".pdf")
            if not pdf_candidate.exists():
                raise RuntimeError(
                    f"xelatex failed (pass {_pass + 1}):\n{e.stdout[-3000:]}\n{e.stderr}"
                )

    # Move produced PDF to requested location
    produced_pdf = tex_dir / (tex_file.stem + ".pdf")
    if not produced_pdf.exists():
        raise RuntimeError(
            f"xelatex did not produce a PDF at {produced_pdf}"
        )
    if produced_pdf != output_pdf:
        shutil.move(str(produced_pdf), str(output_pdf))


def _suppress_title_page(tex_file: Path) -> None:
    """Neutralise \\maketitle so that non-first files don't emit a title page."""
    content = tex_file.read_text(encoding="utf-8")
    marker = "\\begin{document}"
    idx = content.find(marker)
    if idx == -1:
        return
    insert_pos = idx + len(marker)
    content = (
        content[:insert_pos]
        + "\n\\renewcommand{\\maketitle}{}\n"
        + content[insert_pos:]
    )
    tex_file.write_text(content, encoding="utf-8")


def build_diff_with_latexdiff(
    source_files: List[Path],
    against_ref: str,
    output_path: Path,
    config: ProjectConfig,
    project_dir: Path,
    bw: bool = False,
) -> Path:
    """
    Build a diff PDF using latexdiff.

    Workflow:
      1. For each source .md file, get old content at against_ref and current content.
      2. Convert both to .tex with pandoc.
      3. Run latexdiff on the pair.
      4. Concatenate all diff .tex bodies into a single document.
      5. Compile with xelatex.

    Args:
        source_files: Sorted list of source markdown files (current state)
        against_ref: Git ref to compare against
        output_path: Where to write the final PDF
        config: Project configuration
        project_dir: Project root (git repo)

    Returns:
        Path to the generated diff PDF
    """
    with tempfile.TemporaryDirectory(prefix="inkan_diff_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        pdf_parts: List[Path] = []

        for i, src_file in enumerate(source_files):
            current_content = src_file.read_text(encoding="utf-8")
            old_content = get_git_file_content(src_file, against_ref, project_dir)
            if old_content is None:
                old_content = ""

            # Skip files with no changes
            if old_content == current_content:
                continue

            stem = f"file_{i:03d}"
            old_tex = tmp_path / f"{stem}_old.tex"
            new_tex = tmp_path / f"{stem}_new.tex"

            _convert_md_to_tex(old_content, old_tex, config, project_dir)
            _convert_md_to_tex(current_content, new_tex, config, project_dir)

            diff_tex = tmp_path / f"{stem}_diff.tex"
            _run_latexdiff(old_tex, new_tex, diff_tex, bw=bw)

            # Suppress title page on all files after the first
            if pdf_parts:
                _suppress_title_page(diff_tex)

            # Compile this file's diff individually (preserves DIF macros)
            diff_pdf = tmp_path / f"{stem}_diff.pdf"
            _compile_tex_to_pdf(diff_tex, diff_pdf)
            pdf_parts.append(diff_pdf)

        if not pdf_parts:
            raise RuntimeError(
                "No changed files found between current state and " + against_ref
            )

        # Concatenate per-file PDFs
        output_path.parent.mkdir(parents=True, exist_ok=True)
        if len(pdf_parts) == 1:
            shutil.copy2(str(pdf_parts[0]), str(output_path))
        else:
            subprocess.run(
                ["pdfunite"] + [str(p) for p in pdf_parts] + [str(output_path)],
                check=True,
            )

    return output_path


def build_diff_with_pandiff(
    source_files: List[Path],
    against_ref: str,
    output_path: Path,
    project_dir: Path,
) -> Path:
    """
    Build a diff PDF using pandiff.

    pandiff handles each file pair individually; for multi-file projects we
    concatenate old and new content before passing to pandiff.

    Args:
        source_files: Sorted list of current source markdown files
        against_ref: Git ref to compare against
        output_path: Where to write the final PDF
        project_dir: Project root (git repo)

    Returns:
        Path to the generated diff PDF
    """
    # Concatenate old content
    old_parts: List[str] = []
    new_parts: List[str] = []

    for src_file in source_files:
        current_content = src_file.read_text(encoding="utf-8")
        old_content = get_git_file_content(src_file, against_ref, project_dir)
        if old_content is None:
            old_content = ""
        old_parts.append(old_content)
        new_parts.append(current_content)

    old_combined = "\n\n---\n\n".join(old_parts)
    new_combined = "\n\n---\n\n".join(new_parts)

    with tempfile.TemporaryDirectory(prefix="inkan_diff_pandiff_") as tmp_dir:
        tmp_path = Path(tmp_dir)

        old_md = tmp_path / "old.md"
        new_md = tmp_path / "new.md"
        old_md.write_text(old_combined, encoding="utf-8")
        new_md.write_text(new_combined, encoding="utf-8")

        try:
            subprocess.run(
                ["pandiff", str(old_md), str(new_md), "-o", str(output_path)],
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError as e:
            raise RuntimeError(f"pandiff failed:\n{e.stderr}")

    return output_path


def _extract_latex_preamble(tex_content: str) -> str:
    """Extract the preamble (everything before \\begin{document}) from a .tex file."""
    marker = "\\begin{document}"
    idx = tex_content.find(marker)
    if idx == -1:
        return tex_content
    return tex_content[:idx].rstrip()


def _extract_latex_body(tex_content: str) -> str:
    """Extract the body (between \\begin{document} and \\end{document})."""
    begin_marker = "\\begin{document}"
    end_marker = "\\end{document}"

    begin_idx = tex_content.find(begin_marker)
    end_idx = tex_content.rfind(end_marker)

    if begin_idx == -1 or end_idx == -1:
        return tex_content

    body = tex_content[begin_idx + len(begin_marker):end_idx]
    return body.strip()


def build_diff_pdf(
    project_dir: str,
    config: ProjectConfig,
    against_ref: str = "HEAD",
    output_path: Optional[Path] = None,
    bw: bool = False,
) -> Path:
    """
    Main entry point: build a diff PDF for the project.

    Selects pandiff if available, otherwise falls back to latexdiff + xelatex.

    Args:
        project_dir: Project directory path (string)
        config: Project configuration
        against_ref: Git ref to compare against (default: HEAD~1)
        output_path: Where to write the PDF; defaults to <project>-diff.pdf in project dir

    Returns:
        Path to the generated diff PDF

    Raises:
        RuntimeError: If no diff tool is available, git is unavailable, or build fails
    """
    proj_path = Path(project_dir).resolve()

    # Determine output path
    if output_path is None:
        output_path = proj_path / f"{config.project_name}-diff.pdf"

    # Validate git availability
    if not check_git_available(proj_path):
        raise RuntimeError(
            f"Directory '{proj_path}' is not a git repository or git is not available. "
            "The diff command requires a git history to compare against."
        )

    # Get source files
    source_files = config.get_source_files(project_dir)
    if not source_files:
        raise FileNotFoundError(
            "No source markdown files found (expected pattern: [0-9][0-9]-*.md)"
        )

    # Validate against_ref exists
    try:
        subprocess.run(
            ["git", "rev-parse", "--verify", against_ref],
            cwd=proj_path,
            capture_output=True,
            text=True,
            check=True,
        )
    except subprocess.CalledProcessError:
        raise RuntimeError(
            f"Git ref '{against_ref}' does not exist in this repository. "
            "Make sure the project has at least one prior commit."
        )

    # When --bw is requested, prefer latexdiff (proper UNDERLINE markup for e-ink).
    # Otherwise try pandiff first, fall back to latexdiff.
    if not bw and check_pandiff_available():
        return build_diff_with_pandiff(source_files, against_ref, output_path, proj_path)

    if check_latexdiff_available():
        if not shutil.which("xelatex"):
            raise RuntimeError(
                "latexdiff is available but xelatex is not. "
                "Install MacTeX or TeX Live to compile the diff PDF."
            )
        if len(source_files) > 1 and not shutil.which("pdfunite"):
            raise RuntimeError(
                "pdfunite is required for multi-file diff PDFs. "
                "Install poppler-utils: brew install poppler (macOS) "
                "or apt install poppler-utils (Linux)"
            )
        return build_diff_with_latexdiff(
            source_files, against_ref, output_path, config, proj_path, bw=bw
        )

    raise RuntimeError(
        "No diff tool available. Install one of:\n"
        "  • pandiff:    npm install -g pandiff\n"
        "  • latexdiff:  brew install latexdiff  (or included with MacTeX)"
    )
