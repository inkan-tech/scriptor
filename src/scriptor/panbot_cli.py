"""Panbot CLI — AI ghostwriter for reMarkable."""

import os
import signal
from pathlib import Path

import click

from .panbot_config import PanbotConfig, PanbotMultiConfig, load_config


def _find_config(project_dir: str | None = None) -> tuple[PanbotConfig | None, Path]:
    """Find panbot.yaml in project_dir or cwd (single-project mode)."""
    base = Path(project_dir) if project_dir else Path.cwd()
    config_path = base / "panbot.yaml"
    if config_path.exists():
        cfg = load_config(config_path)
        if isinstance(cfg, PanbotConfig):
            return cfg, config_path
        # Multi-project file — not usable for single-project commands
        return None, config_path
    return None, config_path


def _find_any_config(project_dir: str | None = None):
    """Find panbot.yaml; returns PanbotConfig or PanbotMultiConfig."""
    base = Path(project_dir) if project_dir else Path.cwd()
    config_path = base / "panbot.yaml"
    if config_path.exists():
        return load_config(config_path), config_path
    return None, config_path


@click.group()
def panbot():
    """Panbot — AI ghostwriter for reMarkable tablets."""
    pass


@panbot.command("register-remarkable")
@click.argument("code")
def register_remarkable(code: str):
    """Register this machine with your reMarkable account.

    Get a one-time CODE from https://my.remarkable.com/device/desktop/connect
    """
    from .rm_cloud import RemarkableCloud
    cloud = RemarkableCloud()
    try:
        cloud.register_device(code)
        click.echo("Registered with reMarkable Cloud.")
        # Verify by fetching a session token
        cloud._get_user_token()
        click.echo("Session token OK — ready to use.")
    except Exception as e:
        click.echo(f"Registration failed: {e}", err=True)
        raise SystemExit(1)


@panbot.command()
@click.option("--project-dir", type=click.Path(exists=True), default=None)
@click.option("--monorepo", type=click.Path(exists=True), default=None,
              help="Discover all inkan-doc projects under this directory.")
@click.option("--folder-prefix", default="/Panbot",
              help="reMarkable folder prefix for auto-discovered projects.")
def init(project_dir: str | None, monorepo: str | None, folder_prefix: str):
    """Initialize panbot for an inkan-doc project or monorepo."""

    if monorepo:
        _init_monorepo(monorepo, folder_prefix, project_dir)
        return

    base = Path(project_dir) if project_dir else Path.cwd()
    inkan_config = base / "inkan-doc.yaml"
    if not inkan_config.exists():
        click.echo("No inkan-doc.yaml found. Run from an inkan-doc project directory.")
        raise SystemExit(1)

    import yaml
    with open(inkan_config) as f:
        project = yaml.safe_load(f)

    project_name = project.get("project_name", base.name)
    config = PanbotConfig(
        project_dir=str(base),
        folder=f"/Panbot/{project_name}",
    )
    config_path = base / "panbot.yaml"
    config.save_to_file(config_path)
    click.echo(f"Created {config_path}")
    click.echo(f"  Watching folder: {config.folder}")
    click.echo(f"  Project: {project_name}")

    # Warm cache — first run requires a full library scan
    if os.environ.get("PANBOT_SKIP_CLOUD") != "1":
        try:
            from .rm_cloud import RemarkableCloud, check_cloud_available
            if check_cloud_available():
                click.echo("  Building document cache (first time, may take a minute)...")
                cloud = RemarkableCloud()
                docs = cloud.list_documents()
                click.echo(f"  Cached {len(docs)} documents.")
                try:
                    cloud._resolve_folder_from_docs(docs, config.folder)
                    click.echo(f"  Folder {config.folder} found on reMarkable.")
                except RuntimeError:
                    click.echo(f"  Folder {config.folder} not found — create it on your reMarkable.")
            else:
                click.echo("  No RM cloud configured. Run: inkan-doc rm register <code>")
        except Exception as e:
            click.echo(f"  Cache warming skipped: {e}")


def _init_monorepo(monorepo: str, folder_prefix: str, output_dir: str | None) -> None:
    """Discover inkan-doc projects under monorepo and write a multi-project panbot.yaml."""
    monorepo_path = Path(monorepo).resolve()
    multi = PanbotMultiConfig(
        monorepo=str(monorepo_path),
        folder_prefix=folder_prefix,
    )
    discovered = multi.discover_projects()
    if not discovered:
        click.echo(f"No inkan-doc projects found under {monorepo_path}")
        raise SystemExit(1)

    click.echo(f"Found {len(discovered)} inkan-doc projects:")
    for p in discovered:
        click.echo(f"  {p.name:40s}  {p.folder}")

    out_dir = Path(output_dir) if output_dir else monorepo_path
    config_path = out_dir / "panbot.yaml"
    multi.save_to_file(config_path)
    click.echo(f"\nCreated {config_path}")
    click.echo("Run: panbot start --project-dir " + str(out_dir))


