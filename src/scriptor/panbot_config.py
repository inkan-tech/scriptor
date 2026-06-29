"""Panbot configuration model."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, field_validator


class PanbotConfig(BaseModel):
    """Configuration for the Panbot ghostwriter daemon."""

    project_dir: str
    folder: str  # reMarkable folder path e.g. "/Panbot/my-report"
    folder_id: str = ""  # Optional: hardcoded RM folder UUID (skips resolution)
    poll_interval: int = 30  # seconds between cloud checks
    inactivity_trigger: int = 180  # seconds of quiet before processing (3 min)
    ocr_provider: Literal["claude", "groq"] = "groq"
    llm_provider: Literal["claude", "groq"] = "claude"
    diff_engine: Literal["latexdiff", "pandiff", "markdown"] = "latexdiff"
    max_versions: int = 10
    name: str = ""  # optional human-friendly name (used in multi-project mode)
    # Git/GitHub integration
    github_repo: str = ""  # GitHub repo slug e.g. "owner/repo"
    git_branch: str = ""  # branch to push to (default: current branch)
    git_push: bool = False  # auto-push after commit
    git_push_interval: int = 0  # push every N versions (0 = every commit)

    @field_validator("project_dir")
    @classmethod
    def resolve_project_dir(cls, v: str) -> str:
        return str(Path(v).resolve())

    @property
    def state_dir(self) -> Path:
        return Path.home() / ".local" / "share" / "panbot" / Path(self.project_dir).name

    @property
    def versions_dir(self) -> Path:
        return self.state_dir / "versions"

    @property
    def log_file(self) -> Path:
        return self.state_dir / "panbot.log"

    @property
    def pid_file(self) -> Path:
        return self.state_dir / "panbot.pid"

    @classmethod
    def from_file(cls, path: Path) -> "PanbotConfig":
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")
        data = yaml.safe_load(path.read_text())
        # Strip multi-project-only keys so single-project files still load
        data.pop("projects", None)
        data.pop("monorepo", None)
        data.pop("folder_prefix", None)
        return cls(**data)

    def save_to_file(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(self.model_dump(), default_flow_style=False))


class ProjectEntry(BaseModel):
    """One project in a multi-project panbot config."""

    name: str
    project_dir: str
    folder: str
    github_repo: str = ""
    git_branch: str = ""

    @field_validator("project_dir")
    @classmethod
    def resolve_dir(cls, v: str) -> str:
        return str(Path(v).resolve())


class PanbotMultiConfig(BaseModel):
    """Multi-project panbot configuration.

    Supports two forms:

    1. Explicit project list::

        projects:
          - name: architecture-performance
            project_dir: /path/to/architecture-performance
            folder: /Panbot/architecture-performance

    2. Auto-discover from a monorepo root::

        monorepo: /path/to/workspace
        folder_prefix: /Panbot

    Global settings (poll_interval, etc.) serve as defaults for each project.
    """

    projects: list[ProjectEntry] = []
    monorepo: str | None = None
    folder_prefix: str = "/Panbot"
    # Shared daemon settings (defaults for all projects)
    poll_interval: int = 30
    inactivity_trigger: int = 180
    ocr_provider: Literal["claude", "groq"] = "groq"
    llm_provider: Literal["claude", "groq"] = "claude"
    diff_engine: Literal["latexdiff", "pandiff", "markdown"] = "latexdiff"
    max_versions: int = 10
    git_push: bool = False
    git_push_interval: int = 0

    def discover_projects(self) -> list[ProjectEntry]:
        """Return the full resolved project list (explicit + auto-discovered)."""
        result = list(self.projects)
        if self.monorepo:
            monorepo_path = Path(self.monorepo).resolve()
            for candidate in sorted(monorepo_path.iterdir()):
                if not candidate.is_dir():
                    continue
                has_inkan = (
                    (candidate / "inkan-doc.yaml").exists()
                    or (candidate / "inkan-doc.yml").exists()
                )
                if not has_inkan:
                    continue
                # Skip if already listed explicitly
                if any(str(Path(p.project_dir).resolve()) == str(candidate) for p in result):
                    continue
                result.append(ProjectEntry(
                    name=candidate.name,
                    project_dir=str(candidate),
                    folder=f"{self.folder_prefix}/{candidate.name}",
                ))
        return result

    def to_project_configs(self) -> list[PanbotConfig]:
        """Expand all projects into individual PanbotConfig objects."""
        out = []
        for entry in self.discover_projects():
            out.append(PanbotConfig(
                name=entry.name,
                project_dir=entry.project_dir,
                folder=entry.folder,
                github_repo=entry.github_repo,
                git_branch=entry.git_branch,
                poll_interval=self.poll_interval,
                inactivity_trigger=self.inactivity_trigger,
                ocr_provider=self.ocr_provider,
                llm_provider=self.llm_provider,
                diff_engine=self.diff_engine,
                max_versions=self.max_versions,
                git_push=self.git_push,
                git_push_interval=self.git_push_interval,
            ))
        return out

    @classmethod
    def from_file(cls, path: Path) -> "PanbotMultiConfig":
        if not path.exists():
            raise FileNotFoundError(f"Config not found: {path}")
        data = yaml.safe_load(path.read_text())
        return cls(**data)

    def save_to_file(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(yaml.dump(self.model_dump(), default_flow_style=False))

    @property
    def is_multi(self) -> bool:
        return bool(self.projects or self.monorepo)


def load_config(path: Path) -> "PanbotConfig | PanbotMultiConfig":
    """Load either a single-project or multi-project config from a file."""
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}")
    data = yaml.safe_load(path.read_text())
    if "projects" in data or "monorepo" in data:
        return PanbotMultiConfig(**data)
    return PanbotConfig(**data)
