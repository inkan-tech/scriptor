"""LLM-powered markdown modification engine for Panbot."""

import os
import re
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path.home() / ".local" / "inkan-doc.env")

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

MD_FILE_PATTERN = re.compile(r"^[0-9]{2}-[\w-]+\.md$")


class Ghostwriter:
    """LLM-powered document ghostwriter."""

    def __init__(self, llm_provider: str = "claude"):
        self.llm_provider = llm_provider
        self._client = None

    @property
    def client(self):
        if self._client is None:
            if self.llm_provider == "claude":
                if not ANTHROPIC_AVAILABLE:
                    raise RuntimeError("anthropic SDK not installed. Run: uv pip install anthropic")
                self._client = anthropic.Anthropic()
            elif self.llm_provider == "groq":
                if not GROQ_AVAILABLE:
                    raise RuntimeError("groq SDK not installed. Run: uv pip install groq")
                self._client = groq.Groq()
        return self._client

    DEFAULT_PROMPTS = [
        "Write in a professional, clear style suitable for an inkan.link report",
        "Use structured sections with headings, concise paragraphs, and bullet points where appropriate",
        "Maintain a formal but accessible tone",
        "Include relevant context and rationale for technical decisions",
        "Keep the document self-contained and readable by stakeholders",
    ]

    @staticmethod
    def _load_system_template() -> str:
        """Load the system prompt template from bundled resources."""
        try:
            import importlib.resources as pkg_resources
            ref = pkg_resources.files("inkan_doc") / "templates" / "panbot-system-prompt.md"
            return ref.read_text(encoding="utf-8")
        except Exception:
            return (
                "You are Panbot, a ghostwriter for the document '{project_title}'.\n"
                "Modify markdown source files based on handwritten instructions.\n"
                "Return ONLY changed files using: ### filename.md followed by a markdown code block.\n\n"
                "{project_guidelines}"
            )

    def _build_system_prompt(self, project_title: str, project_prompts: list[str]) -> str:
        template = self._load_system_template()
        guidelines = project_prompts if project_prompts else self.DEFAULT_PROMPTS
        guidelines_text = "\n".join(f"- {p}" for p in guidelines)
        return template.format(
            project_title=project_title,
            project_guidelines=guidelines_text,
        )

    def _build_edit_prompt(self, instruction: str, markdown_files: dict[str, str]) -> str:
        prompt = f"Instruction from the author (handwritten):\n{instruction}\n\n"
        prompt += "Current document files:\n\n"
        for filename, content in sorted(markdown_files.items()):
            prompt += f"### {filename}\n```markdown\n{content}\n```\n\n"
        return prompt

    def _parse_response(self, response: str) -> dict[str, str]:
        """Parse LLM response into {filename: content} dict."""
        files = {}
        current_file = None
        in_code_block = False
        lines = []

        for line in response.split("\n"):
            header_match = re.match(r"^###\s+(\S+\.md)\s*$", line)
            if header_match and not in_code_block:
                if current_file and lines:
                    files[current_file] = "\n".join(lines).strip() + "\n"
                current_file = header_match.group(1)
                lines = []
                continue

            if current_file:
                if line.startswith("```markdown"):
                    in_code_block = True
                    continue
                if line.startswith("```") and in_code_block:
                    in_code_block = False
                    files[current_file] = "\n".join(lines).strip() + "\n"
                    current_file = None
                    lines = []
                    continue
                if in_code_block:
                    lines.append(line)

        if current_file and lines:
            files[current_file] = "\n".join(lines).strip() + "\n"

        return files

    def apply_changes(self, project_dir: Path, changes: dict[str, str]) -> list[str]:
        """Write changed files to project directory. Returns list of modified filenames."""
        modified = []
        for filename, content in changes.items():
            if not MD_FILE_PATTERN.match(filename):
                continue
            target = project_dir / filename
            target.write_text(content)
            modified.append(filename)
        return modified

    def ghostwrite(
        self,
        project_dir: Path,
        instruction: str,
        project_prompts: list[str],
        project_title: str,
    ) -> dict[str, str]:
        markdown_files = {}
        for md in sorted(project_dir.glob("[0-9][0-9]-*.md")):
            markdown_files[md.name] = md.read_text()

        if not markdown_files:
            return {}

        system_prompt = self._build_system_prompt(project_title, project_prompts)
        edit_prompt = self._build_edit_prompt(instruction, markdown_files)

        if self.llm_provider == "claude":
            response = self.client.messages.create(
                model=os.environ.get("PANBOT_MODEL", "claude-sonnet-4-5-20250929"),
                max_tokens=8192,
                system=system_prompt,
                messages=[{"role": "user", "content": edit_prompt}],
            )
            text = response.content[0].text
        elif self.llm_provider == "groq":
            response = self.client.chat.completions.create(
                model=os.environ.get("PANBOT_MODEL", "meta-llama/llama-4-scout-17b-16e-instruct"),
                max_tokens=8192,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": edit_prompt},
                ],
            )
            text = response.choices[0].message.content
        else:
            raise ValueError(f"Unknown LLM provider: {self.llm_provider}")

        changes = self._parse_response(text)
        self.apply_changes(project_dir, changes)
        return changes
