"""
DAG scheduler for CI job execution.

Implements:
- Kahn's algorithm for topological sort
- DFS cycle detection (white/gray/black coloring)
- Parallel execution up to max_concurrency
- Failed job → dependents marked SKIPPED, not FAILED
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Coroutine, Dict, List, Optional, Set

logger = logging.getLogger("scheduler")

# Job status values
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_INTEGRITY_FAILURE = "integrity_failure"
STATUS_CONFLICT_FAILURE = "conflict_failure"
STATUS_CYCLE_FAILURE = "cycle_failure"

# Run-level terminal statuses
TERMINAL_STATUSES = {
    STATUS_SUCCEEDED,
    STATUS_FAILED,
    STATUS_INTEGRITY_FAILURE,
    STATUS_CONFLICT_FAILURE,
    STATUS_CYCLE_FAILURE,
}


class CycleError(Exception):
    pass


class DAG:
    """
    Directed Acyclic Graph of jobs.
    Nodes are job names, edges are needs relationships.
    """

    def __init__(self, jobs: Dict[str, Dict[str, Any]]):
        self.jobs = jobs
        self.edges: Dict[str, List[str]] = {name: [] for name in jobs}
        self.reverse_edges: Dict[str, List[str]] = {name: [] for name in jobs}

        for job_name, job in jobs.items():
            for needed in job.get("needs", []):
                self.edges[needed].append(job_name)
                self.reverse_edges[job_name].append(needed)

    def detect_cycles(self) -> None:
        """
        DFS with 3-color marking. Raises CycleError with the cycle path.
        WHITE=0 unvisited, GRAY=1 in-progress, BLACK=2 done.
        """
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {name: WHITE for name in self.jobs}
        path: List[str] = []

        def dfs(node: str):
            color[node] = GRAY
            path.append(node)
            for neighbor in self.edges[node]:
                if color[neighbor] == GRAY:
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    raise CycleError(
                        f"Cycle in job DAG: {' → '.join(cycle)}"
                    )
                if color[neighbor] == WHITE:
                    dfs(neighbor)
            path.pop()
            color[node] = BLACK

        for node in list(self.jobs.keys()):
            if color[node] == WHITE:
                dfs(node)

    def topological_sort(self) -> List[List[str]]:
        """
        Kahn's algorithm. Returns list of levels where jobs in the same level
        have no dependency between them and can run in parallel.
        """
        in_degree: Dict[str, int] = {name: 0 for name in self.jobs}
        for src, dsts in self.edges.items():
            for dst in dsts:
                in_degree[dst] += 1

        queue: deque = deque(
            [name for name, deg in in_degree.items() if deg == 0]
        )
        queue = deque(sorted(queue))  # deterministic ordering

        levels: List[List[str]] = []
        processed = 0

        while queue:
            # All nodes currently at degree 0 form one parallel level
            level = list(queue)
            queue.clear()
            levels.append(sorted(level))

            for node in level:
                processed += 1
                for neighbor in self.edges[node]:
                    in_degree[neighbor] -= 1
                    if in_degree[neighbor] == 0:
                        queue.append(neighbor)

        if processed != len(self.jobs):
            raise CycleError("Cycle detected during topological sort")

        return levels

    def get_dependents(self, job_name: str) -> Set[str]:
        """Return all transitive dependents of a job."""
        result: Set[str] = set()
        stack = list(self.edges[job_name])
        while stack:
            dep = stack.pop()
            if dep not in result:
                result.add(dep)
                stack.extend(self.edges[dep])
        return result


# ---------------------------------------------------------------------------
# RunManager
# ---------------------------------------------------------------------------

class RunManager:
    """
    Manages the lifecycle of pipeline runs.
    - Accepts new runs into a queue
    - Resolves deps, detects cycles, builds lockfiles before any job runs
    - Executes jobs respecting DAG order and parallelism limits
    - Persists state to disk for crash recovery
    """

    def __init__(
        self,
        runs_dir: str,
        registry_url: str,
        auth_db: str,
        max_concurrency: int = 4,
    ):
        self.runs_dir = Path(runs_dir)
        self.registry_url = registry_url
        self.auth_db = auth_db
        self.max_concurrency = max_concurrency
        self._states: Dict[str, Dict[str, Any]] = {}
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None
        self._semaphore: Optional[asyncio.Semaphore] = None

    async def start(self):
        self._semaphore = asyncio.Semaphore(self.max_concurrency)
        self._task = asyncio.create_task(self._process_loop())
        logger.info("RunManager started, max_concurrency=%d", self.max_concurrency)

    async def stop(self):
        if self._task:
            self._task.cancel()

    async def enqueue(self, run_id: str, pipeline: Dict[str, Any], submitter: str):
        state = {
            "run_id": run_id,
            "pipeline_name": pipeline["name"],
            "status": STATUS_QUEUED,
            "jobs": {
                name: {"status": STATUS_QUEUED, "started_at": None, "finished_at": None}
                for name in pipeline["jobs"]
            },
            "started_at": datetime.now(timezone.utc).isoformat(),
            "finished_at": None,
            "submitter": submitter,
            "lockfile": None,
        }
        self._states[run_id] = state
        self._persist_state(run_id)
        await self._queue.put((run_id, pipeline))
        logger.info("Enqueued run %s (%s)", run_id, pipeline["name"])

    def get_run_state(self, run_id: str) -> Optional[Dict[str, Any]]:
        return self._states.get(run_id)

    def _persist_state(self, run_id: str):
        state = self._states.get(run_id)
        if state:
            path = self.runs_dir / run_id / "state.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(state, indent=2))

    def _update_run_status(self, run_id: str, status: str, **kwargs):
        state = self._states[run_id]
        state["status"] = status
        for k, v in kwargs.items():
            state[k] = v
        if status in TERMINAL_STATUSES:
            state["finished_at"] = datetime.now(timezone.utc).isoformat()
        self._persist_state(run_id)

    def _update_job_status(self, run_id: str, job_name: str, status: str, **kwargs):
        state = self._states[run_id]
        job = state["jobs"][job_name]
        job["status"] = status
        for k, v in kwargs.items():
            job[k] = v
        if status in {STATUS_RUNNING}:
            job["started_at"] = datetime.now(timezone.utc).isoformat()
        if status in {STATUS_SUCCEEDED, STATUS_FAILED, STATUS_SKIPPED, STATUS_INTEGRITY_FAILURE}:
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
        self._persist_state(run_id)

    async def _process_loop(self):
        while True:
            run_id, pipeline = await self._queue.get()
            # Run each pipeline concurrently (pipeline-level concurrency)
            asyncio.create_task(self._execute_run(run_id, pipeline))

    async def _execute_run(self, run_id: str, pipeline: Dict[str, Any]):
        """Full lifecycle of a single pipeline run."""
        from runner import JobRunner
        from slack import notify_pipeline_started, notify_pipeline_finished, notify_resolution_failure, notify_integrity_failure
        import aiohttp

        run_dir = self.runs_dir / run_id
        log_dir = run_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        pipeline_name = pipeline["name"]
        notify_pipeline_started(run_id, pipeline_name)
        start_time = time.time()

        # ── 1. Resolve dependencies ──────────────────────────────────────
        lockfile = []
        if pipeline.get("dependencies"):
            self._update_run_status(run_id, STATUS_RUNNING)
            try:
                lockfile = await self._resolve_deps(pipeline["dependencies"])
            except Exception as exc:
                err = str(exc)
                logger.error("Run %s resolution failed: %s", run_id, err)
                if "Cycle" in err:
                    self._update_run_status(run_id, STATUS_CYCLE_FAILURE, error=err)
                else:
                    self._update_run_status(run_id, STATUS_CONFLICT_FAILURE, error=err)
                notify_resolution_failure(run_id, pipeline_name, err)
                return
        else:
            self._update_run_status(run_id, STATUS_RUNNING)

        # Persist lockfile
        lf_path = run_dir / "lockfile.json"
        lf_path.write_text(json.dumps(lockfile, indent=2, sort_keys=True))
        self._states[run_id]["lockfile"] = lockfile
        self._persist_state(run_id)

        # ── 2. Build and validate DAG ─────────────────────────────────────
        dag = DAG(pipeline["jobs"])
        try:
            dag.detect_cycles()
        except CycleError as exc:
            self._update_run_status(run_id, STATUS_CYCLE_FAILURE, error=str(exc))
            notify_resolution_failure(run_id, pipeline_name, str(exc))
            return

        try:
            levels = dag.topological_sort()
        except CycleError as exc:
            self._update_run_status(run_id, STATUS_CYCLE_FAILURE, error=str(exc))
            return

        # ── 3. Execute levels in order ───────────────────────────────────
        job_statuses: Dict[str, str] = {n: STATUS_QUEUED for n in pipeline["jobs"]}
        overall_failed = False
        integrity_failed = False

        # Get a token from auth DB to inject into containers
        forge_token = self._get_pipeline_token(run_id)

        runner = JobRunner(
            registry_url=self.registry_url,
            forge_token=forge_token,
        )

        import tempfile
        import shutil
        shared_workspace = Path(tempfile.mkdtemp(prefix=f"forge-{run_id[:8]}-workspace-"))

        try:
            # Pull dependencies into shared workspace once
            if lockfile:
                integrity_result = await runner.pull_deps(lockfile, shared_workspace, log_dir / "system.log")
                if integrity_result:
                    integrity_failed = True
                    overall_failed = True
                    self._update_run_status(run_id, STATUS_INTEGRITY_FAILURE)
                    notify_integrity_failure(
                        run_id,
                        integrity_result.get("artifact", "unknown"),
                        integrity_result.get("expected_sha", ""),
                        integrity_result.get("actual_sha", ""),
                    )

            if not overall_failed:
                for level in levels:
                    # Check which jobs in this level should be skipped (dep failed)
                    jobs_to_run = []
                    for job_name in level:
                        needs = pipeline["jobs"][job_name].get("needs", [])
                        if any(job_statuses.get(n) in {STATUS_FAILED, STATUS_SKIPPED, STATUS_INTEGRITY_FAILURE} for n in needs):
                            job_statuses[job_name] = STATUS_SKIPPED
                            self._update_job_status(run_id, job_name, STATUS_SKIPPED)
                            for dep in dag.get_dependents(job_name):
                                job_statuses[dep] = STATUS_SKIPPED
                                self._update_job_status(run_id, dep, STATUS_SKIPPED)
                        else:
                            jobs_to_run.append(job_name)

                    tasks = []
                    for job_name in jobs_to_run:
                        self._update_job_status(run_id, job_name, STATUS_RUNNING)
                        task = asyncio.create_task(
                            self._run_job_guarded(
                                runner=runner,
                                run_id=run_id,
                                job_name=job_name,
                                job_def=pipeline["jobs"][job_name],
                                lockfile=lockfile,
                                pipeline=pipeline,
                                log_dir=str(log_dir),
                                workspace=shared_workspace,
                            )
                        )
                        tasks.append((job_name, task))

                    for job_name, task in tasks:
                        try:
                            result = await task
                            if result.get("integrity_failure"):
                                job_statuses[job_name] = STATUS_INTEGRITY_FAILURE
                                self._update_job_status(run_id, job_name, STATUS_INTEGRITY_FAILURE)
                                integrity_failed = True
                                overall_failed = True
                                notify_integrity_failure(
                                    run_id,
                                    result.get("artifact", "unknown"),
                                    result.get("expected_sha", ""),
                                    result.get("actual_sha", ""),
                                )
                            elif result.get("success"):
                                job_statuses[job_name] = STATUS_SUCCEEDED
                                self._update_job_status(run_id, job_name, STATUS_SUCCEEDED)
                            else:
                                job_statuses[job_name] = STATUS_FAILED
                                self._update_job_status(run_id, job_name, STATUS_FAILED)
                                overall_failed = True
                        except Exception as exc:
                            logger.exception("Job %s/%s threw exception: %s", run_id, job_name, exc)
                            job_statuses[job_name] = STATUS_FAILED
                            self._update_job_status(run_id, job_name, STATUS_FAILED)
                            overall_failed = True

            # ── 4. Auto-publish artifacts ─────────────────────────────────────
            if not overall_failed and pipeline.get("artifacts"):
                for art in pipeline["artifacts"]:
                    try:
                        await runner.publish_artifact(
                            run_id=run_id,
                            artifact=art,
                            pipeline=pipeline,
                            workspace=str(shared_workspace),
                        )
                    except Exception as exc:
                        logger.error("Auto-publish %s failed: %s", art["name"], exc)
                        overall_failed = True
                        break

            # ── 5. Final status ───────────────────────────────────────────────
            duration = round(time.time() - start_time, 2)
            if integrity_failed:
                final = STATUS_INTEGRITY_FAILURE
            elif overall_failed:
                final = STATUS_FAILED
            else:
                final = STATUS_SUCCEEDED

            failing_job = next(
                (n for n, s in job_statuses.items() if s in {STATUS_FAILED, STATUS_INTEGRITY_FAILURE}),
                None,
            )
            self._update_run_status(run_id, final)
            notify_pipeline_finished(run_id, pipeline_name, final, duration, failing_job)
            logger.info("Run %s finished: %s (%.2fs)", run_id, final, duration)
        finally:
            try:
                shutil.rmtree(str(shared_workspace), ignore_errors=True)
            except Exception:
                pass

    async def _run_job_guarded(self, runner, run_id, job_name, job_def, lockfile, pipeline, log_dir, workspace):
        """Run a job with the global concurrency semaphore."""
        async with self._semaphore:
            return await runner.run_job(
                run_id=run_id,
                job_name=job_name,
                job_def=job_def,
                lockfile=lockfile,
                pipeline=pipeline,
                log_dir=log_dir,
                workspace=workspace,
            )

    async def _resolve_deps(self, dependencies: List[Dict[str, str]]) -> List[Dict]:
        """Call registry resolver API."""
        import aiohttp
        url = f"{self.registry_url}/resolve"
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json={"dependencies": dependencies}) as resp:
                data = await resp.json()
                if resp.status != 200:
                    raise Exception(data.get("detail", str(data)))
                return data["lockfile"]

    def _get_pipeline_token(self, run_id: str) -> str:
        """Get or create a pipeline token for container injection."""
        import sys
        sys.path.insert(0, "/app/registry")
        from auth import create_token
        token_name = f"pipeline-{run_id[:8]}"
        try:
            return create_token(self.auth_db, token_name)
        except Exception:
            # Token may already exist from a restart, regenerate
            from auth import revoke_token
            revoke_token(self.auth_db, token_name)
            return create_token(self.auth_db, token_name)