@panbot.command()
@click.option("--project-dir", type=click.Path(exists=True), default=None)
@click.argument("github_repo", required=False)
@click.option("--branch", default="", help="Git branch to push to.")
@click.option("--push/--no-push", default=True, help="Auto-push commits to GitHub.")
def link(project_dir: str | None, github_repo: str | None, branch: str, push: bool):
    """Link panbot project to a GitHub repo.

    Examples:

        panbot link owner/repo

        panbot link owner/repo --branch panbot

        panbot link --no-push
    """
    import subprocess

    cfg, config_path = _find_config(project_dir)
    if not cfg:
        click.echo("No panbot.yaml found. Run: panbot init")
        raise SystemExit(1)

    pdir = Path(cfg.project_dir)

    # Check if already a git repo
    is_git = subprocess.run(
        ["git", "rev-parse", "--git-dir"],
        cwd=pdir, capture_output=True,
    ).returncode == 0

    if not is_git:
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=pdir, check=True, capture_output=True)
        subprocess.run(
            ["git", "add", "."], cwd=pdir, check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", "panbot: initial commit"],
            cwd=pdir, check=True, capture_output=True,
        )
        click.echo(f"Initialized git repo in {pdir}")

    if github_repo:
        # Set remote origin
        result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=pdir, capture_output=True, text=True,
        )
        if result.returncode == 0:
            # Remote exists — update it
            subprocess.run(
                ["git", "remote", "set-url", "origin",
                 f"https://github.com/{github_repo}.git"],
                cwd=pdir, check=True, capture_output=True,
            )
        else:
            subprocess.run(
                ["git", "remote", "add", "origin",
                 f"https://github.com/{github_repo}.git"],
                cwd=pdir, check=True, capture_output=True,
            )
        click.echo(f"Remote: https://github.com/{github_repo}")

    if not branch:
        # Use current branch
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=pdir, capture_output=True, text=True,
        )
        branch = result.stdout.strip() if result.returncode == 0 else "main"

    # Update config
    cfg.github_repo = github_repo or cfg.github_repo
    cfg.git_branch = branch
    cfg.git_push = push
    cfg.save_to_file(config_path)

    click.echo(f"Updated {config_path}:")
    click.echo(f"  github_repo: {cfg.github_repo}")
    click.echo(f"  git_branch:  {cfg.git_branch}")
    click.echo(f"  git_push:    {cfg.git_push}")

    # Test push if configured
    if push and cfg.github_repo:
        click.echo("\nTesting push...")
        try:
            subprocess.run(
                ["git", "push", "-u", "origin", branch],
                cwd=pdir, check=True, capture_output=True, text=True, timeout=30,
            )
            click.echo(f"Push OK → origin/{branch}")
        except subprocess.CalledProcessError as e:
            click.echo(f"Push failed: {e.stderr.strip() if e.stderr else e}")
            click.echo("Check your GitHub credentials (gh auth login)")
        except subprocess.TimeoutExpired:
            click.echo("Push timed out — check network")


@panbot.command()
@click.option("--project-dir", type=click.Path(exists=True), default=None)
def start(project_dir: str | None):
    """Start the panbot daemon (single or multi-project)."""
    raw_cfg, config_path = _find_any_config(project_dir)
    if not raw_cfg:
        click.echo("No panbot.yaml found. Run: panbot init")
        raise SystemExit(1)

    if isinstance(raw_cfg, PanbotMultiConfig):
        from .panbot_daemon import PanbotMultiDaemon
        cfgs = raw_cfg.to_project_configs()
        if not cfgs:
            click.echo("No projects configured.")
            raise SystemExit(1)
        click.echo(f"Starting panbot for {len(cfgs)} projects...")
        for c in cfgs:
            click.echo(f"  {c.name or Path(c.project_dir).name:40s}  {c.folder}")
        daemon = PanbotMultiDaemon(cfgs)
        daemon.run()
        return

    cfg = raw_cfg
    cfg.state_dir.mkdir(parents=True, exist_ok=True)

    if cfg.pid_file.exists():
        pid = int(cfg.pid_file.read_text().strip())
        try:
            os.kill(pid, 0)
            click.echo(f"Panbot already running (PID {pid})")
            return
        except ProcessLookupError:
            cfg.pid_file.unlink()

    click.echo(f"Starting panbot for {Path(cfg.project_dir).name}...")
    click.echo(f"  Polling every {cfg.poll_interval}s, trigger after {cfg.inactivity_trigger}s idle")
    click.echo(f"  Watching: {cfg.folder}")
    click.echo(f"  Log: {cfg.log_file}")

    from .panbot_daemon import PanbotDaemon
    daemon = PanbotDaemon(cfg)
    daemon.run()


