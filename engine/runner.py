"""
Job runner: executes individual CI jobs in isolated Docker containers.

Isolation enforced:
- --network forge-net (only registry reachable)
- --memory / --memory-swap from YAML
- --cpus from YAML
- --cap-drop ALL --security-opt no-new-privileges
- --pids-limit 200
- --read-only on everything except workspace tmpfs
- per-job workspace volume
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("runner")

DOCKER_NETWORK = os.getenv("FORGE_BUILD_NETWORK", "forge_build_net")
JOB_TIMEOUT_SECS = int(os.getenv("FORGE_JOB_TIMEOUT", str(30 * 60)))  # 30 min


def _mem_bytes_to_docker(n: int) -> str:
    """Convert bytes → docker memory string like '512m'."""
    return f"{n}"


class JobRunner:
    def __init__(self, registry_url: str, forge_token: str):
        self.registry_url = registry_url
        self.forge_token = forge_token

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------
    async def run_job(
        self,
        run_id: str,
        job_name: str,
        job_def: Dict[str, Any],
        lockfile: List[Dict[str, Any]],
        pipeline: Dict[str, Any],
        log_dir: str,
        workspace: Path,
    ) -> Dict[str, Any]:
        """
        Run a job inside a Docker container. Returns:
          {"success": True/False, "integrity_failure": bool, ...}
        """
        log_path = Path(log_dir) / f"{job_name}.log"

        return await self._run_with_workspace(
            run_id=run_id,
            job_name=job_name,
            job_def=job_def,
            lockfile=lockfile,
            pipeline=pipeline,
            log_path=log_path,
            workspace=workspace,
        )

    async def _run_with_workspace(
        self,
        run_id: str,
        job_name: str,
        job_def: Dict[str, Any],
        lockfile: List[Dict[str, Any]],
        pipeline: Dict[str, Any],
        log_path: Path,
        workspace: Path,
    ) -> Dict[str, Any]:
        resources = job_def.get("resources", {})
        memory_bytes = resources.get("memory_bytes", 512 * 1024 * 1024)
        cpu = float(resources.get("cpu", 1.0))
        runtime = job_def.get("runtime", "alpine:3.18")

        # Ensure Docker image is available
        await self._pull_image_if_needed(runtime, log_path)

        # Build Docker command
        container_name = f"forge-{run_id[:8]}-{job_name}-{int(time.time())}"
        steps_script = self._build_steps_script(job_def["steps"])
        safe_job_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", job_name)
        script_name = f"__forge_run_{safe_job_name}.sh"
        script_path = workspace / script_name
        script_path.write_text(steps_script)
        script_path.chmod(0o755)

        cmd = [
            "docker", "run",
            "--rm",
            "--name", container_name,
            # Network isolation: only forge internal network
            "--network", DOCKER_NETWORK,
            # Resource limits
            "--memory", str(memory_bytes),
            "--memory-swap", str(memory_bytes),  # no swap
            "--cpus", str(cpu),
            # Security
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "200",
            "--read-only",
            "--tmpfs", "/tmp",
            # Workspace volume (rw)
            "-v", f"{workspace}:/workspace:rw",
            # Working dir
            "-w", "/workspace",
            # Environment
            "-e", f"FORGE_TOKEN={self.forge_token}",
            "-e", f"FORGE_URL={self.registry_url}",
            "-e", "HOME=/workspace",
            # Image and command
            runtime,
            "sh", f"/workspace/{script_name}",
        ]

        self._log_line(log_path, job_name, f"[forge] Starting job '{job_name}' on {runtime}")
        self._log_line(log_path, job_name, f"[forge] Resources: {cpu} CPU, {memory_bytes // (1024**2)}MB RAM")

        success = await self._run_container(
            cmd=cmd,
            container_name=container_name,
            log_path=log_path,
            job_name=job_name,
            timeout=JOB_TIMEOUT_SECS,
        )

        if success:
            self._log_line(log_path, job_name, f"[forge] Job '{job_name}' succeeded")
            # Copy workspace back so engine can find artifacts
            # (workspace is the shared dir, artifacts are declared by path)
            return {"success": True, "workspace": str(workspace)}
        else:
            self._log_line(log_path, job_name, f"[forge] Job '{job_name}' FAILED")
            return {"success": False}

    def _build_steps_script(self, steps: List[Dict[str, str]]) -> str:
        """Build a shell script that runs steps sequentially, failing on error."""
        lines = ["#!/bin/sh", "set -e"]
        for step in steps:
            step_name = step["name"]
            lines.append(f"\necho '[forge] === Step: {step_name} ==='")
            lines.append(step["run"])
        lines.append("\necho '[forge] All steps completed'")
        return "\n".join(lines) + "\n"

    async def _pull_image_if_needed(self, image: str, log_path: Path) -> None:
        """Pull Docker image, logging progress."""
        self._log_line(log_path, "forge", f"[forge] Pulling image {image}...")
        proc = await asyncio.create_subprocess_exec(
            "docker", "pull", image,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode != 0:
            self._log_line(log_path, "forge", f"[forge] WARNING: docker pull failed: {stdout.decode()[:500]}")

    async def _run_container(
        self,
        cmd: List[str],
        container_name: str,
        log_path: Path,
        job_name: str,
        timeout: int,
    ) -> bool:
        """
        Run a Docker container, stream stdout/stderr line by line to log file.
        Returns True on success (exit code 0).
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except Exception as exc:
            self._log_line(log_path, job_name, f"[forge] Failed to start container: {exc}")
            return False

        try:
            async with asyncio.timeout(timeout):
                while True:
                    line_bytes = await proc.stdout.readline()
                    if not line_bytes:
                        break
                    line = line_bytes.decode(errors="replace").rstrip("\n")
                    self._log_line(log_path, job_name, line)

                await proc.wait()
        except asyncio.TimeoutError:
            self._log_line(log_path, job_name, f"[forge] Job timed out after {timeout}s — killing container")
            try:
                kill_proc = await asyncio.create_subprocess_exec(
                    "docker", "kill", container_name,
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await kill_proc.wait()
            except Exception:
                pass
            return False

        rc = proc.returncode
        if rc == 137:
            self._log_line(log_path, job_name, "[forge] Container OOM killed (exit 137)")
        return rc == 0

    async def pull_deps(
        self,
        lockfile: List[Dict[str, Any]],
        workspace: Path,
        log_path: Path,
    ) -> Optional[Dict]:
        """
        Pull all locked dependencies into workspace/deps/<name>/.
        Verifies SHA-256 of each downloaded blob.
        Returns integrity failure dict if any mismatch, else None.
        """
        import aiohttp
        deps_dir = workspace / "deps"
        deps_dir.mkdir(exist_ok=True)

        async with aiohttp.ClientSession() as session:
            for entry in lockfile:
                name = entry["name"]
                version = entry["version"]
                expected_sha = entry["sha256"]
                url = f"{self.registry_url}/artifacts/{name}/{version}"

                self._log_line(log_path, "forge", f"[forge] Pulling dep {name}@{version} ...")

                dest_dir = deps_dir / name
                dest_dir.mkdir(exist_ok=True)
                dest_file = dest_dir / f"{name}-{version}.tar.gz"

                actual_sha = hashlib.sha256()
                try:
                    async with session.get(url) as resp:
                        if resp.status != 200:
                            raise Exception(f"Registry returned {resp.status} for {name}@{version}")
                        with open(str(dest_file), "wb") as fout:
                            async for chunk in resp.content.iter_chunked(65536):
                                actual_sha.update(chunk)
                                fout.write(chunk)
                except Exception as exc:
                    self._log_line(log_path, "forge", f"[forge] ERROR pulling {name}@{version}: {exc}")
                    return {"success": False}

                hex_actual = actual_sha.hexdigest()
                if hex_actual != expected_sha:
                    msg = (
                        f"[forge] INTEGRITY FAILURE for {name}@{version}: "
                        f"expected={expected_sha} actual={hex_actual}"
                    )
                    self._log_line(log_path, "forge", msg)
                    return {
                        "success": False,
                        "integrity_failure": True,
                        "artifact": f"{name}@{version}",
                        "expected_sha": expected_sha,
                        "actual_sha": hex_actual,
                    }

                self._log_line(log_path, "forge", f"[forge] ✓ {name}@{version} sha256={hex_actual[:16]}...")

        return None

    async def publish_artifact(
        self,
        run_id: str,
        artifact: Dict[str, Any],
        pipeline: Dict[str, Any],
        workspace: str,
    ) -> None:
        """
        Auto-publish an artifact declared in the pipeline.
        The artifact path is relative to the shared workspace.
        """
        import aiohttp

        # Find the workspace for the last succeeded job
        # Artifacts are left in the run's workspace dir
        art_path_str = artifact["path"].lstrip("./")
        # Search job workspaces for the file
        # The run_dir has workspace subdirs
        found_path = None
        for candidate in Path(workspace).rglob(art_path_str):
            if candidate.is_file():
                found_path = candidate
                break

        if found_path is None:
            raise FileNotFoundError(
                f"Artifact file not found: {artifact['path']} under {workspace}"
            )

        # Compute sha256
        sha = hashlib.sha256()
        with open(str(found_path), "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                sha.update(chunk)
        hex_sha = sha.hexdigest()

        # Upload to registry
        deps_payload = json.dumps(pipeline.get("dependencies", []))
        url = f"{self.registry_url}/artifacts/{artifact['name']}/{artifact['version']}"

        headers = {"Authorization": f"Bearer {self.forge_token}"}

        async with aiohttp.ClientSession(headers=headers) as session:
            with open(str(found_path), "rb") as f:
                data = aiohttp.FormData()
                data.add_field("file", f, filename=found_path.name)
                data.add_field("checksum", f"sha256:{hex_sha}")
                data.add_field("deps", deps_payload)
                async with session.post(url, data=data) as resp:
                    if resp.status not in (200, 201):
                        body = await resp.text()
                        raise Exception(f"Publish failed ({resp.status}): {body}")

        logger.info("Auto-published %s@%s", artifact["name"], artifact["version"])

    def _log_line(self, log_path: Path, job: str, line: str) -> None:
        """Write a timestamped log line to the job log file."""
        import datetime
        ts = datetime.datetime.now(datetime.timezone.utc).isoformat()
        entry = json.dumps({"ts": ts, "job": job, "line": line})
        with open(str(log_path), "a", buffering=1) as f:
            f.write(entry + "\n")
