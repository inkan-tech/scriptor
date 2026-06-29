"""Drop-in CLI replacement for ``rmapi`` (juruen / ddvk).

Mirrors the rmapi command surface — ``ls``, ``get``, ``put``, ``mkdir``,
``rm``, ``mv``, ``find``, ``stat``, ``pwd``, ``cd``, ``version``, ``reset``,
``account``, ``help``, ``geta``, ``mget``, ``mput``, ``refresh``, ``nuke`` —
backed by the pure-Python :class:`~inkan_doc.rm_cloud.RemarkableCloud` client
that emits schema-v4 indexes.

Usage::

    rmapi <command> [args]
    rmapi                    # interactive shell
    rmapi help <command>     # detailed help

Stateful (cwd, kept across invocations in ``~/.config/panbot/rmapi.cwd``):
    pwd, cd

Per the rmapi convention, paths are forward-slash separated; ``..``
navigates up one segment, ``.`` is a no-op, leading ``/`` is absolute.
"""
from __future__ import annotations

import json
import sys
import os
import re
import shlex
from pathlib import Path
from typing import Optional

import click

from . import __version__
from .rm_cloud import (
    CONFIG_PATH,
    RemarkableCloud,
    check_cloud_available,
)

CWD_FILE = Path.home() / ".config" / "panbot" / "rmapi.cwd"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_cwd() -> str:
    if CWD_FILE.exists():
        return CWD_FILE.read_text().strip() or "/"
    return "/"


def _save_cwd(path: str) -> None:
    CWD_FILE.parent.mkdir(parents=True, exist_ok=True)
    CWD_FILE.write_text(path)


def _resolve_path(target: str, cwd: str | None = None) -> str:
    """Resolve a user-supplied path against ``cwd``, normalizing ``.``/``..``."""
    if cwd is None:
        cwd = _load_cwd()
    if not target:
        return cwd or "/"
    if target.startswith("/"):
        base_segs: list[str] = []
        rest = target.strip("/").split("/") if target.strip("/") else []
    else:
        base_segs = [s for s in cwd.strip("/").split("/") if s]
        rest = target.split("/")
    out = list(base_segs)
    for seg in rest:
        if seg in ("", "."):
            continue
        if seg == "..":
            if out:
                out.pop()
            continue
        out.append(seg)
    return "/" + "/".join(out)


def _get_cloud() -> RemarkableCloud:
    if not check_cloud_available():
        click.echo(
            "error: not registered. Run 'rmapi register' or visit "
            "https://my.remarkable.com/device/desktop/connect for an 8-letter "
            "code, then run: rmapi register <code>",
            err=True,
        )
        sys.exit(2)
    return RemarkableCloud()


def _format_entry(d: dict, long: bool = False) -> str:
    is_folder = d.get("Type") == "CollectionType"
    badge = "[d]" if is_folder else "[f]"
    name = d.get("VissibleName", "")
    if not long:
        return f"{badge}\t{name}"
    modified = d.get("ModifiedClient", "") or "-"
    return f"{badge}\t{modified}\t{name}\t({d.get('ID','')})"


# ---------------------------------------------------------------------------
# Click group
# ---------------------------------------------------------------------------

@click.group(
    invoke_without_command=True,
    context_settings={"help_option_names": ["-h", "--help"]},
)
@click.option(
    "--ni", is_flag=True, default=False,
    help="not interactive (skip code prompt). rmapi-compat flag.",
)
@click.option(
    "--json", "json_output", is_flag=True, default=False,
    help="output in JSON format (where applicable).",
)
@click.version_option(version=__version__, prog_name="rmapi (panbot)")
@click.pass_context
def main(ctx, ni: bool, json_output: bool):
    """Drop-in rmapi replacement, pure Python, schema-v4 ready."""
    ctx.ensure_object(dict)
    ctx.obj["ni"] = ni
    ctx.obj["json"] = json_output
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


# ---------------------------------------------------------------------------
# Offline commands
# ---------------------------------------------------------------------------

@main.command()
def version():
    """Print the version."""
    click.echo(__version__)


@main.command()
def reset():
    """Remove the device-token config file."""
    if CONFIG_PATH.exists():
        CONFIG_PATH.unlink()
        click.echo(f"removed {CONFIG_PATH}")
    else:
        click.echo("no config to remove")
    if CWD_FILE.exists():
        CWD_FILE.unlink()


@main.command()
@click.argument("code", required=False)
def register(code: Optional[str]):
    """Pair this client with a reMarkable account.

    Get the 8-letter code from https://my.remarkable.com/device/desktop/connect.
    """
    if not code:
        click.echo(
            "Visit https://my.remarkable.com/device/desktop/connect, log in, "
            "and copy the 8-letter code.\n", err=True,
        )
        code = click.prompt("Code", type=str)
    cloud = RemarkableCloud()
    cloud.register_device(code)
    click.echo("registered.")


