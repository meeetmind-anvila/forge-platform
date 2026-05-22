#!/usr/bin/env python3
"""
forge — Forge CI/CD platform CLI.

Commands:
  forge login <url>                       Store credentials
  forge token create <name>               Create a new API token (server-side)
  forge run <pipeline.yaml>               Submit a pipeline run
  forge logs <run-id> [--follow]          Fetch/stream logs
  forge publish <path> --name N --version V [--deps '...']
  forge resolve <pipeline.yaml>           Print lockfile without running
  forge ls <package>                      List artifact versions
  forge status <run-id>                   Get run status
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Optional

import click
import requests
import sseclient

# ---------------------------------------------------------------------------
# Config store (~/.forge/config.json)
# ---------------------------------------------------------------------------
CONFIG_DIR = Path.home() / ".forge"
CONFIG_FILE = CONFIG_DIR / "config.json"


def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_config(cfg: dict) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2))
    CONFIG_FILE.chmod(0o600)


def get_base_url() -> str:
    cfg = load_config()
    url = cfg.get("url")
    if not url:
        click.echo("Error: not logged in. Run: forge login <url>", err=True)
        sys.exit(1)
    return url.rstrip("/")


def get_token() -> str:
    cfg = load_config()
    token = cfg.get("token")
    if not token:
        click.echo("Error: no token stored. Run: forge login <url>", err=True)
        sys.exit(1)
    return token


def auth_headers() -> dict:
    return {"Authorization": f"Bearer {get_token()}"}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
@click.group()
def cli():
    """Forge — CI/CD platform with integrated artifact registry."""
    pass


@cli.command()
@click.argument("url")
@click.option("--token", prompt=True, hide_input=True, help="API bearer token")
def login(url: str, token: str):
    """Store server URL and bearer token credentials."""
    cfg = load_config()
    cfg["url"] = url.rstrip("/")
    cfg["token"] = token
    save_config(cfg)
    click.echo(f"✓ Logged in to {url}")


@cli.group()
def token():
    """Manage API tokens."""
    pass


@token.command("create")
@click.argument("name")
def token_create(name: str):
    """
    Create a new API token on the server (requires admin access).
    The raw token is shown once.
    """
    base = get_base_url()
    resp = requests.post(
        f"{base}/admin/tokens",
        json={"name": name},
        headers=auth_headers(),
    )
    if resp.status_code == 200:
        data = resp.json()
        click.echo(f"Token '{name}' created.")
        click.echo(f"Raw token (save this, shown only once):\n  {data['token']}")
    else:
        click.echo(f"Error: {resp.status_code} {resp.text}", err=True)
        sys.exit(1)


@cli.command("run")
@click.argument("pipeline_file", type=click.Path(exists=True))
def run_pipeline(pipeline_file: str):
    """Submit a pipeline YAML file for execution."""
    base = get_base_url()
    with open(pipeline_file, "rb") as f:
        resp = requests.post(
            f"{base}/runs",
            files={"pipeline": (Path(pipeline_file).name, f, "text/yaml")},
            headers=auth_headers(),
        )
    if resp.status_code in (200, 201):
        data = resp.json()
        run_id = data["run_id"]
        click.echo(f"✓ Pipeline submitted")
        click.echo(f"  Run ID: {run_id}")
        click.echo(f"  Logs:   forge logs {run_id} --follow")
    else:
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)


@cli.command("logs")
@click.argument("run_id")
@click.option("--follow", "-f", is_flag=True, help="Stream logs in real-time")
def get_logs(run_id: str, follow: bool):
    """Fetch logs for a run. Use --follow to stream live."""
    base = get_base_url()
    url = f"{base}/runs/{run_id}/logs"
    params = {"follow": "true"} if follow else {}

    headers = {"Accept": "text/event-stream", "Cache-Control": "no-cache"}

    resp = requests.get(url, params=params, stream=True, headers=headers)
    if resp.status_code != 200:
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)

    client = sseclient.SSEClient(resp)
    for event in client.events():
        if event.event == "done":
            break
        if event.data and event.data.strip():
            try:
                entry = json.loads(event.data)
                ts = entry.get("ts", "")
                job = entry.get("job", "")
                line = entry.get("line", "")
                if ts:
                    click.echo(f"[{ts}] [{job}] {line}")
                else:
                    click.echo(line)
            except json.JSONDecodeError:
                click.echo(event.data)


@cli.command("publish")
@click.argument("path", type=click.Path(exists=True))
@click.option("--name", required=True, help="Artifact name")
@click.option("--version", required=True, help="Artifact version (semver)")
@click.option("--deps", default=None, help="JSON array of dependency {name,version} objects")
def publish(path: str, name: str, version: str, deps: Optional[str]):
    """Publish an artifact to the registry."""
    import hashlib

    base = get_base_url()

    # Compute sha256
    sha = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            sha.update(chunk)
    checksum = f"sha256:{sha.hexdigest()}"

    with open(path, "rb") as f:
        data = {"checksum": checksum}
        if deps:
            data["deps"] = deps
        resp = requests.post(
            f"{base}/artifacts/{name}/{version}",
            files={"file": (Path(path).name, f, "application/octet-stream")},
            data=data,
            headers=auth_headers(),
        )

    if resp.status_code in (200, 201):
        d = resp.json()
        click.echo(f"✓ Published {name}@{version}")
        click.echo(f"  SHA-256: {d['sha256']}")
        click.echo(f"  Size:    {d['size']} bytes")
    elif resp.status_code == 409:
        click.echo(f"✗ {name}@{version} already exists (immutable)", err=True)
        sys.exit(1)
    elif resp.status_code == 400:
        click.echo(f"✗ Bad request: {resp.json().get('detail', resp.text)}", err=True)
        sys.exit(1)
    else:
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)


@cli.command("resolve")
@click.argument("pipeline_file", type=click.Path(exists=True))
def resolve(pipeline_file: str):
    """Print the resolved lockfile for a pipeline without running it."""
    base = get_base_url()
    with open(pipeline_file, "rb") as f:
        content = f.read()

    # Parse locally first
    sys.path.insert(0, str(Path(__file__).parent.parent / "engine"))
    try:
        from parser import parse_pipeline, PipelineError
        pipeline = parse_pipeline(content.decode())
    except ImportError:
        # Not co-located with engine — submit and get lockfile
        resp = requests.post(
            f"{base}/runs",
            files={"pipeline": (Path(pipeline_file).name, content, "text/yaml")},
            headers=auth_headers(),
        )
        if resp.status_code not in (200, 201):
            click.echo(f"Error: {resp.text}", err=True)
            sys.exit(1)
        run_id = resp.json()["run_id"]
        # Poll for lockfile
        for _ in range(30):
            time.sleep(1)
            lr = requests.get(f"{base}/runs/{run_id}/lockfile")
            if lr.status_code == 200:
                click.echo(json.dumps(lr.json(), indent=2))
                return
        click.echo("Lockfile not yet available", err=True)
        sys.exit(1)
        return

    except Exception as exc:
        click.echo(f"Pipeline parse error: {exc}", err=True)
        sys.exit(1)

    deps = pipeline.get("dependencies", [])
    if not deps:
        click.echo("[]")
        return

    resp = requests.post(
        f"{base}/resolve",
        json={"dependencies": deps},
        headers={"Content-Type": "application/json"},
    )
    if resp.status_code == 200:
        data = resp.json()
        click.echo(json.dumps(data.get("lockfile", data), indent=2))
    else:
        click.echo(f"Resolution failed: {resp.json().get('detail', resp.text)}", err=True)
        sys.exit(1)


@cli.command("ls")
@click.argument("package")
def list_versions(package: str):
    """List all versions of a package in the registry."""
    base = get_base_url()
    resp = requests.get(f"{base}/artifacts/{package}")
    if resp.status_code == 200:
        data = resp.json()
        versions = data.get("versions", [])
        if not versions:
            click.echo(f"No versions found for '{package}'")
            return
        click.echo(f"{package}:")
        for v in versions:
            click.echo(
                f"  {v['version']}  sha256={v['sha256'][:16]}...  "
                f"size={v['size']}  published={v['published_at']}"
            )
    elif resp.status_code == 404:
        click.echo(f"Package '{package}' not found")
    else:
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)


@cli.command("status")
@click.argument("run_id")
def status(run_id: str):
    """Get the current status of a run."""
    base = get_base_url()
    resp = requests.get(f"{base}/runs/{run_id}", headers=auth_headers())
    if resp.status_code == 200:
        data = resp.json()
        click.echo(f"Run:    {run_id}")
        click.echo(f"Status: {data['status']}")
        click.echo(f"Jobs:")
        for job_name, job in data.get("jobs", {}).items():
            click.echo(f"  {job_name}: {job['status']}")
    else:
        click.echo(f"Error {resp.status_code}: {resp.text}", err=True)
        sys.exit(1)


if __name__ == "__main__":
    cli()