@panbot.command()
@click.option("--project-dir", type=click.Path(exists=True), default=None)
def stop(project_dir: str | None):
    """Stop the panbot daemon."""
    cfg, _ = _find_config(project_dir)
    if not cfg or not cfg.pid_file.exists():
        click.echo("No panbot daemon running.")
        return
    pid = int(cfg.pid_file.read_text().strip())
    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"Stopped panbot (PID {pid})")
    except ProcessLookupError:
        click.echo("Daemon already stopped.")
    cfg.pid_file.unlink(missing_ok=True)


@panbot.command()
@click.option("--project-dir", type=click.Path(exists=True), default=None)
def status(project_dir: str | None):
    """Show panbot daemon status (single or multi-project)."""
    raw_cfg, _ = _find_any_config(project_dir)
    if not raw_cfg:
        click.echo("No panbot.yaml found. Not initialized.")
        return

    if isinstance(raw_cfg, PanbotMultiConfig):
        cfgs = raw_cfg.to_project_configs()
        click.echo(f"{len(cfgs)} projects configured:")
        for cfg in cfgs:
            name = cfg.name or Path(cfg.project_dir).name
            if cfg.pid_file.exists():
                pid_text = cfg.pid_file.read_text().strip()
                try:
                    pid = int(pid_text)
                    os.kill(pid, 0)
                    click.echo(f"  [running PID {pid:6d}]  {name:40s}  {cfg.folder}")
                    continue
                except (ProcessLookupError, ValueError):
                    cfg.pid_file.unlink(missing_ok=True)
            click.echo(f"  [stopped         ]  {name:40s}  {cfg.folder}")
        return

    cfg = raw_cfg
    if cfg.pid_file.exists():
        pid = int(cfg.pid_file.read_text().strip())
        try:
            os.kill(pid, 0)
            click.echo(f"Panbot running (PID {pid})")
            click.echo(f"  Project: {Path(cfg.project_dir).name}")
            click.echo(f"  Folder: {cfg.folder}")
            if cfg.github_repo:
                click.echo(f"  GitHub: {cfg.github_repo} (branch: {cfg.git_branch or 'default'})")
                click.echo(f"  Push: {'enabled' if cfg.git_push else 'disabled'}")
            return
        except ProcessLookupError:
            cfg.pid_file.unlink()

    click.echo("Panbot is not running.")


@panbot.command()
@click.option("--project-dir", type=click.Path(exists=True), default=None)
def push(project_dir: str | None):
    """Force-push current PDF to reMarkable."""
    cfg, _ = _find_config(project_dir)
    if not cfg:
        click.echo("No panbot.yaml found. Run: panbot init")
        raise SystemExit(1)

    from .panbot_versions import VersionManager
    vm = VersionManager(cfg)
    current = vm.current_pdf
    if not current or not current.exists():
        click.echo("No current version. Build first: inkan-doc build pdf")
        raise SystemExit(1)

    from .rm_cloud import RemarkableCloud
    cloud = RemarkableCloud()
    cloud.upload_document(current, parent_folder=cfg.folder)
    click.echo(f"Pushed {current.name} to {cfg.folder}")


@panbot.command()
@click.option("--project-dir", type=click.Path(exists=True), default=None)
def pull(project_dir: str | None):
    """Force-pull and process latest changes."""
    cfg, _ = _find_config(project_dir)
    if not cfg:
        click.echo("No panbot.yaml found. Run: panbot init")
        raise SystemExit(1)

    from .panbot_daemon import PanbotDaemon
    daemon = PanbotDaemon(cfg)
    daemon.process_once()


@panbot.command()
@click.option("--project-dir", type=click.Path(exists=True), default=None)
def diff(project_dir: str | None):
    """Generate diff from last two versions."""
    cfg, _ = _find_config(project_dir)
    if not cfg:
        click.echo("No panbot.yaml found. Run: panbot init")
        raise SystemExit(1)

    from .panbot_versions import VersionManager
    vm = VersionManager(cfg)
    diff_path = vm.build_diff()
    if diff_path:
        click.echo(f"Diff: {diff_path}")
    else:
        click.echo("Need at least 2 versions to diff.")


@panbot.command("log")
@click.option("--project-dir", type=click.Path(exists=True), default=None)
@click.option("-n", "--lines", default=50)
def show_log(project_dir: str | None, lines: int):
    """Tail the panbot daemon log."""
    cfg, _ = _find_config(project_dir)
    if not cfg or not cfg.log_file.exists():
        click.echo("No log file found.")
        return
    text = cfg.log_file.read_text()
    for line in text.splitlines()[-lines:]:
        click.echo(line)