# ---------------------------------------------------------------------------
# Stateful path commands
# ---------------------------------------------------------------------------

@main.command()
def pwd():
    """Print the current working directory."""
    click.echo(_load_cwd())


@main.command()
@click.argument("path", required=False, default="/")
def cd(path: str):
    """Change directory. Path is resolved against the current cwd."""
    new = _resolve_path(path)
    if new != "/":
        # Verify it exists and is a folder
        cloud = _get_cloud()
        try:
            cloud._resolve_folder(new)
        except RuntimeError as e:
            click.echo(f"cd: {e}", err=True)
            sys.exit(1)
    _save_cwd(new)
    click.echo(new)


# ---------------------------------------------------------------------------
# Listing / inspection
# ---------------------------------------------------------------------------

@main.command()
@click.argument("path", required=False, default="")
@click.option("-l", "--long", is_flag=True, help="long format")
@click.option("-d", "--group-directories", "group_dirs", is_flag=True)
@click.pass_context
def ls(ctx, path: str, long: bool, group_dirs: bool):
    """List directory."""
    target = _resolve_path(path) if path else _load_cwd()
    cloud = _get_cloud()
    entries = cloud.ls_folder(target)
    if group_dirs:
        entries.sort(key=lambda d: (d.get("Type") != "CollectionType", d.get("VissibleName", "")))
    else:
        entries.sort(key=lambda d: d.get("VissibleName", ""))
    if ctx.obj.get("json"):
        click.echo(json.dumps(entries, indent=2))
        return
    for d in entries:
        click.echo(_format_entry(d, long=long))


@main.command()
@click.argument("path")
@click.pass_context
def stat(ctx, path: str):
    """Show metadata for a file or folder."""
    cloud = _get_cloud()
    target = _resolve_path(path)
    doc = cloud.path_to_doc(target)
    meta = cloud.stat(doc["ID"])
    if ctx.obj.get("json"):
        click.echo(json.dumps({**doc, **meta}, indent=2))
        return
    click.echo(f"id:       {doc['ID']}")
    click.echo(f"name:     {meta.get('visibleName')}")
    click.echo(f"type:     {meta.get('type')}")
    click.echo(f"parent:   {meta.get('parent') or '(root)'}")
    click.echo(f"version:  {meta.get('version')}")
    click.echo(f"modified: {meta.get('lastModified')}")


@main.command()
@click.argument("pattern")
@click.argument("path", required=False, default="")
@click.pass_context
def find(ctx, pattern: str, path: str):
    """Find files whose visibleName contains the pattern (case-insensitive)."""
    cloud = _get_cloud()
    matches = cloud.find(pattern)
    if path:
        # Filter to entries inside the given subtree
        prefix = _resolve_path(path).rstrip("/") + "/"
        # Heuristic: build path index
        all_docs = cloud.list_documents()
        by_id = {d["ID"]: d for d in all_docs}

        def doc_path(d):
            segs = [d.get("VissibleName", "")]
            cur = by_id.get(d.get("Parent", ""))
            while cur:
                segs.insert(0, cur.get("VissibleName", ""))
                cur = by_id.get(cur.get("Parent", ""))
            return "/" + "/".join(segs)

        matches = [d for d in matches if doc_path(d).startswith(prefix)]
    if ctx.obj.get("json"):
        click.echo(json.dumps(matches, indent=2))
        return
    for d in matches:
        click.echo(_format_entry(d, long=True))


# ---------------------------------------------------------------------------
# Transfer
# ---------------------------------------------------------------------------

@main.command()
@click.argument("path")
@click.argument("output", required=False)
def get(path: str, output: Optional[str]):
    """Download a remote file as a .rmdoc archive (zip)."""
    cloud = _get_cloud()
    target = _resolve_path(path)
    doc = cloud.path_to_doc(target)
    out = Path(output) if output else Path.cwd() / f"{doc['VissibleName']}.rmdoc"
    cloud.download_document(doc["ID"], out)
    click.echo(f"downloaded -> {out}")


@main.command()
@click.argument("path")
@click.argument("output", required=False)
@click.pass_context
def geta(ctx, path: str, output: Optional[str]):
    """Download and produce an annotated PDF (delegates to inkan-doc rm review).

    For now this is an alias for ``get`` — annotated rendering depends on
    rmscene + cairo. Use ``inkan-doc rm review <name>`` for the full pipeline.
    """
    ctx.invoke(get, path=path, output=output)


@main.command()
@click.argument("source", type=click.Path(exists=True, dir_okay=False))
@click.argument("parent", required=False, default="")
def put(source: str, parent: str):
    """Upload a local PDF/EPUB to the cloud."""
    cloud = _get_cloud()
    target_parent = _resolve_path(parent) if parent else _load_cwd()
    if target_parent == "/":
        target_parent = ""
    doc_id = cloud.upload_document(Path(source), parent_folder=target_parent)
    click.echo(f"uploaded {source} -> {doc_id}")


