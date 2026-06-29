"""Panbot reMarkable Cloud client.

Pure Python replacement for rmapi. Uses the v3/v4 sync API.
"""

import hashlib
import io
import json
import os
import stat
import time
import uuid
import zipfile
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml

AUTH_HOST = "https://webapp-prod.cloud.remarkable.engineering"
SYNC_HOST = "https://internal.cloud.remarkable.com"
# Simple upload endpoint (v2). The server builds the document hash-tree itself,
# so PDFs/EPUBs can be uploaded without composing the blob tree client-side.
UPLOAD_HOST = "https://internal.cloud.remarkable.com"
CONFIG_PATH = Path.home() / ".config" / "panbot" / "remarkable.yaml"
TRACKING_DIR = Path.home() / ".config" / "panbot" / "tracking"
CACHE_DIR = Path.home() / ".config" / "panbot" / "cache"

API_TIMEOUT = 30.0
TRANSFER_TIMEOUT = 300.0
MAX_CONCURRENT_FETCHES = 200

# Canonical ``rm-filename`` for the root index blob (rmapi-js convention).
# The server also accepts ``<hash>.docSchema`` for root/sub-index blobs, but
# rejects ``<hash>.docSchema`` for leaf blobs (.metadata/.content/...), whose
# rm-filename must be the leaf id itself. Sub-index blobs use ``<id>.docSchema``.
ROOT_FILENAME = "root.docSchema"


@dataclass
class TrackingState:
    document_id: str
    document_name: str
    version: int
    modified_client: str
    last_checked: str = ""
    last_downloaded: str = ""


@dataclass
class ChangeResult:
    changed: bool
    document_name: str
    old_version: int
    new_version: int | None = None
    old_modified: str = ""
    new_modified: str | None = None
    minutes_since_last_change: float = 0.0


