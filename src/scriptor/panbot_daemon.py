"""Panbot daemon - polls reMarkable Cloud and triggers ghostwriting."""

import logging
import os
import signal
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from .panbot_config import PanbotConfig

logger = logging.getLogger("panbot")


class PanbotDaemon:
    """Watches a reMarkable folder for changes and triggers ghostwriting."""

    def __init__(self, cfg: PanbotConfig, cloud=None, monitor=None):
        self.cfg = cfg
        self._cloud = cloud
        self._monitor = monitor
        self._running = False
        self._last_change_time: float | None = None
        self._changed_sources: list[str] = []
        self._known_state: dict[str, dict] = {}
        self._folder_id: str | None = None
        self._triggered = False

    def _emit(self, event: dict) -> None:
        """Emit event to monitor dashboard (if connected)."""
        if self._monitor:
            self._monitor.emit(event)

    @property
    def cloud(self):
        if self._cloud is None:
            from .rm_cloud import RemarkableCloud
            self._cloud = RemarkableCloud()
        return self._cloud

    def _setup_logging(self) -> None:
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(self.cfg.log_file)
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.addHandler(logging.StreamHandler())

    def _resolve_folder(self) -> str:
        """Resolve the panbot folder path to a parent ID."""
        # Use hardcoded folder_id if available (no API calls needed)
        if self.cfg.folder_id:
            return self.cfg.folder_id
        # Use cached docs for resolution (fast)
        import json
        from pathlib import Path as P
        cache_path = P.home() / ".config" / "panbot" / "cache" / "docs.json"
        if cache_path.exists():
            docs = json.loads(cache_path.read_text())
        else:
            docs = self.cloud.list_documents()
        return self.cloud._resolve_folder_from_docs(docs, self.cfg.folder)

    def _get_folder_docs(self) -> list[dict]:
        """Get documents in the panbot folder.

        Ultra-minimal API calls:
        - 1 call: GET root (cached 10s TTL)
        - If root generation unchanged: 0 more calls, return cached
        - If generation changed: 1 call for root blob, find folder entry hash
        - If folder hash unchanged: 0 more calls (change was elsewhere in library)
        - If folder hash changed: fetch only the changed docs' metadata
        """
        if self._folder_id is None:
            return []

        from .rm_cloud import RemarkableCloud
        if not isinstance(self.cloud, RemarkableCloud):
            docs = self.cloud.list_documents()
            return [d for d in docs
                    if d.get("Parent") == self._folder_id
                    and d.get("Type") == "DocumentType"]

        if not hasattr(self, "_known_doc_ids"):
            self._known_doc_ids: set[str] = set()
        if not hasattr(self, "_last_root_generation"):
            self._last_root_generation = None
        if not hasattr(self, "_last_folder_hash"):
            self._last_folder_hash: str | None = None

        # 1 API call: get root (cached with TTL)
        root = self.cloud._get_root()
        root_gen = root.get("generation")

        # Fast path: generation unchanged → nothing synced anywhere
        if root_gen == self._last_root_generation and self._known_doc_ids:
            return list(getattr(self, "_cached_folder_docs", []))

        # Generation changed — fetch root blob to check folder hash (1 more call)
        root_blob = self.cloud._get_blob(root["hash"])
        root_index = self.cloud._parse_index(root_blob.decode("utf-8"))
        self._last_root_generation = root_gen

        # Find folder entry hash — if unchanged, the change was elsewhere
        folder_entry = next((e for e in root_index if e["id"] == self._folder_id), None)
        if not folder_entry:
            return list(getattr(self, "_cached_folder_docs", []))

        folder_hash = folder_entry["hash"]
        folder_changed = folder_hash != self._last_folder_hash
        self._last_folder_hash = folder_hash

        # If folder hash unchanged AND we have known docs, skip all blob fetches
        if not folder_changed and self._known_doc_ids:
            # But still check known doc entries for version/modified changes
            # by comparing their hashes in root_index (no blob fetch needed)
            known_hashes = {}
            for entry in root_index:
                if entry["id"] in self._known_doc_ids:
                    known_hashes[entry["id"]] = entry["hash"]

            old_hashes = getattr(self, "_known_doc_hashes", {})
            if known_hashes == old_hashes:
                # Nothing changed for our docs either
                return list(getattr(self, "_cached_folder_docs", []))

            # A known doc's hash changed — fetch only those that changed
            results = []
            for entry in root_index:
                if entry["id"] in self._known_doc_ids:
                    if entry["hash"] != old_hashes.get(entry["id"]):
                        try:
                            doc = self.cloud._entry_to_doc(entry)
                            if doc and doc.get("Parent") == self._folder_id:
                                results.append(doc)
                        except Exception:
                            pass
                    else:
                        # Unchanged — use cached version
                        cached = next(
                            (d for d in getattr(self, "_cached_folder_docs", [])
                             if d["ID"] == entry["id"]), None)
                        if cached:
                            results.append(cached)

            self._known_doc_hashes = known_hashes
            self._cached_folder_docs = [d for d in results if d.get("Type") == "DocumentType"]
            return self._cached_folder_docs

        # Folder hash changed — something in our folder was modified/added/removed
        # Fetch folder sub-index to find all children (1 call)
        try:
            folder_blob = self.cloud._get_blob(folder_hash)
            folder_sub = self.cloud._parse_index(folder_blob.decode("utf-8"))
        except Exception:
            return list(getattr(self, "_cached_folder_docs", []))

        # Build set of child doc IDs from folder sub-index
        child_ids = set()
        for s in folder_sub:
            base_id = s["id"].split(".")[0]
            if base_id != self._folder_id:
                child_ids.add(base_id)

        # Also include previously known docs
        all_doc_ids = child_ids | self._known_doc_ids

        # Fetch metadata only for docs we need
        results = []
        new_hashes = {}
        old_hashes = getattr(self, "_known_doc_hashes", {})

        for entry in root_index:
            if entry["id"] not in all_doc_ids:
                continue
            new_hashes[entry["id"]] = entry["hash"]
            # Skip blob fetch if hash unchanged
            if entry["hash"] == old_hashes.get(entry["id"]):
                cached = next(
                    (d for d in getattr(self, "_cached_folder_docs", [])
                     if d["ID"] == entry["id"]), None)
                if cached:
                    results.append(cached)
                    continue
            try:
                doc = self.cloud._entry_to_doc(entry)
                if doc and doc.get("Parent") == self._folder_id and doc.get("Type") == "DocumentType":
                    results.append(doc)
                    self._known_doc_ids.add(doc["ID"])
            except Exception:
                pass

        self._known_doc_hashes = new_hashes
        self._cached_folder_docs = results

        # First run bootstrap from cache if no results yet
        if not results and not self._known_doc_ids:
            import json
            cache_path = self.cloud._cache_dir / "docs.json"
            if cache_path.exists():
                cached = json.loads(cache_path.read_text())
                folder_docs = [d for d in cached
                              if d.get("Parent") == self._folder_id
                              and d.get("Type") == "DocumentType"]
                if folder_docs:
                    self._known_doc_ids = {d["ID"] for d in folder_docs}
                    for entry in root_index:
                        if entry["id"] in self._known_doc_ids:
                            new_hashes[entry["id"]] = entry["hash"]
                            try:
                                doc = self.cloud._entry_to_doc(entry)
                                if doc:
                                    results.append(doc)
                            except Exception:
                                pass
                    self._known_doc_hashes = new_hashes
                    self._cached_folder_docs = results

        return self._cached_folder_docs

    def _detect_changes(self) -> list[dict]:
        """Compare current folder state with known state. Returns changed docs.

        NOTE: Does NOT update _known_state — that happens only after
        successful processing via _acknowledge_changes().
        """
        current_docs = self._get_folder_docs()
        changed = []
        for doc in current_docs:
            doc_id = doc["ID"]
            old = self._known_state.get(doc_id)
            if old is None:
                changed.append(doc)
            elif (doc.get("Version") != old.get("Version")
                  or doc.get("ModifiedClient") != old.get("ModifiedClient")):
                changed.append(doc)
        # Store pending state — only committed after successful processing
        self._pending_state = {
            doc["ID"]: {
                "Version": doc.get("Version"),
                "ModifiedClient": doc.get("ModifiedClient"),
            }
            for doc in current_docs
        }
        return changed

    def _acknowledge_changes(self) -> None:
        """Commit pending state after successful processing."""
        if hasattr(self, "_pending_state") and self._pending_state:
            self._known_state.update(self._pending_state)
            self._pending_state = {}

    def _upload_with_retry(self, versioned_pdf, vm, version_num, doc_name, project_dir) -> None:
        """Upload to RM, retrying on rate limit until it succeeds."""
        import shutil

        for attempt in range(10):
            try:
                # Upload current
                doc_id = self.cloud.upload_document(versioned_pdf, parent_folder=self.cfg.folder)
                logger.info(f"Uploaded {versioned_pdf.name}")
                self._add_to_cache(versioned_pdf.stem, doc_id)

                # Upload previous
                prev_pdf = vm.previous_pdf
                if prev_pdf and prev_pdf.exists():
                    prev_num = version_num - 1
                    prev_versioned = project_dir / f"{doc_name}-v{prev_num}.pdf"
                    shutil.copy2(prev_pdf, prev_versioned)
                    self.cloud.upload_document(prev_versioned, parent_folder=self.cfg.folder)
                    logger.info(f"Uploaded {prev_versioned.name} (previous)")

                # Action outputs (logos, etc.) are embedded in the PDF — don't upload separately

                return  # success
            except Exception as e:
                err = str(e)
                if "429" in err or "Too Many" in err:
                    wait = 60 * (attempt + 1)
                    logger.warning(f"Upload rate limited, waiting {wait}s (attempt {attempt+1})")
                    self._emit({"type": "rate_limited", "backoff": wait})
                    time.sleep(wait)
                    self._emit({"type": "rate_limit_cleared"})
                else:
                    logger.error(f"Upload failed: {e}")
                    return

        logger.error("Upload failed after 10 retries")

    def _add_to_cache(self, name: str, doc_id: str = "") -> None:
        """Add a document to the local cache so the daemon tracks it."""
        import json
        cache_path = self.cloud._cache_dir / "docs.json"
        if not cache_path.exists():
            return
        try:
            docs = json.loads(cache_path.read_text())
            if not any(d.get("VissibleName") == name and d.get("Parent") == self._folder_id for d in docs):
                docs.append({
                    "ID": doc_id or name,
                    "VissibleName": name,
                    "Type": "DocumentType",
                    "Parent": self._folder_id or "",
                    "Version": 0,
                    "ModifiedClient": "",
                })
                cache_path.write_text(json.dumps(docs))
                if hasattr(self, "_known_doc_ids"):
                    self._known_doc_ids.add(doc_id or name)
        except Exception:
            pass

    def _on_change_detected(self, source_name: str) -> None:
        """Called when a change is detected in any source."""
        self._last_change_time = time.time()
        if source_name not in self._changed_sources:
            self._changed_sources.append(source_name)
        self._triggered = False
        logger.info(f"Change detected: {source_name}")
        self._emit({"type": "change_detected", "source": source_name})

    def _should_trigger(self) -> bool:
        """Check if inactivity trigger should fire."""
        if self._last_change_time is None:
            return False
        if self._triggered:
            return False
        elapsed = time.time() - self._last_change_time
        return elapsed >= self.cfg.inactivity_trigger

    def _classify_source(self, doc_name: str) -> str:
        """Classify document. Returns a readable name for the user."""
        name_lower = doc_name.lower()
        if name_lower in ("panbot", "panbot-chat"):
            return "panbot-notebook"
        if name_lower in ("panbot-prompt", "panbot-prompts"):
            return "panbot-prompt"
        # Use actual document name instead of generic "project-pdf"
        return doc_name

    def process_chat(self, text: str) -> str:
        """Handle a chat instruction from the monitor dashboard.

        Queues the text as an instruction and triggers processing immediately.
        Returns a status string for the UI.
        """
        logger.info(f"Chat instruction received: {text[:80]}")
        self._changed_sources.append("chat")
        self._last_change_time = time.time() - self.cfg.inactivity_trigger
        self._triggered = False
        self._emit({"type": "change_detected", "source": "chat", "text": text})
        # Run synchronously (called from executor thread)
        # Temporarily inject the instruction text
        self._chat_instruction = text
        try:
            self.process_once()
        except Exception as e:
            logger.error(f"Chat processing failed: {e}")
            return f"Error: {e}"
        finally:
            self._chat_instruction = None
        return "Processing complete."

    def process_once(self) -> None:
        """Run one full ghostwriting cycle.

        Always:
        1. OCR handwriting from changed sources
        2. Ghostwrite changes to markdown
        3. Build versioned PDF (doc-vN.pdf)
        4. Upload current + previous version to RM (only 2 visible)
        5. Update panbot notebook with recognized text + changes
        """
        import shutil
        from datetime import datetime, timezone
        from .panbot_ghostwriter import Ghostwriter
        from .panbot_versions import VersionManager

        vm = VersionManager(self.cfg)
        gw = Ghostwriter(llm_provider=self.cfg.llm_provider)
        project_dir = Path(self.cfg.project_dir)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        sources = list(self._changed_sources)
        self._changed_sources.clear()
        logger.info(f"Processing triggered by: {', '.join(sources)}")
        self._emit({"type": "processing", "step": "start", "detail": f"Triggered by {', '.join(sources)}"})

        # --- 1. Load project config ---
        try:
            from .config import ProjectConfig
            project_config = ProjectConfig.from_directory(str(project_dir))
            project_title = project_config.title
            doc_name = project_config.project_name
        except Exception:
            project_config = None
            project_title = project_dir.name
            doc_name = project_dir.name

        # --- 2. Notify user (add page to panbot notebook) ---
        try:
            self._update_panbot_notebook(
                vm, now, doc_name, "Processing your changes...", {}, "Processing"
            )
        except Exception as e:
            logger.warning(f"Processing notification failed: {e}")

        # --- 3. Extract instructions from changed sources ---
        instruction_parts = []
        project_prompts = []

        # Chat instructions bypass OCR extraction
        chat_text = getattr(self, "_chat_instruction", None)
        if chat_text:
            instruction_parts.append(chat_text)

        for source in sources:
            if source == "chat":
                continue  # already handled above
            try:
                text = self._extract_instruction(source)
                if source == "panbot-prompt":
                    project_prompts.append(text)
                else:
                    instruction_parts.append(text)
            except Exception as e:
                logger.error(f"Extract from {source}: {e}")

        instruction = "\n\n".join(instruction_parts) if instruction_parts else ""
        if not instruction and not project_prompts:
            logger.warning("No instructions extracted (likely rate limited), will retry.")
            # Don't acknowledge — changes will be re-detected and retried
            # Sleep to avoid hammering the API
            time.sleep(120)
            return

        if not instruction:
            instruction = "Apply project prompt guidelines to improve the document."

        # --- 4. Classify and route instructions ---
        self._emit({"type": "processing", "step": "classify", "detail": instruction[:100]})
        actions, edits = self._classify_instructions(instruction)
        action_results = []

        # --- 4a. Execute actions (skills, shell commands) ---
        for action in actions:
            try:
                result = self._execute_action(action, project_dir)
                action_results.append(f"Action: {action[:50]}\nResult: {result}")
                logger.info(f"Action completed: {action[:50]}")
            except Exception as e:
                action_results.append(f"Action: {action[:50]}\nError: {e}")
                logger.error(f"Action failed: {e}")

        # --- 4b. Ghostwrite document edits ---
        # Include action results so ghostwriter can reference generated files
        edit_parts = list(edits)
        if action_results:
            # Check if logo was generated
            logo_path = project_dir / "panbot-output-logo.svg"
            if logo_path.exists():
                edit_parts.append("A logo was generated at panbot-output-logo.svg — embed it in the document with ![Logo](panbot-output-logo.svg)")

        edit_instruction = "\n\n".join(edit_parts) if edit_parts else ""
        changes = {}
        if edit_instruction:
            self._emit({"type": "processing", "step": "ghostwrite", "detail": edit_instruction[:80]})
            try:
                changes = gw.ghostwrite(
                    project_dir=project_dir,
                    instruction=edit_instruction,
                    project_prompts=project_prompts,
                    project_title=project_title,
                )
            except Exception as e:
                logger.error(f"Ghostwriting failed: {e}")

        if not changes and not action_results:
            logger.info("No changes produced.")
            self._update_panbot_notebook(
                vm, now, doc_name, instruction, {}, "No changes — instruction unclear"
            )
            self._acknowledge_changes()
            return

        # --- 5. Build versioned PDF ---
        self._emit({"type": "processing", "step": "build"})
        try:
            from .pandoc_utils import build_document
            pdf_path = build_document(str(project_dir), "pdf", project_config)
        except Exception as e:
            logger.error(f"PDF build failed: {e}")
            try:
                self._update_panbot_notebook(vm, now, doc_name, f"Build failed: {e}", {}, "Build error")
            except Exception:
                pass
            return

        version_num = vm.snapshot(Path(pdf_path), source=", ".join(sources))
        versioned_pdf = project_dir / f"{doc_name}-v{version_num}.pdf"
        shutil.copy2(pdf_path, versioned_pdf)
        logger.info(f"Created v{version_num}")

        # --- 5b. Git commit changed markdown files ---
        if changes:
            self._git_commit_changes(project_dir, changes, version_num, instruction)

        # --- 6. Build diff ---
        diff_path = vm.build_diff()

        # --- 7. Upload to RM with retry on rate limit ---
        self._emit({"type": "processing", "step": "upload"})
        self._upload_with_retry(versioned_pdf, vm, version_num, doc_name, project_dir)

        # --- 8. Update panbot notebook ---
        self._emit({"type": "processing", "step": "notify"})
        self._emit({"type": "version", "number": version_num,
                    "files_changed": list(changes.keys()),
                    "timestamp": now})
        full_report = instruction
        if action_results:
            full_report += "\n\nACTION RESULTS:\n" + "\n".join(action_results)

        self._update_panbot_notebook(
            vm, now, doc_name, full_report, changes,
            f"v{version_num} ready"
        )
        self._emit({"type": "processing", "step": "done"})
        self._acknowledge_changes()
        logger.info(f"v{version_num} complete.")

    def _classify_instructions(self, instruction: str) -> tuple[list[str], list[str]]:
        """Classify instruction lines into actions vs document edits.

        Actions are things that require executing a command or skill:
        - "fais un logo" / "make a logo" → action (image generation)
        - "utilise google media" → action (skill invocation)
        - "build a pdf" → action (build command)

        Edits are document modifications:
        - "rewrite section 2" → edit
        - "add a conclusion" → edit

        Returns (actions, edits).
        """
        import os

        action_keywords = [
            # Image/media generation
            "logo", "image", "photo", "illustration", "dessine", "dessin",
            "google media", "google-media", "media skill",
            # Build commands
            "build a pdf", "build pdf", "construis un pdf",
            # General action patterns
            "utilise", "use skill", "execute", "run", "lance",
        ]
        # Test/gibberish patterns to ignore
        ignore_keywords = [
            "test handwriting", "one more thing", "better handwriting",
            "handwriting recognition",
        ]

        actions = []
        edits = []

        for line in instruction.split("\n"):
            line = line.strip()
            if not line:
                continue

            line_lower = line.lower()

            # Skip test phrases
            if any(k in line_lower for k in ignore_keywords):
                continue

            # Check if it's an action
            if any(k in line_lower for k in action_keywords):
                actions.append(line)
            else:
                edits.append(line)

        return actions, edits

    def _execute_action(self, action: str, project_dir: Path) -> str:
        """Execute an action instruction. Returns result description."""
        import os

        action_lower = action.lower()

        # --- Logo / image generation ---
        if any(k in action_lower for k in ["logo", "image", "illustration", "dessine"]):
            return self._action_generate_image(action, project_dir)

        # --- Build PDF ---
        if any(k in action_lower for k in ["build a pdf", "build pdf", "construis un pdf"]):
            try:
                from .pandoc_utils import build_document
                from .config import ProjectConfig
                pc = ProjectConfig.from_directory(str(project_dir))
                pdf = build_document(str(project_dir), "pdf", pc)
                return f"PDF built: {pdf}"
            except Exception as e:
                return f"Build failed: {e}"

        # --- Generic: use LLM to interpret and execute ---
        return self._action_llm_execute(action, project_dir)

    def _action_generate_image(self, action: str, project_dir: Path) -> str:
        """Generate an image/logo using LLM description + SVG or external API."""
        import os

        # Use GROQ to generate an SVG logo description
        try:
            import groq
            client = groq.Groq(api_key=os.getenv("GROQ_API_KEY"))
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": (
                        "You are a logo designer. Generate a complete, valid SVG logo based on the user's description. "
                        "Return ONLY the SVG code, no commentary. Use simple shapes, bold colors, and clear text. "
                        "The SVG should be 200x200 pixels with a viewBox of 0 0 200 200."
                    )},
                    {"role": "user", "content": action},
                ],
                temperature=0.7,
                max_tokens=2048,
            )
            svg_content = response.choices[0].message.content.strip()

            # Extract SVG if wrapped in code block
            if "```" in svg_content:
                import re
                match = re.search(r"```(?:svg|xml)?\s*\n(.*?)```", svg_content, re.DOTALL)
                if match:
                    svg_content = match.group(1).strip()

            # Save SVG
            svg_path = project_dir / "panbot-output-logo.svg"
            svg_path.write_text(svg_content)

            # Convert to PDF for RM
            try:
                import cairosvg
                pdf_path = project_dir / "panbot-output-logo.pdf"
                cairosvg.svg2pdf(bytestring=svg_content.encode(), write_to=str(pdf_path))
                return f"Logo generated: {svg_path.name} + {pdf_path.name}"
            except Exception:
                return f"Logo SVG generated: {svg_path.name} (PDF conversion failed)"

        except Exception as e:
            return f"Image generation failed: {e}"

    def _action_llm_execute(self, action: str, project_dir: Path) -> str:
        """Use LLM to interpret and execute a generic action."""
        import os

        try:
            import groq
            client = groq.Groq(api_key=os.getenv("GROQ_API_KEY"))
            response = client.chat.completions.create(
                model="llama-3.3-70b-versatile",
                messages=[
                    {"role": "system", "content": (
                        "You are Panbot, an assistant that executes tasks for a document project. "
                        "The user gave a handwritten instruction. Interpret what they want and provide "
                        "a clear text response describing what was done or what should be done. "
                        "Be concise. Respond in the same language as the instruction."
                    )},
                    {"role": "user", "content": f"Instruction: {action}\nProject directory: {project_dir}"},
                ],
                temperature=0.3,
                max_tokens=512,
            )
            return response.choices[0].message.content.strip()
        except Exception as e:
            return f"Could not execute: {e}"

    def _update_panbot_notebook(
        self, vm, now, doc_name, instruction, changes, status_line
    ):
        """Update the panbot notebook in-place: add typed status page."""
        import json
        import zipfile
        import uuid as uuid_mod
        from io import BytesIO

        versions = vm.list_versions()
        vn = versions[-1]["version"] if versions else 0

        # Short changelog format
        msg = f"v{vn} — {now}\n"
        if changes:
            msg += "Changed: " + ", ".join(changes.keys()) + "\n"
        if instruction:
            # First line of instruction only
            first_line = instruction.split("\n")[0].strip()[:60]
            msg += f"Input: {first_line}\n"
        msg += f"\nWrite here for v{vn+1}."

        try:
            from rmscene import simple_text_document, write_blocks

            # Find panbot notebook doc ID
            panbot_doc = self._resolve_panbot_doc("panbot")
            if not panbot_doc:
                logger.warning("panbot notebook not found on RM")
                return

            doc_id = panbot_doc["ID"]

            with tempfile.TemporaryDirectory() as tmp:
                tmp = Path(tmp)
                rmdoc = tmp / "panbot.rmdoc"
                self.cloud.download_document(doc_id, rmdoc)

                extract_dir = tmp / "ex"
                with zipfile.ZipFile(rmdoc) as zf:
                    zf.extractall(extract_dir)

                content_path = extract_dir / f"{doc_id}.content"
                content = json.loads(content_path.read_text())

                # Remove all handwritten pages, keep only typed status
                # Delete old .rm files (handwriting)
                rm_dir = extract_dir / doc_id
                if rm_dir.exists():
                    for old_rm in rm_dir.glob("*.rm"):
                        old_rm.unlink()

                # Create single typed status page
                new_page_id = str(uuid_mod.uuid4())
                blocks = list(simple_text_document(msg))
                rm_buf = BytesIO()
                write_blocks(rm_buf, blocks)
                rm_data = rm_buf.getvalue()

                rm_path = rm_dir / f"{new_page_id}.rm"
                rm_path.parent.mkdir(parents=True, exist_ok=True)
                rm_path.write_bytes(rm_data)

                # Replace all pages with just the status page
                import time as time_mod
                now_ms = str(int(time_mod.time() * 1000))
                content["cPages"]["pages"] = [{
                    "id": new_page_id,
                    "idx": {"timestamp": "2:1", "value": "a"},
                    "modifed": now_ms,
                    "template": {"timestamp": "1:1", "value": "Blank"},
                }]
                content_path.write_text(json.dumps(content))

                # Re-upload notebook blobs
                self._upload_notebook(doc_id, extract_dir)
                logger.info(f"Panbot notebook updated with status page")

        except Exception as e:
            logger.warning(f"Notebook update failed: {e}")

    def _resolve_panbot_doc(self, name: str) -> dict | None:
        """Find a document by name in the panbot folder. Uses cache for speed."""
        # Check known_docs cache first
        if not hasattr(self, "_doc_id_cache"):
            self._doc_id_cache = {}

        if name in self._doc_id_cache:
            doc_id = self._doc_id_cache[name]
            try:
                root = self.cloud._get_root()
                root_blob = self.cloud._get_blob(root["hash"])
                root_index = self.cloud._parse_index(root_blob.decode("utf-8"))
                for entry in root_index:
                    if entry["id"] == doc_id:
                        return self.cloud._entry_to_doc(entry)
            except Exception:
                pass

        # Scan folder docs (uses the known state from _get_folder_docs)
        for doc in self._get_folder_docs():
            if doc.get("VissibleName") == name:
                self._doc_id_cache[name] = doc["ID"]
                return doc
        return None

    def _upload_notebook(self, doc_id: str, extract_dir: Path) -> None:
        """Upload all blobs for a notebook and update root index."""
        import hashlib

        files_to_upload = []
        for f in sorted(extract_dir.rglob("*")):
            if f.is_file():
                rel = str(f.relative_to(extract_dir))
                files_to_upload.append((rel, f.read_bytes()))

        sub_entries = []
        total_size = 0
        for filename, data in files_to_upload:
            blob_hash = self.cloud._hash_blob(data)
            self.cloud._put_blob(blob_hash, data, f"{doc_id}.content")
            sub_entries.append({
                "hash": blob_hash, "type": "0", "id": filename,
                "subfiles": 0, "size": len(data),
            })
            total_size += len(data)

        sub_index_text = self.cloud._build_index(sub_entries)
        sub_index_data = sub_index_text.encode("utf-8")
        sub_index_hash = self.cloud._hash_index_v3(sub_entries)
        self.cloud._put_blob(sub_index_hash, sub_index_data, f"{doc_id}.content")

        num_files = len(files_to_upload)

        def update_root(entries):
            new_entries = [e for e in entries if e["id"] != doc_id]
            new_entries.append({
                "hash": sub_index_hash, "type": "80000000",
                "id": doc_id, "subfiles": num_files, "size": total_size,
            })
            return new_entries

        self.cloud._update_root_with_retry(update_root)

    def _extract_instruction(self, source: str) -> str:
        """Download notebook, extract strokes, render to PNG, OCR."""
        import zipfile
        from pathlib import Path as P

        if source == "panbot-notebook":
            doc_name = "panbot"
        elif source == "panbot-prompt":
            doc_name = "panbot-prompt"
        else:
            project_dir = Path(self.cfg.project_dir)
            try:
                from .config import ProjectConfig
                pc = ProjectConfig.from_directory(str(project_dir))
                doc_name = pc.project_name
            except Exception:
                doc_name = project_dir.name

        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = P(tmp)
            output = tmp_path / f"{doc_name}.rmdoc"

            # Resolve doc by name in folder
            doc = self._resolve_panbot_doc(doc_name)
            if not doc:
                # Fall back to name resolution via list
                self.cloud.download_by_name(doc_name, str(output))
            else:
                self.cloud.download_document(doc["ID"], output)

            # Extract strokes and OCR each page
            with zipfile.ZipFile(output) as zf:
                zf.extractall(tmp_path / "ex")

            rm_files = sorted((tmp_path / "ex").rglob("*.rm"))
            if not rm_files:
                return ""

            from rmscene import read_blocks
            from .ocr_providers import ocr_smart, ocr_with_visual_context
            import cairosvg
            is_pdf_annotation = source not in ("panbot-notebook", "panbot-prompt")

            # For PDF annotations, get the PDF for page overlay
            pdf_path = None
            if is_pdf_annotation:
                pdf_path = Path(self.cfg.project_dir) / "my-panbot-test.pdf"
                if not pdf_path.exists():
                    # Try to find any PDF
                    pdfs = list(Path(self.cfg.project_dir).glob("*.pdf"))
                    pdf_path = pdfs[0] if pdfs else None

            all_text = []
            for pi, rm_file in enumerate(rm_files):
                try:
                    with open(rm_file, "rb") as f:
                        blocks = list(read_blocks(f))

                    lines = []
                    for b in blocks:
                        if type(b).__name__ == "SceneLineItemBlock":
                            if (hasattr(b.item, "value")
                                    and hasattr(b.item.value, "points")
                                    and b.item.deleted_length == 0):
                                pts = [(p.x, p.y) for p in b.item.value.points]
                                if pts:
                                    lines.append(pts)

                    if not lines:
                        continue

                    # Render annotation to cropped PNG
                    all_pts = [p for l in lines for p in l]
                    mn_x = min(p[0] for p in all_pts)
                    mn_y = min(p[1] for p in all_pts)
                    mx_x = max(p[0] for p in all_pts)
                    mx_y = max(p[1] for p in all_pts)
                    w, h = mx_x - mn_x + 40, mx_y - mn_y + 40

                    svg = (
                        f'<svg xmlns="http://www.w3.org/2000/svg" '
                        f'viewBox="{mn_x-20} {mn_y-20} {w} {h}" '
                        f'width="{int(w)}" height="{int(h)}" '
                        f'style="background:white">\n'
                    )
                    for pts in lines:
                        path = "M " + " L ".join(
                            f"{x:.1f} {y:.1f}" for x, y in pts
                        )
                        svg += f'  <path d="{path}" fill="none" stroke="black" stroke-width="2"/>\n'
                    svg += "</svg>"

                    annot_png = tmp_path / f"annot{pi}.png"
                    cairosvg.svg2png(
                        bytestring=svg.encode(),
                        write_to=str(annot_png),
                        output_width=int(w),
                        output_height=int(h),
                    )

                    # For PDF annotations: create overlay on PDF page for context
                    overlay_png = None
                    if is_pdf_annotation and pdf_path and pdf_path.exists():
                        try:
                            import subprocess
                            from PIL import Image
                            # Render annotation at full page size
                            full_svg = f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1404 1872" width="1404" height="1872">\n'
                            for pts in lines:
                                path = "M " + " L ".join(f"{x+702:.1f} {y:.1f}" for x, y in pts)
                                full_svg += f'  <path d="{path}" fill="none" stroke="black" stroke-width="3"/>\n'
                            full_svg += "</svg>"
                            annot_full = tmp_path / f"annot_full{pi}.png"
                            cairosvg.svg2png(bytestring=full_svg.encode(), write_to=str(annot_full), output_width=1275, output_height=1650)

                            # Extract PDF page as image
                            subprocess.run([
                                "pdftoppm", "-png", "-f", str(pi + 1), "-l", str(pi + 1),
                                "-r", "150", str(pdf_path), str(tmp_path / "pdfpg")
                            ], capture_output=True)
                            page_pngs = sorted(tmp_path.glob("pdfpg-*.png"))
                            if page_pngs:
                                page_img = Image.open(page_pngs[0]).convert("RGBA")
                                annot_img = Image.open(annot_full).convert("RGBA")
                                annot_resized = annot_img.resize(page_img.size, Image.LANCZOS)
                                overlay = Image.alpha_composite(page_img, annot_resized)
                                overlay_png = tmp_path / f"overlay{pi}.png"
                                overlay.save(str(overlay_png))
                                for p in page_pngs:
                                    p.unlink()
                        except Exception as e:
                            logger.warning(f"Page overlay failed: {e}")

                    # Always use visual understanding (catches arrows, circles, drawings)
                    # With page overlay for PDFs, without for notebooks
                    text, conf = ocr_with_visual_context(annot_png, overlay_png)
                    prov = "visual-context" if overlay_png else "visual"

                    if text:
                        all_text.append(text)
                        logger.info(f"Page {pi+1}: OCR ({prov}, {conf:.2f}): {text[:60]}")
                        self._emit({"type": "ocr_result", "pages": [
                            {"page": pi + 1, "text": text, "confidence": conf, "provider": prov}
                        ]})
                except Exception as e:
                    logger.error(f"Page {pi+1} extraction failed: {e}")

            return "\n\n".join(all_text)

    def run(self) -> None:
        """Main daemon loop."""
        self._setup_logging()

        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.pid_file.write_text(str(os.getpid()))

        def handle_signal(sig, frame):
            logger.info("Shutting down...")
            self._running = False

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        self._running = True

        # Start monitor dashboard
        if self._monitor is None:
            try:
                from .panbot_monitor import PanbotMonitor
                self._monitor = PanbotMonitor()
                self._monitor.start_background()
            except Exception as e:
                logger.warning(f"Monitor dashboard disabled: {e}")

        # Wire chat callback so the dashboard can trigger processing
        if self._monitor is not None:
            self._monitor._process_callback = self.process_chat

        project_name = Path(self.cfg.project_dir).name
        logger.info(f"Panbot started for {project_name}")
        logger.info(f"Watching: {self.cfg.folder}")
        logger.info(f"Poll: {self.cfg.poll_interval}s, Trigger: {self.cfg.inactivity_trigger}s")
        self._emit({"type": "status", "state": "polling"})
        self._emit({"type": "config", "project_name": project_name, "folder_path": self.cfg.folder})
        self._emit({"type": "poll_interval", "interval": self.cfg.poll_interval})

        # Send existing version history to monitor
        from .panbot_versions import VersionManager
        vm = VersionManager(self.cfg)
        for v in vm.list_versions():
            self._emit({"type": "version", "number": v["version"],
                        "timestamp": v.get("timestamp", ""), "files_changed": [],
                        "source": v.get("source", "")})

        # Resolve folder with retry on rate limits
        for attempt in range(10):
            try:
                self._folder_id = self._resolve_folder()
                logger.info(f"Folder resolved: {self._folder_id}")
                break
            except Exception as e:
                if "429" in str(e) or "Too Many" in str(e):
                    wait = 60 * (attempt + 1)
                    logger.warning(f"Rate limited on startup, waiting {wait}s (attempt {attempt+1})")
                    self._emit({"type": "status", "state": "rate_limited"})
                    time.sleep(wait)
                else:
                    logger.error(f"Failed to resolve folder: {e}")
                    self.cfg.pid_file.unlink(missing_ok=True)
                    raise
        else:
            logger.error("Could not resolve folder after 10 retries")
            self.cfg.pid_file.unlink(missing_ok=True)
            return

        try:
            for doc in self._get_folder_docs():
                self._known_state[doc["ID"]] = {
                    "Version": doc.get("Version"),
                    "ModifiedClient": doc.get("ModifiedClient"),
                }
        except Exception as e:
            logger.warning(f"Initial folder scan: {e}")
        logger.info(f"Tracking {len(self._known_state)} documents")

        # Adaptive polling: start at configured interval, back off on errors/quiet
        current_interval = self.cfg.poll_interval
        max_interval = self.cfg.poll_interval * 10  # 5 min max when quiet
        consecutive_errors = 0
        consecutive_quiet = 0

        while self._running:
            try:
                changed_docs = self._detect_changes()
                consecutive_errors = 0  # reset on success

                if changed_docs:
                    # Only fire _on_change_detected for genuinely new changes
                    # (not re-detections of the same unprocessed change)
                    if self._last_change_time is None or self._triggered:
                        for doc in changed_docs:
                            source = self._classify_source(doc["VissibleName"])
                            self._on_change_detected(source)
                        current_interval = self.cfg.poll_interval
                        consecutive_quiet = 0
                        self._emit({"type": "poll_interval", "interval": current_interval})
                else:
                    # No changes — gradually increase interval
                    consecutive_quiet += 1
                    if consecutive_quiet > 10:
                        prev = current_interval
                        current_interval = min(current_interval + 10, max_interval)
                        if current_interval != prev:
                            self._emit({"type": "poll_interval", "interval": current_interval})

                if self._should_trigger():
                    self._triggered = True
                    try:
                        self.process_once()
                        current_interval = self.cfg.poll_interval
                    except Exception as e:
                        logger.error(f"Processing failed: {e}")
                        # Back off after failed processing to avoid rate limit spiral
                        logger.info("Backing off 120s after failed processing")
                        time.sleep(120)

            except Exception as e:
                err_msg = str(e)
                if "429" in err_msg or "Too Many" in err_msg:
                    # Rate limited — exponential backoff, cap at 10 min
                    consecutive_errors += 1
                    backoff = min(60 * consecutive_errors, 600)
                    logger.warning(f"Rate limited, waiting {backoff}s (attempt {consecutive_errors})")
                    self._emit({"type": "rate_limited", "backoff": backoff, "attempt": consecutive_errors})
                    time.sleep(backoff)
                    self._emit({"type": "rate_limit_cleared"})
                    continue
                else:
                    logger.error(f"Poll error: {e}")
                    consecutive_errors += 1

            # Normal sleep — longer when idle or erroring
            sleep_time = current_interval
            if consecutive_errors > 0:
                sleep_time = min(current_interval * (consecutive_errors + 1), max_interval)
            time.sleep(sleep_time)

        self.cfg.pid_file.unlink(missing_ok=True)
        logger.info("Panbot stopped.")

    def _git_commit_changes(
        self,
        project_dir: Path,
        changes: dict,
        version_num: int,
        instruction: str,
    ) -> None:
        """Stage modified markdown files, commit, and optionally push."""
        try:
            result = subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=project_dir,
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                logger.debug("Not a git repo — skipping git commit")
                return

            md_files = [f for f in changes.keys() if f.endswith(".md")]
            if not md_files:
                return

            subprocess.run(
                ["git", "add", "--"] + md_files,
                cwd=project_dir,
                check=True,
                capture_output=True,
            )

            summary = instruction.strip().split("\n")[0][:80] if instruction else "apply edits"
            commit_msg = f"panbot: v{version_num} - {summary}"

            subprocess.run(
                ["git", "commit", "-m", commit_msg],
                cwd=project_dir,
                check=True,
                capture_output=True,
            )
            logger.info(f"Git commit: {commit_msg}")

            # Auto-push if configured
            if self.cfg.git_push:
                should_push = True
                if self.cfg.git_push_interval > 0:
                    should_push = (version_num % self.cfg.git_push_interval == 0)
                if should_push:
                    self._git_push(project_dir)

        except subprocess.CalledProcessError as e:
            logger.debug(f"Git commit skipped: {e.stderr.decode().strip()}")
        except Exception as e:
            logger.warning(f"Git commit failed: {e}")

    def _git_push(self, project_dir: Path) -> None:
        """Push current branch to remote origin."""
        try:
            # Get current branch
            result = subprocess.run(
                ["git", "rev-parse", "--abbrev-ref", "HEAD"],
                cwd=project_dir, capture_output=True, text=True,
            )
            branch = result.stdout.strip() if result.returncode == 0 else ""
            if self.cfg.git_branch:
                branch = self.cfg.git_branch

            if not branch:
                logger.warning("Git push skipped: no branch")
                return

            # Check if remote exists
            result = subprocess.run(
                ["git", "remote", "get-url", "origin"],
                cwd=project_dir, capture_output=True, text=True,
            )
            if result.returncode != 0:
                # No remote — set up from github_repo if available
                if self.cfg.github_repo:
                    subprocess.run(
                        ["git", "remote", "add", "origin",
                         f"https://github.com/{self.cfg.github_repo}.git"],
                        cwd=project_dir, check=True, capture_output=True,
                    )
                    logger.info(f"Added remote: {self.cfg.github_repo}")
                else:
                    logger.warning("Git push skipped: no remote origin")
                    return

            subprocess.run(
                ["git", "push", "origin", branch],
                cwd=project_dir, check=True, capture_output=True, text=True,
                timeout=60,
            )
            logger.info(f"Git pushed to origin/{branch}")
            self._emit({"type": "git_push", "branch": branch, "repo": self.cfg.github_repo})
        except subprocess.TimeoutExpired:
            logger.warning("Git push timed out (60s)")
        except subprocess.CalledProcessError as e:
            logger.warning(f"Git push failed: {e.stderr.strip() if e.stderr else e}")
        except Exception as e:
            logger.warning(f"Git push failed: {e}")