@main.command()
@click.argument("local_dir", type=click.Path(exists=True, file_okay=False))
@click.argument("parent", required=False, default="")
def mput(local_dir: str, parent: str):
    """Recursively upload local files to a remote folder."""
    cloud = _get_cloud()
    target_parent = _resolve_path(parent) if parent else _load_cwd()
    if target_parent == "/":
        target_parent = ""
    count = 0
    for f in Path(local_dir).rglob("*"):
        if f.is_file() and f.suffix.lower() in (".pdf", ".epub"):
            cloud.upload_document(f, parent_folder=target_parent)
            click.echo(f"  + {f}")
            count += 1
    click.echo(f"uploaded {count} file(s)")


@main.command()
@click.argument("path")
@click.argument("local_dir", type=click.Path(file_okay=False))
def mget(path: str, local_dir: str):
    """Recursively download a remote folder."""
    cloud = _get_cloud()
    target = _resolve_path(path)
    parent_id = "" if target in ("/", "") else cloud._resolve_folder(target)
    Path(local_dir).mkdir(parents=True, exist_ok=True)
    count = 0
    for d in cloud.list_documents():
        if d.get("Parent") != parent_id:
            continue
        if d.get("Type") == "CollectionType":
            continue
        out = Path(local_dir) / f"{d['VissibleName']}.rmdoc"
        cloud.download_document(d["ID"], out)
        click.echo(f"  -> {out}")
        count += 1
    click.echo(f"downloaded {count} file(s)")


# ---------------------------------------------------------------------------
# Mutations
# ---------------------------------------------------------------------------

@main.command()
@click.argument("path")
def mkdir(path: str):
    """Create a folder. Nested paths are created if all intermediate
    folders already exist (use mkdir -p style: not yet supported)."""
    cloud = _get_cloud()
    target = _resolve_path(path)
    parent_path = "/".join(target.rstrip("/").split("/")[:-1]) or "/"
    leaf = target.rstrip("/").split("/")[-1]
    folder_id = cloud.mkdir(leaf, parent_folder=parent_path)
    click.echo(f"created folder {leaf} ({folder_id})")


@main.command()
@click.argument("path")
def rm(path: str):
    """Delete a document or folder."""
    cloud = _get_cloud()
    target = _resolve_path(path)
    doc = cloud.path_to_doc(target)
    cloud.delete_document(doc["ID"], doc.get("Version", 1))
    click.echo(f"deleted {target}")


@main.command()
@click.argument("src")
@click.argument("dst")
def mv(src: str, dst: str):
    """Move or rename a document/folder.

    If ``dst`` is an existing folder, ``src`` is moved into it (keeping its
    name). Otherwise ``dst`` is treated as the new path: rename + reparent.
    """
    cloud = _get_cloud()
    src_path = _resolve_path(src)
    dst_path = _resolve_path(dst)
    doc = cloud.path_to_doc(src_path)

    # Is dst an existing folder?
    try:
        dst_folder_id = cloud._resolve_folder(dst_path)
        cloud.move(doc["ID"], dst_folder_id)
        click.echo(f"moved {src_path} -> {dst_path}/")
        return
    except RuntimeError:
        pass

    # Otherwise: rename + maybe reparent
    new_parent_path = "/".join(dst_path.rstrip("/").split("/")[:-1]) or "/"
    new_name = dst_path.rstrip("/").split("/")[-1]
    new_parent_id = (
        ""
        if new_parent_path == "/"
        else cloud._resolve_folder(new_parent_path)
    )
    if doc.get("Parent", "") != new_parent_id:
        cloud.move(doc["ID"], new_parent_id)
    if doc.get("VissibleName") != new_name:
        cloud.rename(doc["ID"], new_name)
    click.echo(f"renamed/moved {src_path} -> {dst_path}")


@main.command()
@click.confirmation_option(prompt="REALLY delete every doc? (cannot undo)")
def nuke():
    """Delete every document and folder. Cannot be undone."""
    cloud = _get_cloud()
    for d in cloud.list_documents():
        try:
            cloud.delete_document(d["ID"], d.get("Version", 1))
            click.echo(f"  - {d.get('VissibleName')}")
        except Exception as e:
            click.echo(f"  ! failed: {d.get('VissibleName')}: {e}", err=True)


@main.command()
def refresh():
    """Force-refresh the local doc cache (full rescan)."""
    cloud = _get_cloud()
    cloud._invalidate_root_cache()
    docs = cloud.list_documents_v3(full_rescan=True)
    click.echo(f"refreshed: {len(docs)} doc(s)")