@panbot.command()
@click.option("--project-dir", type=click.Path(exists=True), default=None)
def history(project_dir: str | None):
    """Show version history."""
    cfg, _ = _find_config(project_dir)
    if not cfg:
        click.echo("No panbot.yaml found.")
        return

    from .panbot_versions import VersionManager
    vm = VersionManager(cfg)
    versions = vm.list_versions()
    if not versions:
        click.echo("No versions yet.")
        return
    click.echo(f"{len(versions)} versions:")
    for v in versions:
        click.echo(f"  {v['version']:3d}  {v['timestamp']}  {v['source']}")


@panbot.command()
@click.option("--port", default=8417, help="Monitor port")
def monitor(port: int):
    """Open the monitoring dashboard in a browser."""
    import webbrowser
    url = f"http://localhost:{port}"
    click.echo(f"Opening {url}")
    webbrowser.open(url)


# --- Tags ---

@panbot.group()
def tags():
    """Manage tags on reMarkable documents."""
    pass


@tags.command("list")
@click.argument("document_name")
@click.option("--pages", is_flag=True, help="Also show page-level tags")
def tags_list(document_name: str, pages: bool):
    """List tags on a reMarkable document."""
    from .rm_cloud import RemarkableCloud
    cloud = RemarkableCloud()
    all_tags = cloud.get_tags_by_name(document_name)

    doc_tags = all_tags["tags"]
    if doc_tags:
        click.echo(f"Document tags ({len(doc_tags)}):")
        for t in doc_tags:
            click.echo(f"  {t['name']}")
    else:
        click.echo("No document-level tags.")

    if pages:
        page_tags = all_tags["pageTags"]
        if page_tags:
            click.echo(f"\nPage tags ({len(page_tags)}):")
            for t in page_tags:
                click.echo(f"  {t['name']} (page: {t['pageId']})")
        else:
            click.echo("No page-level tags.")


@tags.command("add")
@click.argument("document_name")
@click.argument("tag_name")
@click.option("--page-id", help="Page UUID (omit for document-level tag)")
def tags_add(document_name: str, tag_name: str, page_id: str | None):
    """Add a tag to a reMarkable document."""
    from .rm_cloud import RemarkableCloud
    cloud = RemarkableCloud()
    doc = cloud._resolve_name_to_doc(document_name)
    doc_id = doc["ID"]

    if page_id:
        tag = cloud.add_page_tag(doc_id, tag_name, page_id)
        click.echo(f"Added page tag '{tag['name']}' to page {tag['pageId']}")
    else:
        tag = cloud.add_doc_tag(doc_id, tag_name)
        click.echo(f"Added tag '{tag['name']}'")


@tags.command("remove")
@click.argument("document_name")
@click.argument("tag_name")
@click.option("--page-id", help="Page UUID (omit to remove document-level tag)")
@click.option("--all-pages", is_flag=True, help="Remove tag from all pages")
def tags_remove(document_name: str, tag_name: str, page_id: str | None, all_pages: bool):
    """Remove a tag from a reMarkable document."""
    from .rm_cloud import RemarkableCloud
    cloud = RemarkableCloud()
    doc = cloud._resolve_name_to_doc(document_name)
    doc_id = doc["ID"]

    if page_id or all_pages:
        count = cloud.remove_page_tag(doc_id, tag_name, page_id)
        if count:
            click.echo(f"Removed {count} page tag(s) '{tag_name}'")
        else:
            click.echo(f"Tag '{tag_name}' not found on page(s).")
    else:
        if cloud.remove_doc_tag(doc_id, tag_name):
            click.echo(f"Removed tag '{tag_name}'")
        else:
            click.echo(f"Tag '{tag_name}' not found.")


@tags.command("pages")
@click.argument("document_name")
def tags_pages(document_name: str):
    """List page UUIDs in a document (for use with --page-id)."""
    from .rm_cloud import RemarkableCloud
    cloud = RemarkableCloud()
    doc = cloud._resolve_name_to_doc(document_name)
    page_ids = cloud.get_doc_pages(doc["ID"])
    if page_ids:
        click.echo(f"Pages ({len(page_ids)}):")
        for i, pid in enumerate(page_ids, 1):
            click.echo(f"  {i}. {pid}")
    else:
        click.echo("No pages found.")


@tags.command("find")
@click.argument("tag_name")
def tags_find(tag_name: str):
    """Find all documents with a specific tag."""
    from .rm_cloud import RemarkableCloud
    cloud = RemarkableCloud()
    click.echo(f"Searching for tag '{tag_name}'...")
    results = cloud.find_by_tag(tag_name)
    if results:
        click.echo(f"Found {len(results)} document(s):")
        for r in results:
            click.echo(f"  {r['VissibleName']} ({r['ID'][:8]}...)")
    else:
        click.echo("No documents found with that tag.")


def main():
    panbot()