class RemarkableCloud:
    """Client for the reMarkable Cloud API (v3/v4 sync)."""

    def __init__(
        self,
        config_path: Path | None = None,
        transport: httpx.BaseTransport | None = None,
        tracking_dir: Path | None = None,
        cache_dir: Path | None = None,
    ):
        self._config_path = config_path or CONFIG_PATH
        self._tracking_dir = tracking_dir or TRACKING_DIR
        self._cache_dir = cache_dir or CACHE_DIR
        self._device_token: str | None = None
        self._device_id: str | None = None
        self._user_token: str | None = None
        self._transport = transport
        self._auth_lock = threading.Lock()
        # Persistent HTTP client for connection pooling (reuses TCP+TLS)
        self._client: httpx.Client | None = None
        self._client_lock = threading.Lock()
        # Root cache with TTL to avoid redundant /sync/v4/root calls
        self._root_cache: dict | None = None
        self._root_cache_time: float = 0.0
        self._root_cache_ttl: float = 10.0  # seconds
        self._load_config()

    def _get_persistent_client(self, timeout: float = API_TIMEOUT) -> httpx.Client:
        """Return a persistent httpx.Client with connection pooling."""
        if self._client is None or self._client.is_closed:
            with self._client_lock:
                if self._client is None or self._client.is_closed:
                    kwargs: dict = {"timeout": timeout}
                    if self._transport:
                        kwargs["transport"] = self._transport
                    self._client = httpx.Client(**kwargs)
        return self._client

    def _make_client(self, timeout: float = API_TIMEOUT) -> httpx.Client:
        kwargs: dict = {"timeout": timeout}
        if self._transport:
            kwargs["transport"] = self._transport
        return httpx.Client(**kwargs)

    def close(self) -> None:
        """Close the persistent HTTP client."""
        if self._client and not self._client.is_closed:
            self._client.close()

    def _load_config(self) -> None:
        if self._config_path.exists():
            config = yaml.safe_load(self._config_path.read_text())
            if config:
                self._device_token = config.get("device_token")
                self._device_id = config.get("device_id")

    def _save_config(self) -> None:
        self._config_path.parent.mkdir(parents=True, exist_ok=True)
        os.chmod(self._config_path.parent, stat.S_IRWXU)
        config = {
            "device_token": self._device_token,
            "device_id": self._device_id,
        }
        self._config_path.write_text(yaml.dump(config))
        os.chmod(self._config_path, stat.S_IRUSR | stat.S_IWUSR)

    def register_device(self, code: str) -> str:
        self._device_id = str(uuid.uuid4())
        payload = {
            "code": code,
            "deviceDesc": "desktop-linux",
            "deviceID": self._device_id,
        }
        with self._make_client() as client:
            resp = client.post(
                f"{AUTH_HOST}/token/json/2/device/new",
                json=payload,
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Device registration failed (HTTP {resp.status_code}): {resp.text}. "
                "Get a new code at https://my.remarkable.com/device/desktop/connect"
            )
        self._device_token = resp.text.strip()
        self._save_config()
        return self._device_token

    def _get_user_token(self) -> str:
        if not self._device_token:
            raise RuntimeError(
                "No device token configured. Run: panbot rm register"
            )
        with self._make_client() as client:
            resp = client.post(
                f"{AUTH_HOST}/token/json/2/user/new",
                headers={"Authorization": f"Bearer {self._device_token}"},
            )
        if resp.status_code == 401:
            raise RuntimeError(
                "Device token expired or revoked. Run: panbot rm register"
            )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to get user token (HTTP {resp.status_code}): {resp.text}"
            )
        self._user_token = resp.text.strip()
        return self._user_token

    def _ensure_authenticated(self) -> None:
        if not self._user_token:
            with self._auth_lock:
                if not self._user_token:
                    self._get_user_token()

    def _auth_headers(self) -> dict[str, str]:
        self._ensure_authenticated()
        return {
            "Authorization": f"Bearer {self._user_token}",
        }

    def _request_with_retry(  # type: ignore[no-untyped-def]
        self, method: str, url: str, **kwargs,
    ) -> httpx.Response:
        """Make an authenticated request with 401 retry and connection reuse."""
        kwargs.setdefault("headers", {}).update(self._auth_headers())
        timeout = kwargs.pop("timeout_val", API_TIMEOUT)
        client = self._get_persistent_client(timeout=timeout)
        try:
            resp = getattr(client, method)(url, **kwargs)
        except httpx.HTTPError:
            # Connection may be stale — recreate and retry
            with self._client_lock:
                if self._client is client:
                    try:
                        self._client.close()
                    except Exception:
                        pass
                    self._client = None
            client = self._get_persistent_client(timeout=timeout)
            resp = getattr(client, method)(url, **kwargs)
        if resp.status_code == 401:
            # Token expired — refresh and retry once
            self._user_token = None
            self._get_user_token()
            kwargs["headers"].update(self._auth_headers())
            resp = getattr(client, method)(url, **kwargs)
        return resp

    # --- v3/v4 sync API methods ---

    def _get_root(self, max_age: float | None = None) -> dict:
        """GET /sync/v4/root -> {hash, generation, schemaVersion}.

        Cached for ``max_age`` seconds (default: self._root_cache_ttl) to avoid
        redundant API calls within the same poll cycle.
        """
        ttl = max_age if max_age is not None else self._root_cache_ttl
        now = time.monotonic()
        if self._root_cache and (now - self._root_cache_time) < ttl:
            return self._root_cache
        resp = self._request_with_retry(
            "get", f"{SYNC_HOST}/sync/v4/root",
        )
        if resp.status_code != 200:
            raise RuntimeError(
                f"Failed to get sync root (HTTP {resp.status_code}): {resp.text}"
            )
        self._root_cache = resp.json()
        self._root_cache_time = now
        return self._root_cache

    def _get_blob(self, blob_hash: str, filename: str | None = None) -> bytes:
        """GET /sync/v3/files/<hash> -> raw blob content.

        Retries up to 3 times on 429 with exponential backoff.

        The server now requires an ``rm-filename`` header on GETs. Callers may
        pass an explicit ``filename``; otherwise we derive one from ``blob_hash``
        with a ``.docSchema`` suffix, which the server accepts for index blobs.
        """
        rm_filename = filename if filename is not None else f"{blob_hash}.docSchema"
        for attempt in range(3):
            resp = self._request_with_retry(
                "get", f"{SYNC_HOST}/sync/v3/files/{blob_hash}",
                headers={"rm-filename": rm_filename},
                timeout_val=TRANSFER_TIMEOUT,
            )
            if resp.status_code == 200:
                return resp.content
            if resp.status_code == 429:
                wait = 5 * (attempt + 1)
                time.sleep(wait)
                continue
            raise RuntimeError(
                f"Failed to get blob {blob_hash} (HTTP {resp.status_code})"
            )
        raise RuntimeError(
            f"Failed to get blob {blob_hash} (HTTP 429): rate limited after 3 retries"
        )

    def _parse_index(self, content: str) -> list[dict]:
        """Parse index text into list of {hash, type, id, subfiles, size}.

        Supports schema 3 and schema 4. Schema 4 has an extra info line
        (`0:{id}:{count}:{size}`) right after the version line, which is
        skipped. The schema version itself is also recoverable via
        :meth:`_parse_index_with_schema`.
        """
        return self._parse_index_with_schema(content)[0]

    def _parse_index_with_schema(self, content: str) -> tuple[list[dict], str]:
        """Like ``_parse_index`` but also returns the schema version ("3"/"4")."""
        lines = content.strip().split("\n")
        if not lines:
            return [], "3"
        schema = lines[0].strip() or "3"
        body = lines[1:]
        if schema == "4" and body:
            # Skip the info line: `0:{id_or_dot}:{count}:{size}`
            body = body[1:]
        entries = []
        for line in body:
            if not line.strip():
                continue
            parts = line.split(":")
            if len(parts) < 5:
                continue
            entries.append({
                "hash": parts[0],
                "type": parts[1],
                "id": parts[2],
                "subfiles": int(parts[3]),
                "size": int(parts[4]),
            })
        return entries, schema

    def _get_doc_metadata(self, doc_entry: dict) -> dict:
        """Fetch sub-index, find .metadata blob, return parsed JSON."""
        sub_blob = self._get_blob(
            doc_entry["hash"], filename=f"{doc_entry['id']}.docSchema",
        )
        sub_index = self._parse_index(sub_blob.decode("utf-8"))
        for entry in sub_index:
            if entry["id"].endswith(".metadata"):
                meta_blob = self._get_blob(entry["hash"], filename=entry["id"])
                return json.loads(meta_blob.decode("utf-8"))
        return {}

    def _entry_to_doc(self, entry: dict) -> dict | None:
        """Convert a root index entry to a document dict by fetching metadata."""
        try:
            metadata = self._get_doc_metadata(entry)
        except Exception:
            return None
        if not metadata:
            return None

        modified = ""
        if metadata.get("lastModified"):
            try:
                ts_ms = int(metadata["lastModified"])
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                modified = dt.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
            except (ValueError, OSError):
                modified = metadata["lastModified"]

        return {
            "ID": entry["id"],
            "VissibleName": metadata.get("visibleName", ""),
            "Type": metadata.get("type", "DocumentType"),
            "Parent": metadata.get("parent", ""),
            "Version": metadata.get("version", 0),
            "ModifiedClient": modified,
        }

    def _load_cache(self, root: dict | None = None) -> list[dict] | None:
        """Load cached doc list if root generation hasn't changed.

        Accepts an optional pre-fetched root dict to avoid redundant API calls.
        """
        cache_meta = self._cache_dir / "root.yaml"
        cache_docs = self._cache_dir / "docs.json"
        if not cache_meta.exists() or not cache_docs.exists():
            return None
        try:
            meta = yaml.safe_load(cache_meta.read_text())
            if root is None:
                root = self._get_root()
            if root["generation"] == meta.get("generation"):
                return json.loads(cache_docs.read_text())
        except Exception:
            return None
        return None

    def _save_cache(self, root: dict, docs: list[dict]) -> None:
        """Save doc list and root info to cache."""
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        (self._cache_dir / "root.yaml").write_text(yaml.dump({
            "hash": root["hash"],
            "generation": root["generation"],
        }))
        (self._cache_dir / "docs.json").write_text(json.dumps(docs))

    def _fetch_blob_safe(
        self, blob_hash: str, filename: str | None = None,
    ) -> bytes | None:
        """Fetch blob, return None on error instead of raising."""
        try:
            return self._get_blob(blob_hash, filename=filename)
        except Exception:
            return None

    def list_documents_v3(self, *, full_rescan: bool = False) -> list[dict]:
        """List all docs using v3/v4 protocol with concurrent fetching.

        Two paths:
        - **Cache hit**: cached docs returned if root generation unchanged.
        - **Incremental refresh**: when generation changed and a cache exists,
          only re-fetch sub-indexes whose hash changed (added or modified
          docs). Unchanged entries reuse cached metadata.
        - **Cold scan**: full two-phase parallel fetch (used on first run or
          when ``full_rescan=True``).

        On a 2700-doc account the incremental path turns a ~60s scan into
        a few hundred ms when only a handful of docs changed.
        """
        root = self._get_root()
        cached = self._load_cache(root=root)
        if cached is not None and not full_rescan:
            return cached

        # Try incremental path: cache may exist but be stale
        if not full_rescan:
            cached_docs = self._load_cached_docs_no_root_check()
            if cached_docs is not None:
                refreshed = self._incremental_refresh(root, cached_docs)
                if refreshed is not None:
                    self._save_cache(root, refreshed)
                    return refreshed

        # Cold scan: fetch every sub-index + metadata
        root_blob = self._get_blob(root["hash"], filename=ROOT_FILENAME)
        root_index = self._parse_index(root_blob.decode("utf-8"))
        docs = self._scan_full(root_index)
        self._save_cache(root, docs)
        return docs

    def _load_cached_docs_no_root_check(self) -> list[dict] | None:
        """Return cached docs.json regardless of root generation."""
        cache_docs = self._cache_dir / "docs.json"
        if not cache_docs.exists():
            return None
        try:
            return json.loads(cache_docs.read_text())
        except Exception:
            return None

    def _scan_full(self, root_index: list[dict]) -> list[dict]:
        """Two-phase parallel scan: sub-indexes then metadata blobs."""
        sub_index_map: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCHES) as pool:
            future_to_entry = {
                pool.submit(
                    self._fetch_blob_safe, entry["hash"],
                    f"{entry['id']}.docSchema",
                ): entry
                for entry in root_index
            }
            for future in as_completed(future_to_entry):
                entry = future_to_entry[future]
                blob = future.result()
                if blob is not None:
                    try:
                        sub_index_map[entry["id"]] = self._parse_index(
                            blob.decode("utf-8")
                        )
                    except Exception:
                        pass

        # (hash -> (doc_id, metadata_filename)) for correct rm-filename header
        meta_hash_to_doc_id: dict[str, tuple[str, str]] = {}
        for doc_id, sub_entries in sub_index_map.items():
            for sub in sub_entries:
                if sub["id"].endswith(".metadata"):
                    meta_hash_to_doc_id[sub["hash"]] = (doc_id, sub["id"])
                    break

        metadata_map: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCHES) as pool:
            future_to_hash = {
                pool.submit(self._get_blob, h, meta_id): h
                for h, (_, meta_id) in meta_hash_to_doc_id.items()
            }
            for future in as_completed(future_to_hash):
                h = future_to_hash[future]
                doc_id = meta_hash_to_doc_id[h][0]
                try:
                    blob = future.result()
                    if blob is not None:
                        metadata_map[doc_id] = json.loads(blob.decode("utf-8"))
                except Exception:
                    pass

        return [
            d for d in (
                self._build_doc_dict(entry["id"], metadata_map.get(entry["id"]))
                for entry in root_index
            )
            if d is not None
        ]

    def _build_doc_dict(self, doc_id: str, metadata: dict | None) -> dict | None:
        if not metadata:
            return None
        modified = ""
        if metadata.get("lastModified"):
            try:
                ts_ms = int(metadata["lastModified"])
                dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
                modified = dt.strftime("%Y-%m-%dT%H:%M:%S.000000000Z")
            except (ValueError, OSError):
                modified = metadata["lastModified"]
        return {
            "ID": doc_id,
            "VissibleName": metadata.get("visibleName", ""),
            "Type": metadata.get("type", "DocumentType"),
            "Parent": metadata.get("parent", ""),
            "Version": metadata.get("version", 0),
            "ModifiedClient": modified,
        }

    def _incremental_refresh(
        self, root: dict, cached_docs: list[dict],
    ) -> list[dict] | None:
        """Refresh only the docs whose root entry hash changed.

        Returns the refreshed full doc list, or ``None`` if too few entries
        match cache to be useful (forces a cold scan).
        """
        try:
            root_blob = self._get_blob(root["hash"], filename=ROOT_FILENAME)
            root_index = self._parse_index(root_blob.decode("utf-8"))
        except Exception:
            return None

        cached_by_id = {d["ID"]: d for d in cached_docs}
        # Track which root entry hashes we've seen for each doc — if cache has
        # an "_entry_hash" we use that; otherwise we conservatively re-fetch.
        # First pass: classify each root entry
        unchanged: list[dict] = []
        to_refetch: list[dict] = []
        for entry in root_index:
            cached_doc = cached_by_id.get(entry["id"])
            cached_hash = cached_doc.get("_entry_hash") if cached_doc else None
            if cached_doc and cached_hash == entry["hash"]:
                unchanged.append(cached_doc)
            else:
                to_refetch.append(entry)

        # If nothing in cache matches, fall back to cold scan
        if not unchanged and len(root_index) > 50:
            return None

        # Refetch only what changed
        if to_refetch:
            sub_map: dict[str, list[dict]] = {}
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCHES) as pool:
                fut = {
                    pool.submit(
                        self._fetch_blob_safe, e["hash"],
                        f"{e['id']}.docSchema",
                    ): e
                    for e in to_refetch
                }
                for f in as_completed(fut):
                    blob = f.result()
                    if blob:
                        try:
                            sub_map[fut[f]["id"]] = self._parse_index(
                                blob.decode("utf-8")
                            )
                        except Exception:
                            pass
            meta_hash_to_id: dict[str, tuple[str, str]] = {}
            for doc_id, subs in sub_map.items():
                for s in subs:
                    if s["id"].endswith(".metadata"):
                        meta_hash_to_id[s["hash"]] = (doc_id, s["id"])
                        break
            meta_map: dict[str, dict] = {}
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCHES) as pool:
                fut = {
                    pool.submit(self._get_blob, h, meta_id): h
                    for h, (_, meta_id) in meta_hash_to_id.items()
                }
                for f in as_completed(fut):
                    h = fut[f]
                    doc_id = meta_hash_to_id[h][0]
                    try:
                        blob = f.result()
                        if blob:
                            meta_map[doc_id] = json.loads(blob.decode("utf-8"))
                    except Exception:
                        pass
            for entry in to_refetch:
                doc = self._build_doc_dict(entry["id"], meta_map.get(entry["id"]))
                if doc:
                    doc["_entry_hash"] = entry["hash"]
                    unchanged.append(doc)
        # Stamp _entry_hash on previously-cached entries too (for future runs)
        present_ids = {e["id"] for e in root_index}
        out = [d for d in unchanged if d["ID"] in present_ids]
        # Stamp hashes for any entry in root_index without one
        hash_by_id = {e["id"]: e["hash"] for e in root_index}
        for d in out:
            if "_entry_hash" not in d:
                d["_entry_hash"] = hash_by_id.get(d["ID"], "")
        return out

    def _get_doc_sub_index(self, doc_id: str, root: dict | None = None) -> list[dict]:
        """Get the sub-index entries for a document by its ID."""
        if root is None:
            root = self._get_root()
        root_blob = self._get_blob(root["hash"], filename=ROOT_FILENAME)
        root_index = self._parse_index(root_blob.decode("utf-8"))
        for entry in root_index:
            if entry["id"] == doc_id:
                sub_blob = self._get_blob(
                    entry["hash"], filename=f"{entry['id']}.docSchema",
                )
                return self._parse_index(sub_blob.decode("utf-8"))
        raise RuntimeError(f"Document {doc_id} not found in sync root")

    def download_document_v3(self, doc_id: str, output_path: Path) -> Path:
        """Download doc by fetching all blobs from sub-index, create zip."""
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)

        sub_index = self._get_doc_sub_index(doc_id)
        if not sub_index:
            raise RuntimeError(f"Document {doc_id} has no files")

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
            for entry in sub_index:
                blob_data = self._get_blob(entry["hash"], filename=entry["id"])
                zf.writestr(entry["id"], blob_data)

        output_path.write_bytes(zip_buffer.getvalue())
        return output_path

    def list_folder_by_id(self, folder_id: str) -> list[dict]:
        """List documents inside a folder given its UUID directly.

        Scans only entries not yet in cache — much faster than list_folder_fast
        when cache is empty, as it avoids a full 2000+ doc cold scan.
        """
        root = self._get_root()
        root_blob = self._get_blob(root["hash"], filename=ROOT_FILENAME)
        root_index = self._parse_index(root_blob.decode("utf-8"))

        # Load cache to skip known entries
        cache_docs_path = self._cache_dir / "docs.json"
        cached_by_id: dict[str, dict] = {}
        if cache_docs_path.exists():
            try:
                for d in json.loads(cache_docs_path.read_text()):
                    cached_by_id[d["ID"]] = d
            except Exception:
                pass

        # Entries already known to be in folder (from cache)
        known_folder_docs = {
            did: d for did, d in cached_by_id.items()
            if d.get("Parent") == folder_id
        }

        # Entries not in cache — need to fetch metadata to check parent
        unknown_entries = [e for e in root_index if e["id"] not in cached_by_id]

        fresh_docs: list[dict] = []
        if unknown_entries:
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCHES) as pool:
                future_to_entry = {
                    pool.submit(self._entry_to_doc, entry): entry
                    for entry in unknown_entries
                }
                for future in as_completed(future_to_entry):
                    doc = future.result()
                    if doc and doc.get("Parent") == folder_id:
                        fresh_docs.append(doc)

        return list(known_folder_docs.values()) + fresh_docs

    def list_folder_fast(self, folder_path: str) -> list[dict]:
        """List documents inside a specific folder, using cache + targeted refresh.

        Much faster than list_documents() for large libraries — only fetches
        metadata for the folder itself and its children (not all 2000+ docs).
        Falls back to list_folder_by_id if cache is empty (avoids full scan).
        """
        cache_docs_path = self._cache_dir / "docs.json"

        # Resolve folder ID — use cache when available, otherwise scan root for folder
        if cache_docs_path.exists():
            cached_docs = json.loads(cache_docs_path.read_text())
            if cached_docs:
                try:
                    folder_id = self._resolve_folder_from_docs(cached_docs, folder_path)
                except RuntimeError:
                    folder_id = None
            else:
                folder_id = None
        else:
            cached_docs = []
            folder_id = None

        if folder_id is None:
            # Cache empty or folder not in cache — resolve folder by scanning root metadata
            folder_id = self._resolve_folder_id_from_root(folder_path)

        # Get current root generation
        root = self._get_root()
        cache_meta_path = self._cache_dir / "root.yaml"
        cached_meta = yaml.safe_load(cache_meta_path.read_text()) if cache_meta_path.exists() else {}

        if cached_docs and root["generation"] == cached_meta.get("generation"):
            # No changes at all — use cache
            return [d for d in cached_docs if d.get("Parent") == folder_id]

        # Generation changed or no cache — use targeted per-folder scan
        return self.list_folder_by_id(folder_id)

    def _resolve_folder_id_from_root(self, folder_path: str) -> str:
        """Resolve a folder path to UUID by scanning root index metadata only.

        Scans only root-level entries whose sub-index contains a .metadata blob
        of type CollectionType — much faster than a full list_documents() scan
        because it stops as soon as all path segments are found.
        """
        if not folder_path or folder_path == "/":
            return ""
        segments = [s for s in folder_path.strip("/").split("/") if s]

        root = self._get_root()
        root_blob = self._get_blob(root["hash"], filename=ROOT_FILENAME)
        root_index = self._parse_index(root_blob.decode("utf-8"))

        parent = ""
        for segment in segments:
            found_id: str | None = None
            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCHES) as pool:
                future_to_entry = {
                    pool.submit(self._entry_to_doc, entry): entry
                    for entry in root_index
                }
                for future in as_completed(future_to_entry):
                    doc = future.result()
                    if (doc
                            and doc.get("VissibleName") == segment
                            and doc.get("Type") == "CollectionType"
                            and doc.get("Parent", "") == parent):
                        found_id = doc["ID"]
                        # Cancel remaining futures — segment found
                        for f in future_to_entry:
                            f.cancel()
                        break
            if found_id is None:
                raise RuntimeError(
                    f"Folder '{segment}' not found in '{folder_path}'"
                )
            parent = found_id
        return parent

    def _resolve_folder_from_docs(self, docs: list[dict], folder_path: str) -> str:
        """Resolve folder path to ID using a doc list (cache or live)."""
        if not folder_path or folder_path == "/":
            return ""
        segments = [s for s in folder_path.strip("/").split("/") if s]
        parent = ""
        for segment in segments:
            found = False
            for doc in docs:
                if (doc.get("VissibleName") == segment
                        and doc.get("Type") == "CollectionType"
                        and doc.get("Parent", "") == parent):
                    parent = doc["ID"]
                    found = True
                    break
            if not found:
                raise RuntimeError(f"Folder '{segment}' not found in '{folder_path}'")
        return parent

    # --- Unified API methods ---

    def list_documents(self) -> list[dict]:
        """List all documents on reMarkable Cloud."""
        return self.list_documents_v3()

    def _resolve_name_to_doc(self, document_name: str) -> dict:
        """Find document by VissibleName. Returns first match."""
        docs = self.list_documents()
        for doc in docs:
            if doc.get("VissibleName") == document_name:
                return doc
        raise RuntimeError(f"Document '{document_name}' not found on reMarkable")

    def download_document(self, doc_id: str, output_path: Path) -> Path:
        """Download document by ID."""
        return self.download_document_v3(doc_id, output_path)

    def download_by_name(self, document_name: str, output_path: str) -> str:
        """Download document by name. Returns path string."""
        doc = self._resolve_name_to_doc(document_name)
        result = self.download_document(doc["ID"], Path(output_path))
        return str(result)

    def _resolve_folder(self, folder_path: str) -> str:
        """Resolve folder path to parent ID. Returns empty string for root."""
        if not folder_path or folder_path == "/":
            return ""
        docs = self.list_documents()
        segments = [s for s in folder_path.strip("/").split("/") if s]
        parent = ""
        for segment in segments:
            found = False
            for doc in docs:
                if (doc.get("VissibleName") == segment
                        and doc.get("Type") == "CollectionType"
                        and doc.get("Parent", "") == parent):
                    parent = doc["ID"]
                    found = True
                    break
            if not found:
                raise RuntimeError(
                    f"Folder '{segment}' not found in '{folder_path}'"
                )
        return parent

    def _hash_blob(self, data: bytes) -> str:
        """SHA-256 hex digest of data."""
        return hashlib.sha256(data).hexdigest()

    @staticmethod
    def _crc32c_header(data: bytes) -> str:
        """Compute CRC32C and return base64-encoded value for x-goog-hash."""
        import struct
        # CRC32C lookup table
        _CRC32C_TABLE = None

        def _make_table():
            table = []
            for i in range(256):
                crc = i
                for _ in range(8):
                    if crc & 1:
                        crc = (crc >> 1) ^ 0x82F63B78
                    else:
                        crc >>= 1
                table.append(crc)
            return table

        nonlocal_table = _make_table()
        crc = 0xFFFFFFFF
        for byte in data:
            crc = (crc >> 8) ^ nonlocal_table[(crc ^ byte) & 0xFF]
        crc ^= 0xFFFFFFFF
        crc_bytes = struct.pack(">I", crc)
        import base64
        return base64.b64encode(crc_bytes).decode("ascii")

    def _put_blob(self, blob_hash: str, data: bytes, filename: str) -> None:
        """PUT /sync/v3/files/<hash> with rm-filename and CRC32C headers."""
        crc_b64 = self._crc32c_header(data)
        resp = self._request_with_retry(
            "put",
            f"{SYNC_HOST}/sync/v3/files/{blob_hash}",
            headers={
                "rm-filename": filename,
                "x-goog-hash": f"crc32c={crc_b64}",
            },
            content=data,
            timeout_val=TRANSFER_TIMEOUT,
        )
        if resp.status_code not in (200, 201, 202):
            raise RuntimeError(
                f"Failed to upload blob {filename} (HTTP {resp.status_code}): {resp.text[:200]}"
            )

    def _build_index(self, entries: list[dict]) -> str:
        """Build a schema-v3 index. Kept for backward compatibility / tests."""
        sorted_entries = sorted(entries, key=lambda e: e["id"])
        lines = ["3\n"]
        for e in sorted_entries:
            lines.append(
                f"{e['hash']}:{e['type']}:{e['id']}:{e['subfiles']}:{e['size']}\n"
            )
        return "".join(lines)

    def _build_index_v4(self, entries: list[dict], index_id: str) -> str:
        """Build a schema-v4 index.

        ``index_id`` is "." for the root index, otherwise the document UUID.
        Includes the v4 info line: ``0:{index_id}:{count}:{total_size}``.
        """
        sorted_entries = sorted(entries, key=lambda e: e["id"])
        total_size = sum(int(e.get("size", 0)) for e in sorted_entries)
        lines = ["4\n", f"0:{index_id}:{len(sorted_entries)}:{total_size}\n"]
        for e in sorted_entries:
            lines.append(
                f"{e['hash']}:{e['type']}:{e['id']}:{e['subfiles']}:{e['size']}\n"
            )
        return "".join(lines)

    def _hash_index_v3(self, entries: list[dict]) -> str:
        """Schema-v3 index hash: SHA256 of concatenated child hash bytes."""
        sorted_entries = sorted(entries, key=lambda e: e["id"])
        raw = b""
        for e in sorted_entries:
            try:
                raw += bytes.fromhex(e["hash"])
            except ValueError:
                raw += hashlib.sha256(e["hash"].encode()).digest()
        return hashlib.sha256(raw).hexdigest()

    def _update_root(self, new_root_hash: str, generation: int) -> dict:
        """PUT /sync/v3/root with optimistic concurrency."""
        payload = {
            "broadcast": True,
            "hash": new_root_hash,
            "generation": generation,
        }
        resp = self._request_with_retry(
            "put",
            f"{SYNC_HOST}/sync/v3/root",
            headers={
                "Content-Type": "application/json",
            },
            content=json.dumps(payload),
        )
        if resp.status_code == 412:
            raise RuntimeError("Root conflict (generation mismatch)")
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Failed to update root (HTTP {resp.status_code}): {resp.text}"
            )
        return payload

    def _update_root_with_retry(self, modify_fn) -> None:
        """Read root, apply modify_fn to root index entries, upload new root.

        Always emits a schema-v4 root index — current servers reject new v3
        root uploads. Retries up to 5 times on HTTP 412 (generation conflict).
        """
        # Bypass root cache while updating to avoid generation mismatches
        self._invalidate_root_cache()
        for attempt in range(5):
            root = self._get_root(max_age=0)
            root_blob = self._get_blob(root["hash"], filename=ROOT_FILENAME)
            root_entries = self._parse_index(root_blob.decode("utf-8"))

            new_entries = modify_fn(root_entries)

            # v4 root: id is "." in the info line; hash is SHA256 of full file
            new_root_text = self._build_index_v4(new_entries, ".")
            new_root_data = new_root_text.encode("utf-8")
            new_root_hash = self._hash_blob(new_root_data)
            self._put_blob(new_root_hash, new_root_data, "root.docSchema")

            try:
                self._update_root(new_root_hash, root["generation"])
                self._invalidate_root_cache()
                return
            except RuntimeError as e:
                if "conflict" in str(e).lower() and attempt < 4:
                    self._invalidate_root_cache()
                    continue
                raise
        raise RuntimeError("Failed to update root after 5 retries")

    def _invalidate_root_cache(self) -> None:
        """Force the next ``_get_root`` to hit the network."""
        self._root_cache = None
        self._root_cache_time = 0.0

    def upload_document(
        self, file_path: Path, parent_folder: str = "",
    ) -> str:
        """Upload a document to reMarkable Cloud using v3/v4 hash-tree protocol.

        parent_folder can be a path like "/my-panbot-test" or a UUID.
        Returns the document UUID.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        # Resolve folder path to UUID if it looks like a path
        resolved_parent = parent_folder
        if parent_folder and "/" in parent_folder:
            # Try cache first (fast), fall back to full resolve
            cache_path = self._cache_dir / "docs.json"
            if cache_path.exists():
                cached = json.loads(cache_path.read_text())
                resolved_parent = self._resolve_folder_from_docs(cached, parent_folder)
            else:
                resolved_parent = self._resolve_folder(parent_folder)

        doc_id = str(uuid.uuid4())
        file_type = file_path.suffix.lstrip(".")
        if file_type not in ("pdf", "epub"):
            file_type = "pdf"
        now_ms = str(int(time.time() * 1000))

        content_json = json.dumps({
            "fileType": file_type,
            "pageCount": 0,
            "lastOpenedPage": 0,
            "lineHeight": -1,
            "margins": 180,
            "orientation": "portrait",
            "textScale": 1,
            "transform": {},
        }).encode("utf-8")

        metadata_json = json.dumps({
            "visibleName": file_path.stem,
            "type": "DocumentType",
            "parent": resolved_parent,
            "lastModified": now_ms,
            "lastOpened": "",
            "lastOpenedPage": 0,
            "version": 0,
            "pinned": False,
            "synced": False,
            "modified": False,
            "deleted": False,
            "metadatamodified": False,
        }).encode("utf-8")

        pagedata = b"Blank\n"
        file_data = file_path.read_bytes()

        # (index_id, upload_rm_filename, data)
        # index_id goes in the sub-index; upload_rm_filename is for the PUT header
        # API only accepts .content/.metadata/.pagedata as rm-filename extensions
        files = [
            (f"{doc_id}.content", f"{doc_id}.content", content_json),
            (f"{doc_id}.metadata", f"{doc_id}.metadata", metadata_json),
            (f"{doc_id}.pagedata", f"{doc_id}.pagedata", pagedata),
            (f"{doc_id}.{file_type}", f"{doc_id}.content", file_data),
        ]

        sub_entries = []
        total_size = 0
        for index_id, upload_name, data in files:
            blob_hash = self._hash_blob(data)
            self._put_blob(blob_hash, data, upload_name)
            sub_entries.append({
                "hash": blob_hash,
                "type": "0",
                "id": index_id,
                "subfiles": 0,
                "size": len(data),
            })
            total_size += len(data)

        # v4 sub-index: hash is SHA256 of full file (same as any other blob)
        sub_index_text = self._build_index_v4(sub_entries, doc_id)
        sub_index_data = sub_index_text.encode("utf-8")
        sub_index_hash = self._hash_blob(sub_index_data)
        self._put_blob(sub_index_hash, sub_index_data, f"{doc_id}.docSchema")

        num_files = len(files)

        def add_to_root(entries):
            entries.append({
                "hash": sub_index_hash,
                "type": "0",
                "id": doc_id,
                "subfiles": num_files,
                "size": total_size,
            })
            return entries

        self._update_root_with_retry(add_to_root)
        return doc_id

    def upload_simple(
        self,
        file_path: Path,
        visible_name: str | None = None,
    ) -> str:
        """Upload a PDF/EPUB via the simple upload API (``POST /doc/v2/files``).

        Unlike :meth:`upload_document`, the server builds the document hash-tree
        itself, so no client-side blob composition or root rewrite is needed.
        This is the fast path for ingesting a raw PDF or EPUB.

        Args:
            file_path: Path to a ``.pdf`` or ``.epub`` file.
            visible_name: Display name on the reMarkable. Defaults to the file
                stem.

        Returns:
            The new document UUID (``docID`` returned by the server).
        """
        import base64

        file_path = Path(file_path)
        if not file_path.exists():
            raise FileNotFoundError(f"File not found: {file_path}")

        suffix = file_path.suffix.lower()
        mime = {
            ".pdf": "application/pdf",
            ".epub": "application/epub+zip",
        }.get(suffix)
        if mime is None:
            raise ValueError(
                f"upload_simple only supports .pdf and .epub, got: {suffix}"
            )

        name = visible_name if visible_name is not None else file_path.stem
        meta_b64 = base64.b64encode(
            json.dumps({"file_name": name}).encode("utf-8")
        ).decode("ascii")

        resp = self._request_with_retry(
            "post",
            f"{UPLOAD_HOST}/doc/v2/files",
            content=file_path.read_bytes(),
            headers={
                "Content-Type": mime,
                "rm-meta": meta_b64,
                "rm-source": "RoR-Browser",
            },
            timeout_val=TRANSFER_TIMEOUT,
        )
        if resp.status_code not in (200, 201):
            raise RuntimeError(
                f"Simple upload failed (HTTP {resp.status_code}): {resp.text}"
            )
        return resp.json()["docID"]

    def delete_document(self, doc_id: str, version: int) -> bool:
        """Delete a document by removing it from the root index."""

        def remove_from_root(entries):
            new_entries = [e for e in entries if e["id"] != doc_id]
            if len(new_entries) == len(entries):
                raise RuntimeError(f"Document {doc_id} not found in root index")
            return new_entries

        self._update_root_with_retry(remove_from_root)
        return True

    def delete_by_name(self, document_name: str) -> bool:
        """Delete document by name."""
        doc = self._resolve_name_to_doc(document_name)
        return self.delete_document(doc["ID"], doc.get("Version", 1))

    # --- Folders, move, stat ---

    def mkdir(self, folder_path: str, parent_folder: str = "") -> str:
        """Create a folder (CollectionType document) in the cloud.

        ``folder_path`` is a leaf folder name unless ``parent_folder`` is set,
        in which case the leaf is created under that parent. To create nested
        folders, call ``mkdir`` for each segment.

        Returns the new folder UUID.
        """
        # Resolve parent if it looks like a path
        resolved_parent = parent_folder
        if parent_folder and "/" in parent_folder:
            cache_path = self._cache_dir / "docs.json"
            if cache_path.exists():
                cached = json.loads(cache_path.read_text())
                resolved_parent = self._resolve_folder_from_docs(cached, parent_folder)
            else:
                resolved_parent = self._resolve_folder(parent_folder)

        folder_id = str(uuid.uuid4())
        now_ms = str(int(time.time() * 1000))

        content_json = json.dumps({}).encode("utf-8")
        metadata_json = json.dumps({
            "visibleName": folder_path,
            "type": "CollectionType",
            "parent": resolved_parent,
            "lastModified": now_ms,
            "lastOpened": "",
            "lastOpenedPage": 0,
            "version": 1,
            "pinned": False,
            "synced": False,
            "modified": False,
            "deleted": False,
            "metadatamodified": False,
        }).encode("utf-8")

        files = [
            (f"{folder_id}.content", f"{folder_id}.content", content_json),
            (f"{folder_id}.metadata", f"{folder_id}.metadata", metadata_json),
        ]
        sub_entries = []
        total_size = 0
        for index_id, upload_name, data in files:
            blob_hash = self._hash_blob(data)
            self._put_blob(blob_hash, data, upload_name)
            sub_entries.append({
                "hash": blob_hash,
                "type": "0",
                "id": index_id,
                "subfiles": 0,
                "size": len(data),
            })
            total_size += len(data)

        sub_index_text = self._build_index_v4(sub_entries, folder_id)
        sub_index_data = sub_index_text.encode("utf-8")
        sub_index_hash = self._hash_blob(sub_index_data)
        self._put_blob(sub_index_hash, sub_index_data, f"{folder_id}.docSchema")

        def add_to_root(entries):
            entries.append({
                "hash": sub_index_hash,
                "type": "0",
                "id": folder_id,
                "subfiles": len(files),
                "size": total_size,
            })
            return entries

        self._update_root_with_retry(add_to_root)
        return folder_id

    def _get_doc_metadata_blob(self, doc_id: str) -> tuple[dict, dict]:
        """Return (metadata_dict, sub_index_metadata_entry) for a doc."""
        sub_index = self._get_doc_sub_index(doc_id)
        for entry in sub_index:
            if entry["id"].endswith(".metadata"):
                blob = self._get_blob(entry["hash"], filename=entry["id"])
                return json.loads(blob.decode("utf-8")), entry
        raise RuntimeError(f"Document {doc_id} has no .metadata blob")

    def _update_metadata(self, doc_id: str, metadata: dict) -> None:
        """Write a new .metadata JSON for the given doc and rebuild sub-index.

        Used by ``rename``/``move``: keeps every other sub-file unchanged.
        """
        new_meta_data = json.dumps(metadata).encode("utf-8")
        new_meta_hash = self._hash_blob(new_meta_data)
        self._put_blob(new_meta_hash, new_meta_data, f"{doc_id}.metadata")

        def update_in_root(root_entries):
            for i, entry in enumerate(root_entries):
                if entry["id"] != doc_id:
                    continue
                sub_blob = self._get_blob(
                    entry["hash"], filename=f"{entry['id']}.docSchema",
                )
                sub_entries = self._parse_index(sub_blob.decode("utf-8"))
                new_sub = []
                for se in sub_entries:
                    if se["id"].endswith(".metadata"):
                        new_sub.append({
                            "hash": new_meta_hash,
                            "type": se["type"],
                            "id": se["id"],
                            "subfiles": se["subfiles"],
                            "size": len(new_meta_data),
                        })
                    else:
                        new_sub.append(se)
                sub_text = self._build_index_v4(new_sub, doc_id)
                sub_data = sub_text.encode("utf-8")
                sub_hash = self._hash_blob(sub_data)
                self._put_blob(sub_hash, sub_data, f"{doc_id}.docSchema")
                root_entries[i] = {
                    "hash": sub_hash,
                    "type": "0",
                    "id": doc_id,
                    "subfiles": entry["subfiles"],
                    "size": entry["size"],
                }
                return root_entries
            raise RuntimeError(f"Document {doc_id} not found in root")

        self._update_root_with_retry(update_in_root)

    def rename(self, doc_id: str, new_name: str) -> None:
        """Rename a document or folder."""
        meta, _ = self._get_doc_metadata_blob(doc_id)
        meta["visibleName"] = new_name
        meta["lastModified"] = str(int(time.time() * 1000))
        meta["metadatamodified"] = True
        meta["version"] = int(meta.get("version", 0)) + 1
        self._update_metadata(doc_id, meta)

    def move(self, doc_id: str, new_parent_id: str) -> None:
        """Move a document/folder to another parent (folder UUID, "" for root,
        or "trash" to send to trash).
        """
        meta, _ = self._get_doc_metadata_blob(doc_id)
        meta["parent"] = new_parent_id
        meta["lastModified"] = str(int(time.time() * 1000))
        meta["metadatamodified"] = True
        meta["version"] = int(meta.get("version", 0)) + 1
        self._update_metadata(doc_id, meta)

    def stat(self, doc_id: str) -> dict:
        """Return the full metadata dict for a doc/folder by id."""
        meta, _ = self._get_doc_metadata_blob(doc_id)
        return meta

    def find(self, pattern: str, *, recursive: bool = True) -> list[dict]:
        """Return docs whose visibleName contains ``pattern`` (case-insensitive)."""
        needle = pattern.lower()
        return [
            d for d in self.list_documents()
            if needle in d.get("VissibleName", "").lower()
        ]

    def ls_folder(self, folder_path: str = "/") -> list[dict]:
        """List entries under ``folder_path``. Uses cache when available."""
        if not folder_path or folder_path == "/":
            parent = ""
        else:
            parent = self._resolve_folder(folder_path)
        return [
            d for d in self.list_documents()
            if d.get("Parent", "") == parent
        ]

    def path_to_doc(self, path: str) -> dict:
        """Resolve a /a/b/c.pdf path to a doc dict.

        The leaf segment must match a document or collection name; intermediate
        segments must be CollectionType folders.
        """
        if not path or path == "/":
            raise RuntimeError("path is empty")
        segments = [s for s in path.strip("/").split("/") if s]
        if not segments:
            raise RuntimeError("path is empty")
        leaf = segments[-1]
        parent_path = "/" + "/".join(segments[:-1])
        parent_id = (
            self._resolve_folder(parent_path)
            if segments[:-1]
            else ""
        )
        for d in self.list_documents():
            if (
                d.get("VissibleName") == leaf
                and d.get("Parent", "") == parent_id
            ):
                return d
        raise RuntimeError(f"path '{path}' not found")

    # --- Tags ---

    def _get_doc_content(self, doc_id: str, root: dict | None = None) -> dict:
        """Fetch the .content JSON for a document from Cloud."""
        sub_index = self._get_doc_sub_index(doc_id, root)
        for entry in sub_index:
            if entry["id"].endswith(".content"):
                blob = self._get_blob(entry["hash"], filename=entry["id"])
                return json.loads(blob.decode("utf-8"))
        return {}

    def get_doc_tags(self, doc_id: str) -> list[dict]:
        """Get document-level tags for a document.

        Returns list of {"name": str, "timestamp": int}.
        """
        content = self._get_doc_content(doc_id)
        return content.get("tags", [])

    def get_doc_page_tags(self, doc_id: str) -> list[dict]:
        """Get page-level tags for a document.

        Returns list of {"name": str, "pageId": str, "timestamp": int}.
        """
        content = self._get_doc_content(doc_id)
        return content.get("pageTags", [])

    def get_doc_all_tags(self, doc_id: str) -> dict:
        """Get both document and page tags in one call.

        Returns {"tags": [...], "pageTags": [...]}.
        """
        content = self._get_doc_content(doc_id)
        return {
            "tags": content.get("tags", []),
            "pageTags": content.get("pageTags", []),
        }

    def get_doc_pages(self, doc_id: str) -> list[str]:
        """Get list of page UUIDs for a document."""
        content = self._get_doc_content(doc_id)
        return content.get("pages", [])

    def _update_doc_content(self, doc_id: str, content: dict) -> None:
        """Write updated .content JSON back to Cloud for a document.

        Rebuilds the document sub-index with the new .content blob,
        then updates the root index.
        """
        content_data = json.dumps(content, indent=2).encode("utf-8")
        content_hash = self._hash_blob(content_data)
        content_filename = f"{doc_id}.content"
        self._put_blob(content_hash, content_data, content_filename)

        def update_in_root(root_entries):
            # Find the document entry and rebuild its sub-index
            for i, entry in enumerate(root_entries):
                if entry["id"] == doc_id:
                    sub_blob = self._get_blob(
                    entry["hash"], filename=f"{entry['id']}.docSchema",
                )
                    sub_entries = self._parse_index(sub_blob.decode("utf-8"))

                    # Replace .content entry
                    new_sub = []
                    for se in sub_entries:
                        if se["id"].endswith(".content"):
                            new_sub.append({
                                "hash": content_hash,
                                "type": se["type"],
                                "id": se["id"],
                                "subfiles": se["subfiles"],
                                "size": len(content_data),
                            })
                        else:
                            new_sub.append(se)

                    # Build and upload new v4 sub-index
                    sub_text = self._build_index_v4(new_sub, doc_id)
                    sub_data = sub_text.encode("utf-8")
                    sub_hash = self._hash_blob(sub_data)
                    self._put_blob(sub_hash, sub_data, f"{doc_id}.docSchema")

                    # Update root entry (force v4 type=0)
                    root_entries[i] = {
                        "hash": sub_hash,
                        "type": "0",
                        "id": entry["id"],
                        "subfiles": entry["subfiles"],
                        "size": entry["size"],
                    }
                    return root_entries

            raise RuntimeError(f"Document {doc_id} not found in root index")

        self._update_root_with_retry(update_in_root)

    def add_doc_tag(self, doc_id: str, tag_name: str) -> dict:
        """Add a document-level tag. Returns the tag dict.

        Skips if tag already exists.
        """
        content = self._get_doc_content(doc_id)
        tags = content.get("tags", [])

        for t in tags:
            if t["name"] == tag_name:
                return t

        new_tag = {"name": tag_name, "timestamp": int(time.time() * 1000)}
        tags.append(new_tag)
        content["tags"] = tags
        self._update_doc_content(doc_id, content)
        return new_tag

    def remove_doc_tag(self, doc_id: str, tag_name: str) -> bool:
        """Remove a document-level tag. Returns True if removed."""
        content = self._get_doc_content(doc_id)
        tags = content.get("tags", [])
        original = len(tags)
        tags = [t for t in tags if t["name"] != tag_name]
        if len(tags) == original:
            return False
        content["tags"] = tags
        self._update_doc_content(doc_id, content)
        return True

    def add_page_tag(self, doc_id: str, tag_name: str, page_id: str) -> dict:
        """Add a page-level tag. Returns the tag dict."""
        content = self._get_doc_content(doc_id)
        page_tags = content.get("pageTags", [])

        for t in page_tags:
            if t["name"] == tag_name and t["pageId"] == page_id:
                return t

        new_tag = {"name": tag_name, "pageId": page_id, "timestamp": int(time.time() * 1000)}
        page_tags.append(new_tag)
        content["pageTags"] = page_tags
        self._update_doc_content(doc_id, content)
        return new_tag

    def remove_page_tag(self, doc_id: str, tag_name: str, page_id: str | None = None) -> int:
        """Remove page-level tag(s). Returns count removed.

        If page_id is None, removes tag from all pages.
        """
        content = self._get_doc_content(doc_id)
        page_tags = content.get("pageTags", [])
        original = len(page_tags)

        if page_id:
            page_tags = [t for t in page_tags if not (t["name"] == tag_name and t["pageId"] == page_id)]
        else:
            page_tags = [t for t in page_tags if t["name"] != tag_name]

        removed = original - len(page_tags)
        if removed > 0:
            content["pageTags"] = page_tags
            self._update_doc_content(doc_id, content)
        return removed

    def find_by_tag(self, tag_name: str) -> list[dict]:
        """Find all documents that have a specific tag.

        Returns list of doc dicts (same format as list_documents) with
        an extra "tags" and "pageTags" field.

        Implementation: fetches every document's .content blob concurrently
        using two parallel waves (sub-index → content). On a 2700-doc account
        this completes in seconds rather than minutes.
        """
        docs = [
            d for d in self.list_documents()
            if d.get("Type") == "DocumentType"
        ]
        if not docs:
            return []

        # Wave 1: fetch sub-indexes for every doc in parallel
        root = self._get_root()
        root_blob = self._get_blob(root["hash"], filename=ROOT_FILENAME)
        root_index = {e["id"]: e for e in self._parse_index(root_blob.decode("utf-8"))}

        sub_indexes: dict[str, list[dict]] = {}
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCHES) as pool:
            fut = {}
            for d in docs:
                entry = root_index.get(d["ID"])
                if entry:
                    fut[pool.submit(
                        self._fetch_blob_safe, entry["hash"],
                        f"{entry['id']}.docSchema",
                    )] = d["ID"]
            for f in as_completed(fut):
                blob = f.result()
                if blob:
                    try:
                        sub_indexes[fut[f]] = self._parse_index(blob.decode("utf-8"))
                    except Exception:
                        pass

        # Wave 2: collect .content blob hashes, fetch in parallel.
        # Leaf blobs require the leaf id as rm-filename (the <hash>.docSchema
        # fallback is rejected by the server for leaves).
        content_hash_by_doc: dict[str, tuple[str, str]] = {}
        for doc_id, sub in sub_indexes.items():
            for s in sub:
                if s["id"].endswith(".content") and not s["id"].endswith(
                    (".pdf.content", ".epub.content")
                ):
                    content_hash_by_doc[doc_id] = (s["hash"], s["id"])
                    break

        contents: dict[str, dict] = {}
        with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_FETCHES) as pool:
            fut = {
                pool.submit(self._fetch_blob_safe, h, leaf_id): doc_id
                for doc_id, (h, leaf_id) in content_hash_by_doc.items()
            }
            for f in as_completed(fut):
                blob = f.result()
                if blob:
                    try:
                        contents[fut[f]] = json.loads(blob.decode("utf-8"))
                    except Exception:
                        pass

        # Filter docs with the tag
        results = []
        docs_by_id = {d["ID"]: d for d in docs}
        for doc_id, content in contents.items():
            doc_tag_names = [t.get("name") for t in content.get("tags", [])]
            page_tag_names = [t.get("name") for t in content.get("pageTags", [])]
            if tag_name in doc_tag_names or tag_name in page_tag_names:
                results.append({
                    **docs_by_id[doc_id],
                    "tags": content.get("tags", []),
                    "pageTags": content.get("pageTags", []),
                })
        return results

    def get_tags_by_name(self, document_name: str) -> dict:
        """Get all tags for a document by name.

        Returns {"tags": [...], "pageTags": [...]}.
        """
        doc = self._resolve_name_to_doc(document_name)
        return self.get_doc_all_tags(doc["ID"])

    # --- Change tracking ---

    def _tracking_file(self, doc_id: str) -> Path:
        return self._tracking_dir / f"{doc_id}.yaml"

    def _load_tracking(self, doc_id: str) -> TrackingState | None:
        path = self._tracking_file(doc_id)
        if not path.exists():
            return None
        data = yaml.safe_load(path.read_text())
        return TrackingState(**data)

    def _save_tracking(self, state: TrackingState) -> None:
        self._tracking_dir.mkdir(parents=True, exist_ok=True)
        data = {
            "document_id": state.document_id,
            "document_name": state.document_name,
            "version": state.version,
            "modified_client": state.modified_client,
            "last_checked": state.last_checked,
            "last_downloaded": state.last_downloaded,
        }
        self._tracking_file(state.document_id).write_text(yaml.dump(data))

    def watch_document(self, document_name: str) -> TrackingState:
        """Start tracking a document for changes."""
        doc = self._resolve_name_to_doc(document_name)
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        state = TrackingState(
            document_id=doc["ID"],
            document_name=doc["VissibleName"],
            version=doc.get("Version", 1),
            modified_client=doc.get("ModifiedClient", now),
            last_checked=now,
            last_downloaded=now,
        )
        self._save_tracking(state)
        return state

    def _get_doc_by_id(self, doc_id: str) -> dict:
        """Fetch a single document's metadata by ID via v3/v4 sync."""
        docs = self.list_documents()
        for doc in docs:
            if doc.get("ID") == doc_id:
                return doc
        raise RuntimeError(f"Document {doc_id} not found")

    def _find_tracking_by_name(self, document_name: str) -> TrackingState | None:
        """Find tracking state by document name from local tracking files."""
        if not self._tracking_dir.exists():
            return None
        for path in self._tracking_dir.glob("*.yaml"):
            data = yaml.safe_load(path.read_text())
            if data and data.get("document_name") == document_name:
                return TrackingState(**data)
        return None

    def check_changes(self, document_name: str) -> ChangeResult:
        """Check if a tracked document has changed."""
        # Try efficient path: load tracking state locally, then single-doc query
        state = self._find_tracking_by_name(document_name)
        if state:
            doc = self._get_doc_by_id(state.document_id)
        else:
            # No tracking state — fall back to name resolution
            doc = self._resolve_name_to_doc(document_name)
            state = self._load_tracking(doc["ID"])
        if not state:
            raise RuntimeError(
                f"Document '{document_name}' is not being tracked. "
                "Run: panbot rm watch first"
            )

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        new_version = doc.get("Version", 1)
        new_modified = doc.get("ModifiedClient", "")

        # Capture old values BEFORE mutation
        old_version = state.version
        old_modified = state.modified_client
        changed = (new_version != old_version or new_modified != old_modified)

        # Update tracking state
        state.last_checked = now
        if changed:
            state.version = new_version
            state.modified_client = new_modified
        self._save_tracking(state)

        return ChangeResult(
            changed=changed,
            document_name=document_name,
            old_version=old_version,
            new_version=new_version if changed else None,
            old_modified=old_modified,
            new_modified=new_modified if changed else None,
        )

    def poll_changes(
        self,
        document_name: str,
        interval_minutes: float = 5,
        timeout_minutes: float = 60,
        stabilization_factor: float = 2.0,
    ) -> ChangeResult:
        """Poll until changes stabilize or timeout."""
        start = time.time()
        timeout_secs = timeout_minutes * 60
        interval_secs = interval_minutes * 60
        stabilization_secs = interval_minutes * stabilization_factor * 60
        last_change_time: float | None = None
        first_change_result: ChangeResult | None = None

        # Load tracked version for timeout return
        tracked_state = self._find_tracking_by_name(document_name)
        tracked_version = tracked_state.version if tracked_state else 0

        while True:
            elapsed = time.time() - start
            if elapsed >= timeout_secs:
                return ChangeResult(
                    changed=False,
                    document_name=document_name,
                    old_version=tracked_version,
                )

            result = self.check_changes(document_name)
            if result.changed:
                last_change_time = time.time()
                if first_change_result is None:
                    first_change_result = result
                else:
                    # Update new_version to latest
                    first_change_result.new_version = result.new_version
                    first_change_result.new_modified = result.new_modified

            # Check stabilization: no changes for stabilization_secs
            if last_change_time is not None and first_change_result is not None:
                since_last = time.time() - last_change_time
                if since_last >= stabilization_secs:
                    first_change_result.minutes_since_last_change = since_last / 60
                    return first_change_result

            time.sleep(interval_secs)

    def unwatch_document(self, document_name: str) -> None:
        """Stop tracking a document."""
        doc = self._resolve_name_to_doc(document_name)
        path = self._tracking_file(doc["ID"])
        if path.exists():
            path.unlink()

    # --- Cloud-wide change polling (no full scan) ---

    def diff_root(
        self,
        previous_entries: list[dict] | None = None,
    ) -> tuple[dict, list[dict], dict[str, dict]]:
        """Fetch the current root and diff against ``previous_entries``.

        Returns ``(root, current_entries, changes)`` where ``changes`` is::

            {
                "added":    {id: entry, ...},
                "removed":  {id: entry, ...},
                "modified": {id: entry, ...},  # same id, hash changed
            }

        Pass ``previous_entries=None`` on first call — everything is "added".
        Cheap: 2 HTTP calls (root + root index blob).
        """
        self._invalidate_root_cache()
        root = self._get_root(max_age=0)
        root_blob = self._get_blob(root["hash"], filename=ROOT_FILENAME)
        current = self._parse_index(root_blob.decode("utf-8"))
        prev = {e["id"]: e for e in (previous_entries or [])}
        cur = {e["id"]: e for e in current}
        added = {i: e for i, e in cur.items() if i not in prev}
        removed = {i: e for i, e in prev.items() if i not in cur}
        modified = {
            i: e for i, e in cur.items()
            if i in prev and prev[i].get("hash") != e.get("hash")
        }
        return root, current, {
            "added": added,
            "removed": removed,
            "modified": modified,
        }

    def poll_root_changes(
        self,
        callback,
        interval: float = 5.0,
        timeout: float | None = None,
        previous_entries: list[dict] | None = None,
    ) -> list[dict]:
        """Poll the cloud root for changes and invoke ``callback(changes)``.

        ``callback`` receives a dict ``{added, removed, modified}`` (same shape
        as :meth:`diff_root`) every time the root generation advances. Empty
        intervals are skipped — the callback only fires on real change.

        Cost per poll: 1 GET ``/sync/v4/root`` (~30 bytes) when nothing changed,
        plus 1 GET on the root index blob when generation moved. No metadata
        fetched here — to enrich, the caller can call ``list_documents()``
        which now uses incremental refresh.

        Args:
            callback: callable(changes_dict) -> None
            interval: seconds between polls (default 5).
            timeout: stop after N seconds (None = run forever).
            previous_entries: baseline; defaults to current root on entry.

        Returns:
            The latest seen root entries list (resume baseline for future calls).
        """
        start = time.time()
        # Prime baseline: if not provided, snapshot current state silently.
        if previous_entries is None:
            self._invalidate_root_cache()
            root = self._get_root(max_age=0)
            blob = self._get_blob(root["hash"], filename=ROOT_FILENAME)
            previous_entries = self._parse_index(blob.decode("utf-8"))
            last_generation = root.get("generation")
        else:
            last_generation = None

        while True:
            if timeout is not None and (time.time() - start) >= timeout:
                return previous_entries

            try:
                self._invalidate_root_cache()
                root = self._get_root(max_age=0)
            except Exception:
                time.sleep(interval)
                continue

            gen = root.get("generation")
            if last_generation is not None and gen == last_generation:
                # Nothing happened — the cheap path
                time.sleep(interval)
                continue

            try:
                blob = self._get_blob(root["hash"], filename=ROOT_FILENAME)
                current = self._parse_index(blob.decode("utf-8"))
            except Exception:
                time.sleep(interval)
                continue

            prev = {e["id"]: e for e in previous_entries}
            cur = {e["id"]: e for e in current}
            changes = {
                "added": {i: e for i, e in cur.items() if i not in prev},
                "removed": {i: e for i, e in prev.items() if i not in cur},
                "modified": {
                    i: e for i, e in cur.items()
                    if i in prev and prev[i].get("hash") != e.get("hash")
                },
            }
            if any(changes.values()):
                try:
                    callback(changes)
                except Exception as cb_err:  # pragma: no cover
                    print(f"poll_root_changes callback error: {cb_err}")

            previous_entries = current
            last_generation = gen
            time.sleep(interval)


