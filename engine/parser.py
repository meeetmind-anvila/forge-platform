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