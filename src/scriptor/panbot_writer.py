"""Write text pages to reMarkable notebooks for Panbot communication."""

import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .panbot_config import PanbotConfig


def generate_text_pdf(text: str, output_path: Path) -> None:
    """Generate a single-page PDF containing the given text.

    Uses pandoc to convert markdown text to a clean, readable PDF.
    Falls back to a minimal PDF if pandoc is unavailable.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    md_path = None

    try:
        with tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False) as f:
            timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
            md_content = f"---\ngeometry: margin=2cm\nfontsize: 14pt\n---\n\n"
            md_content += f"**Panbot** - {timestamp}\n\n---\n\n{text}\n"
            f.write(md_content)
            md_path = f.name

        subprocess.run(
            ["pandoc", md_path, "-o", str(output_path),
             "--pdf-engine=xelatex",
             "-V", "pagestyle=empty"],
            capture_output=True, check=True,
        )
        Path(md_path).unlink(missing_ok=True)
        return
    except (subprocess.CalledProcessError, FileNotFoundError):
        if md_path:
            Path(md_path).unlink(missing_ok=True)

    _write_minimal_pdf(text, output_path)


def _write_minimal_pdf(text: str, output_path: Path) -> None:
    """Write a minimal valid PDF with text content."""
    lines = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").split("\n")
    stream_lines = []
    y = 750
    stream_lines.append("BT")
    stream_lines.append("/F1 12 Tf")
    for line in lines:
        stream_lines.append(f"1 0 0 1 50 {y} Tm")
        stream_lines.append(f"({line}) Tj")
        y -= 18
    stream_lines.append("ET")
    stream_content = "\n".join(stream_lines)

    pdf = f"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length {len(stream_content)}>>
stream
{stream_content}
endstream
endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
xref
0 6
0000000000 65535 f
0000000009 00000 n
0000000058 00000 n
0000000115 00000 n
0000000266 00000 n
trailer<</Size 6/Root 1 0 R>>
startxref
0
%%EOF"""
    output_path.write_text(pdf)


class PanbotWriter:
    """Handles writing status messages to the panbot notebook on reMarkable."""

    def __init__(self, cfg: PanbotConfig, cloud=None):
        self.cfg = cfg
        self._cloud = cloud
        self._msg_dir = cfg.state_dir / "messages"
        self._msg_dir.mkdir(parents=True, exist_ok=True)

    @property
    def cloud(self):
        if self._cloud is None:
            from .rm_cloud import RemarkableCloud
            self._cloud = RemarkableCloud()
        return self._cloud

    def notify(self, message: str, prefix: str = "panbot") -> Path:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
        pdf_path = self._msg_dir / f"{prefix}-{timestamp}.pdf"
        generate_text_pdf(message, pdf_path)
        self.cloud.upload_document(pdf_path, parent_folder=self.cfg.folder)
        return pdf_path

    def notify_processing(self) -> Path:
        now = datetime.now(timezone.utc).strftime("%H:%M")
        return self.notify(
            f"Panbot is processing your changes...\n\n"
            f"Started at {now} UTC.\n\n"
            f"A new version will appear shortly."
        )

    def notify_complete(self, version: int, source: str, changes_summary: str = "") -> Path:
        msg = (
            f"New version ready (v{version})\n\n"
            f"Triggered by: {source}\n"
        )
        if changes_summary:
            msg += f"\nChanges:\n{changes_summary}\n"
        msg += "\nCheck the updated PDF and diff in your Panbot folder."
        return self.notify(msg)

    def notify_error(self, error: str) -> Path:
        return self.notify(f"Panbot error\n\n{error}\n\nWill retry on next change.")