class PanbotMultiDaemon:
    """Manages multiple PanbotDaemon instances with a shared cloud client."""

    def __init__(self, configs: list[PanbotConfig], monitor=None):
        self._configs = configs
        self._monitor = monitor
        self._daemons: list[PanbotDaemon] = []
        self._running = False

    def _make_daemons(self) -> list[PanbotDaemon]:
        try:
            from .rm_cloud import RemarkableCloud
            shared_cloud = RemarkableCloud()
        except Exception:
            shared_cloud = None

        return [
            PanbotDaemon(cfg, cloud=shared_cloud, monitor=self._monitor)
            for cfg in self._configs
        ]

    def run(self) -> None:
        """Run all project daemons concurrently in daemon threads."""
        import threading

        self._daemons = self._make_daemons()
        self._running = True

        def handle_signal(sig, frame):
            logger.info("Multi-daemon shutting down...")
            self._running = False
            for d in self._daemons:
                d._running = False

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

        threads = []
        for d in self._daemons:
            name = d.cfg.name or Path(d.cfg.project_dir).name
            t = threading.Thread(target=d.run, name=f"panbot-{name}", daemon=True)
            t.start()
            threads.append(t)
            logger.info(f"Started daemon thread for {name}")

        for t in threads:
            t.join()

        logger.info("All panbot daemons stopped.")
