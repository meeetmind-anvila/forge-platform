"""
Pipeline YAML parser.
Uses ruamel.yaml for line-number-aware error reporting.
Unknown fields → error with line. Missing required → error with line.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap, CommentedSeq

yaml = YAML()
yaml.preserve_quotes = True


class PipelineError(Exception):
    """Raised for any pipeline validation error, message includes line info."""
    pass


# ---------------------------------------------------------------------------
# Schema definitions
# ---------------------------------------------------------------------------

PIPELINE_REQUIRED = {"name", "version", "jobs"}
PIPELINE_OPTIONAL = {"dependencies", "artifacts"}
PIPELINE_ALL = PIPELINE_REQUIRED | PIPELINE_OPTIONAL

JOB_REQUIRED = {"steps"}
JOB_OPTIONAL = {"runtime", "resources", "needs"}
JOB_ALL = JOB_REQUIRED | JOB_OPTIONAL

STEP_REQUIRED = {"name", "run"}
STEP_OPTIONAL: set = set()
STEP_ALL = STEP_REQUIRED | STEP_OPTIONAL

RESOURCES_OPTIONAL = {"cpu", "memory"}

DEP_REQUIRED = {"name", "version"}
DEP_OPTIONAL: set = set()

ARTIFACT_REQUIRED = {"name", "version", "path"}
ARTIFACT_OPTIONAL: set = set()


def _line(node: Any) -> str:
    """Try to extract line number from ruamel node."""
    if hasattr(node, "lc"):
        return f" (line {node.lc.line + 1})"
    return ""


def _check_fields(node: CommentedMap, required: set, allowed: set, ctx: str) -> None:
    """Validate fields of a mapping node."""
    for key in node:
        if key not in allowed:
            lc = node.lc.value(key) if hasattr(node, "lc") else None
            line_info = f" (line {lc[0] + 1})" if lc else ""
            raise PipelineError(
                f"Unknown field '{key}' in {ctx}{line_info}. "
                f"Allowed: {sorted(allowed)}"
            )
    for req in required:
        if req not in node:
            raise PipelineError(f"Missing required field '{req}' in {ctx}")


def _parse_memory(mem_str: str) -> int:
    """Parse memory string like '512Mi', '1Gi', '256M' to bytes."""
    m = re.match(r"^(\d+(?:\.\d+)?)(Ki|Mi|Gi|K|M|G|)$", str(mem_str).strip())
    if not m:
        raise PipelineError(f"Invalid memory value: {mem_str!r}")
    val = float(m.group(1))
    unit = m.group(2)
    multipliers = {
        "Ki": 1024,
        "Mi": 1024 ** 2,
        "Gi": 1024 ** 3,
        "K": 1000,
        "M": 1000 ** 2,
        "G": 1000 ** 3,
        "": 1,
    }
    return int(val * multipliers[unit])


def parse_pipeline(yaml_text: str) -> Dict[str, Any]:
    """
    Parse and validate a pipeline YAML string.
    Returns a dict with keys: name, version, dependencies, jobs, artifacts.
    Raises PipelineError with line info on any validation failure.
    """
    try:
        doc = yaml.load(yaml_text)
    except Exception as exc:
        raise PipelineError(f"YAML parse error: {exc}")

    if doc is None or not isinstance(doc, dict):
        raise PipelineError("Pipeline document is empty or not a mapping")

    _check_fields(doc, PIPELINE_REQUIRED, PIPELINE_ALL, "pipeline root")

    name = doc["name"]
    if not isinstance(name, str) or not name.strip():
        raise PipelineError("'name' must be a non-empty string")

    version = str(doc["version"])
    if not re.match(r"^\d+\.\d+\.\d+", version):
        raise PipelineError(f"Pipeline 'version' must be semver, got: {version!r}")

    # --- dependencies ---
    dependencies = []
    if "dependencies" in doc:
        raw_deps = doc["dependencies"]
        if not isinstance(raw_deps, (list, CommentedSeq)):
            raise PipelineError(f"'dependencies' must be a list{_line(doc)}")
        for i, dep in enumerate(raw_deps):
            if not isinstance(dep, dict):
                raise PipelineError(f"dependencies[{i}] must be a mapping")
            _check_fields(dep, DEP_REQUIRED, DEP_REQUIRED | DEP_OPTIONAL, f"dependencies[{i}]")
            dependencies.append({
                "name": str(dep["name"]),
                "version": str(dep["version"]),
            })

    # --- jobs ---
    raw_jobs = doc["jobs"]
    if not isinstance(raw_jobs, dict):
        raise PipelineError(f"'jobs' must be a mapping{_line(doc)}")
    if not raw_jobs:
        raise PipelineError("'jobs' must have at least one job")

    jobs = {}
    for job_name, job_def in raw_jobs.items():
        if not isinstance(job_def, dict):
            raise PipelineError(f"jobs.{job_name} must be a mapping")
        _check_fields(job_def, JOB_REQUIRED, JOB_ALL, f"jobs.{job_name}")

        # steps
        raw_steps = job_def["steps"]
        if not isinstance(raw_steps, (list, CommentedSeq)) or not raw_steps:
            raise PipelineError(f"jobs.{job_name}.steps must be a non-empty list")

        steps = []
        for si, step in enumerate(raw_steps):
            if not isinstance(step, dict):
                raise PipelineError(f"jobs.{job_name}.steps[{si}] must be a mapping")
            _check_fields(step, STEP_REQUIRED, STEP_ALL, f"jobs.{job_name}.steps[{si}]")
            steps.append({
                "name": str(step["name"]),
                "run": str(step["run"]),
            })

        # resources
        resources = {"cpu": 1.0, "memory_bytes": 512 * 1024 * 1024}
        if "resources" in job_def:
            res = job_def["resources"]
            if not isinstance(res, dict):
                raise PipelineError(f"jobs.{job_name}.resources must be a mapping")
            for k in res:
                if k not in RESOURCES_OPTIONAL:
                    raise PipelineError(f"Unknown resource field '{k}' in jobs.{job_name}.resources")
            if "cpu" in res:
                try:
                    resources["cpu"] = float(res["cpu"])
                except (ValueError, TypeError):
                    raise PipelineError(f"jobs.{job_name}.resources.cpu must be numeric")
            if "memory" in res:
                resources["memory_bytes"] = _parse_memory(res["memory"])

        # runtime
        runtime = str(job_def.get("runtime", "alpine:3.18"))

        # needs
        needs = []
        if "needs" in job_def:
            n = job_def["needs"]
            if isinstance(n, str):
                needs = [n]
            elif isinstance(n, (list, CommentedSeq)):
                needs = [str(x) for x in n]
            else:
                raise PipelineError(f"jobs.{job_name}.needs must be a string or list")
            for needed in needs:
                if needed == job_name:
                    raise PipelineError(f"jobs.{job_name} cannot depend on itself")

        jobs[job_name] = {
            "runtime": runtime,
            "resources": resources,
            "steps": steps,
            "needs": needs,
        }

    # Validate needs references
    for job_name, job in jobs.items():
        for needed in job["needs"]:
            if needed not in jobs:
                raise PipelineError(
                    f"jobs.{job_name} needs '{needed}' which is not defined"
                )

    # --- artifacts ---
    artifacts = []
    if "artifacts" in doc:
        raw_arts = doc["artifacts"]
        if not isinstance(raw_arts, (list, CommentedSeq)):
            raise PipelineError("'artifacts' must be a list")
        for i, art in enumerate(raw_arts):
            if not isinstance(art, dict):
                raise PipelineError(f"artifacts[{i}] must be a mapping")
            _check_fields(art, ARTIFACT_REQUIRED, ARTIFACT_REQUIRED | ARTIFACT_OPTIONAL, f"artifacts[{i}]")
            art_version = str(art["version"])
            if not re.match(r"^\d+\.\d+\.\d+", art_version):
                raise PipelineError(
                    f"artifacts[{i}].version must be semver, got: {art_version!r}"
                )
            artifacts.append({
                "name": str(art["name"]),
                "version": art_version,
                "path": str(art["path"]),
            })

    return {
        "name": name,
        "version": version,
        "dependencies": dependencies,
        "jobs": jobs,
        "artifacts": artifacts,
    }