def check_cloud_available() -> bool:
    """Check if cloud config exists with a valid device token."""
    if not CONFIG_PATH.exists():
        return False
    try:
        config = yaml.safe_load(CONFIG_PATH.read_text())
        return bool(config and config.get("device_token"))
    except Exception:
        return False


def list_remarkable_documents() -> list[dict[str, str]]:
    """List documents — wrapper matching rm_utils signature."""
    cloud = RemarkableCloud()
    docs = cloud.list_documents()
    return [
        {"id": d["ID"], "name": d["VissibleName"]}
        for d in docs
        if d.get("Type") == "DocumentType"
    ]


def download_remarkable_document(document_name: str, output_path: str) -> str:
    """Download document — wrapper matching rm_utils signature."""
    cloud = RemarkableCloud()
    return cloud.download_by_name(document_name, output_path)


def upload_to_remarkable(file_path: str, destination: str | None = None) -> str:
    """Upload document — wrapper matching rm_utils signature."""
    p = Path(file_path)
    if not p.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    cloud = RemarkableCloud()
    cloud.upload_document(p, parent_folder=destination or "")
    return p.stem


def delete_remarkable_document(document_name: str) -> bool:
    """Delete document — wrapper for CLI."""
    cloud = RemarkableCloud()
    return cloud.delete_by_name(document_name)
