# Scriptor

**reMarkable cloud sync, handwriting OCR, and knowledge extraction for Python.**

*Named after Otis, the Roman scribe from Astérix aux Jeux Olympiques — diligently transcribing everything.*

## What is Scriptor?

Scriptor is a Python library and CLI that provides complete integration with the reMarkable tablet ecosystem:

- **Cloud sync** — detect changes, upload PDFs/EPUBs, download notebooks
- **Handwriting extraction** — parse `.rmdoc` files, extract strokes and annotations
- **OCR** — convert handwriting to text via MyScript or local providers
- **Knowledge pipeline** — transform handwritten notes into structured knowledge with LLM assistance
- **Monitoring daemon** — watch for reMarkable changes and trigger workflows automatically

## Installation

```bash
uv add scriptor

# With reMarkable scene parsing
uv add "scriptor[rmscene]"

# With OCR support
uv add "scriptor[ocr]"

# With AI knowledge extraction
uv add "scriptor[ai]"

# Full install
uv add "scriptor[rmscene,ocr,ai,monitor]"
```

## Quick Start

```bash
# Check reMarkable connection
scriptor rm check

# List documents on device
scriptor rm list

# Upload a PDF
scriptor rm upload my-document.pdf

# Download a notebook
scriptor rm download "My Notes"

# Extract handwriting from a .rmdoc file
scriptor rm extract notes.rmdoc

# Start the monitor daemon (watch for changes)
scriptor monitor start
```

## Python API

```python
from scriptor.rm_cloud import RemarkableCloud

rc = RemarkableCloud()

# List all documents
docs = rc.list_documents()

# Upload a file
rc.upload("report.pdf", destination="/Work/Reports")

# Download a notebook
rc.download("My Notes", output="notes.rmdoc")

# Detect changes since last sync
changes = rc.get_changes()
```

### Handwriting Extraction

```python
from scriptor.rm_utils import extract_handwriting
from scriptor.ocr_providers import get_ocr_provider

# Extract strokes from .rmdoc
strokes = extract_handwriting("notes.rmdoc")

# OCR via MyScript
ocr = get_ocr_provider("myscript")
text = ocr.recognize(strokes)
print(text)
```

### Knowledge Extraction (with AI)

```python
from scriptor.prompt_generation import generate_knowledge_prompt
from scriptor.panbot_ghostwriter import transform_to_knowledge

# Transform handwritten notes into structured knowledge
knowledge = transform_to_knowledge("notes.rmdoc")
print(knowledge.summary)
print(knowledge.action_items)
```

## Configuration

Create `~/.local/scriptor.env`:

```bash
# MyScript OCR (optional)
MYSCRIPT_APPLICATION_KEY=your_key
MYSCRIPT_HMAC_KEY=your_hmac

# AI provider (optional)
ANTHROPIC_API_KEY=your_key
```

## Architecture

```
src/scriptor/
├── rm_cloud.py          # reMarkable Cloud API (sync, upload, download, change detection)
├── rm_utils.py          # .rmdoc file parsing and stroke extraction
├── rmapi_compat.py      # rmapi CLI compatibility layer
├── notebook.py          # Notebook structure and management
├── myscript_analysis.py # MyScript handwriting analysis
├── ocr_providers.py     # Pluggable OCR providers
├── diff_utils.py        # Change detection between document versions
├── panbot_daemon.py     # Background monitoring daemon
├── panbot_monitor.py    # File system watcher for reMarkable changes
├── panbot_cli.py        # CLI entry point
├── panbot_config.py     # Configuration management
├── panbot_ghostwriter.py# Notes → structured content transformation
├── panbot_writer.py     # Document writer utilities
├── panbot_versions.py   # Version tracking
├── prompt_generation.py # LLM prompt generation from handwritten notes
├── review_schema.py     # Review/annotation schema
└── review_utils.py      # Review workflow utilities
```

## License

MIT License — see LICENSE for details.

## Contributing

Issues and PRs welcome at [github.com/inkan-tech/scriptor](https://github.com/inkan-tech/scriptor).