@main.command()
@click.option("--full", is_flag=True, help="also count documents and folders")
@click.pass_context
def account(ctx, full: bool):
    """Show account info. Use --full to count documents (slow on big accounts)."""
    cloud = _get_cloud()
    cloud._invalidate_root_cache()
    root = cloud._get_root()
    root_blob = cloud._get_blob(root["hash"]).decode("utf-8")
    entries = cloud._parse_index(root_blob)
    info = {
        "device_id": cloud._device_id,
        "root_hash": root["hash"],
        "generation": root["generation"],
        "schemaVersion": root.get("schemaVersion"),
        "root_entries": len(entries),
    }
    if full:
        docs = cloud.list_documents()
        info["documents"] = sum(1 for d in docs if d.get("Type") == "DocumentType")
        info["folders"] = sum(1 for d in docs if d.get("Type") == "CollectionType")
    if ctx.obj.get("json"):
        click.echo(json.dumps(info, indent=2))
        return
    for k, v in info.items():
        click.echo(f"{k:>14}: {v}")


# ---------------------------------------------------------------------------
# Polling / watch — not in stock rmapi but useful here
# ---------------------------------------------------------------------------

@main.command()
@click.option("--interval", default=5.0, type=float, help="poll interval (s)")
@click.option("--timeout", default=None, type=float, help="stop after N seconds")
def watch(interval: float, timeout: Optional[float]):
    """Stream cloud changes in real time (added/modified/removed entries)."""
    cloud = _get_cloud()

    def on_change(changes):
        for kind in ("added", "modified", "removed"):
            for doc_id in changes.get(kind, {}):
                click.echo(f"{kind:>8}  {doc_id}")

    click.echo(f"watching… interval={interval}s, ctrl-c to stop")
    try:
        cloud.poll_root_changes(on_change, interval=interval, timeout=timeout)
    except KeyboardInterrupt:
        click.echo("\nstopped.")


# ---------------------------------------------------------------------------
# Tags — not in rmapi but kept for parity with rm_cloud features
# ---------------------------------------------------------------------------

@main.group()
def tag():
    """Document and page tag management."""


@tag.command("add")
@click.argument("path")
@click.argument("tag_name")
def tag_add(path: str, tag_name: str):
    cloud = _get_cloud()
    doc = cloud.path_to_doc(_resolve_path(path))
    cloud.add_doc_tag(doc["ID"], tag_name)
    click.echo(f"tagged {path} with '{tag_name}'")


@tag.command("rm")
@click.argument("path")
@click.argument("tag_name")
def tag_rm(path: str, tag_name: str):
    cloud = _get_cloud()
    doc = cloud.path_to_doc(_resolve_path(path))
    if cloud.remove_doc_tag(doc["ID"], tag_name):
        click.echo(f"removed tag '{tag_name}' from {path}")
    else:
        click.echo(f"tag '{tag_name}' not found on {path}", err=True)


@tag.command("ls")
@click.argument("path")
@click.pass_context
def tag_ls(ctx, path: str):
    cloud = _get_cloud()
    doc = cloud.path_to_doc(_resolve_path(path))
    tags = cloud.get_doc_all_tags(doc["ID"])
    if ctx.obj.get("json"):
        click.echo(json.dumps(tags, indent=2))
        return
    for t in tags["tags"]:
        click.echo(f"[doc]  {t['name']}")
    for t in tags["pageTags"]:
        click.echo(f"[page] {t['name']}  pageId={t.get('pageId','')}")


@tag.command("find")
@click.argument("tag_name")
@click.pass_context
def tag_find(ctx, tag_name: str):
    cloud = _get_cloud()
    docs = cloud.find_by_tag(tag_name)
    if ctx.obj.get("json"):
        click.echo(json.dumps(docs, indent=2))
        return
    for d in docs:
        click.echo(_format_entry(d, long=True))


# ---------------------------------------------------------------------------
# Interactive shell (very minimal — no readline editing)
# ---------------------------------------------------------------------------

@main.command()
def shell():
    """Open an interactive shell (rmapi> prompt)."""
    click.echo("rmapi-compat interactive shell. type 'help' or 'exit'.")
    while True:
        try:
            cwd = _load_cwd()
            line = input(f"rmapi[{cwd}]> ").strip()
        except (EOFError, KeyboardInterrupt):
            click.echo()
            return
        if not line:
            continue
        if line in ("exit", "quit"):
            return
        try:
            argv = shlex.split(line)
        except ValueError as e:
            click.echo(f"parse error: {e}", err=True)
            continue
        try:
            main.main(args=argv, prog_name="rmapi", standalone_mode=False)
        except SystemExit:
            pass
        except click.ClickException as e:
            e.show()
        except Exception as e:
            click.echo(f"error: {e}", err=True)


if __name__ == "__main__":
    main()
