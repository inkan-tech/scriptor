"""Dual-version management for Panbot ghostwriter."""

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .panbot_config import PanbotConfig


class VersionManager:
    """Manages current/previous document versions and diffs."""

    def __init__(self, cfg: PanbotConfig):
        self.cfg = cfg
        self.cfg.versions_dir.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self.cfg.versions_dir / "manifest.json"

    def _load_manifest(self) -> list[dict]:
        if self._manifest_path.exists():
            return json.loads(self._manifest_path.read_text())
        return []

    def _save_manifest(self, versions: list[dict]) -> None:
        self._manifest_path.write_text(json.dumps(versions, indent=2))

    def list_versions(self) -> list[dict]:
        return self._load_manifest()

    @property
    def current_pdf(self) -> Optional[Path]:
        versions = self._load_manifest()
        if not versions:
            return None
        latest = versions[-1]
        p = self.cfg.versions_dir / f"v{latest['version']:03d}" / "output.pdf"
        return p if p.exists() else None

    @property
    def previous_pdf(self) -> Optional[Path]:
        versions = self._load_manifest()
        if len(versions) < 2:
            return None
        prev = versions[-2]
        p = self.cfg.versions_dir / f"v{prev['version']:03d}" / "output.pdf"
        return p if p.exists() else None

    def snapshot(self, pdf_path: Path, source: str) -> int:
        versions = self._load_manifest()
        next_num = (versions[-1]["version"] + 1) if versions else 1

        version_dir = self.cfg.versions_dir / f"v{next_num:03d}"
        version_dir.mkdir(parents=True, exist_ok=True)

        shutil.copy2(pdf_path, version_dir / "output.pdf")

        project = Path(self.cfg.project_dir)
        for md in sorted(project.glob("[0-9][0-9]-*.md")):
            shutil.copy2(md, version_dir / md.name)

        versions.append({
            "version": next_num,
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "source": source,
            "pdf": str(version_dir / "output.pdf"),
        })

        while len(versions) > self.cfg.max_versions:
            old = versions.pop(0)
            old_dir = self.cfg.versions_dir / f"v{old['version']:03d}"
            if old_dir.exists():
                shutil.rmtree(old_dir)

        self._save_manifest(versions)
        return next_num

    def get_previous_markdown(self) -> Optional[Path]:
        versions = self._load_manifest()
        if len(versions) < 2:
            return None
        prev = versions[-2]
        return self.cfg.versions_dir / f"v{prev['version']:03d}"

    def build_diff(self) -> Optional[Path]:
        prev_dir = self.get_previous_markdown()
        if not prev_dir:
            return None

        project = Path(self.cfg.project_dir)
        diff_path = self.cfg.versions_dir / "latest-diff.md"

        lines = ["# Changes\n\n"]
        for md in sorted(project.glob("[0-9][0-9]-*.md")):
            prev_file = prev_dir / md.name
            if prev_file.exists():
                old_text = prev_file.read_text()
                new_text = md.read_text()
                if old_text != new_text:
                    lines.append(f"## {md.name}\n\n")
                    lines.append("```diff\n")
                    import difflib
                    diff = difflib.unified_diff(
                        old_text.splitlines(keepends=True),
                        new_text.splitlines(keepends=True),
                        fromfile=f"previous/{md.name}",
                        tofile=f"current/{md.name}",
                    )
                    lines.extend(diff)
                    lines.append("```\n\n")
            else:
                lines.append(f"## {md.name} (new file)\n\n")

        diff_path.write_text("".join(lines))
        return diff_path
