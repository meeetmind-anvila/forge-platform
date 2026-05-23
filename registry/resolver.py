"""
Custom semver parser and dependency resolver.

Supported constraint syntax:
  exact:    1.2.3
  caret:    ^1.2.3   → >=1.2.3 <2.0.0  (^0.2.3 → >=0.2.3 <0.3.0, ^0.0.3 → =0.0.3)
  tilde:    ~1.2.3   → >=1.2.3 <1.3.0
  ranges:   >=1.0.0 <2.0.0  (space-separated comparators AND-ed together)
  single:   >=1.0.0 | >1.0.0 | <=2.0.0 | <2.0.0 | =1.0.0

Resolution algorithm (BFS):
  1. Collect all constraints on each package from all paths
  2. Fetch available versions from registry
  3. Filter versions satisfying ALL constraints
  4. Select highest satisfying version
  5. Recurse for transitive deps

Determinism: versions sorted semantically, highest selected → identical lockfile
for identical registry state.
"""

import re
import sqlite3
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from metadata import get_all_versions_for_name


# ---------------------------------------------------------------------------
# Semver parsing
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[a-zA-Z0-9._-]+))?"
    r"(?:\+(?P<build>[a-zA-Z0-9._-]+))?$"
)


class Version:
    """Comparable semver version."""

    __slots__ = ("major", "minor", "patch", "pre", "raw")

    def __init__(self, major: int, minor: int, patch: int, pre: Optional[str] = None, raw: str = ""):
        self.major = major
        self.minor = minor
        self.patch = patch
        self.pre = pre  # None means release; any string means pre-release
        self.raw = raw

    @classmethod
    def parse(cls, s: str) -> "Version":
        m = _SEMVER_RE.match(s.strip())
        if not m:
            raise ValueError(f"Invalid semver: {s!r}")
        return cls(
            int(m.group("major")),
            int(m.group("minor")),
            int(m.group("patch")),
            m.group("pre"),
            raw=s.strip(),
        )

    def _tuple(self):
        # Pre-release versions sort BEFORE the release: treat None as highest
        pre_sort = (0,) if self.pre is None else (1, self.pre)
        return (self.major, self.minor, self.patch, pre_sort)

    def __eq__(self, other):
        return self._tuple() == other._tuple()

    def __lt__(self, other):
        a = (self.major, self.minor, self.patch)
        b = (other.major, other.minor, other.patch)
        if a != b:
            return a < b
        # pre-release < release
        if self.pre is not None and other.pre is None:
            return True
        if self.pre is None and other.pre is not None:
            return False
        if self.pre and other.pre:
            return self.pre < other.pre
        return False

    def __le__(self, other):
        return self == other or self < other

    def __gt__(self, other):
        return other < self

    def __ge__(self, other):
        return other <= self

    def __repr__(self):
        return self.raw


# ---------------------------------------------------------------------------
# Constraint parsing
# ---------------------------------------------------------------------------

class Constraint:
    """A single version constraint, e.g. >=1.2.3 or <2.0.0."""

    def __init__(self, op: str, version: Version):
        self.op = op
        self.version = version

    def satisfies(self, v: Version) -> bool:
        if self.op == "=":
            return v == self.version
        if self.op == ">=":
            return v >= self.version
        if self.op == ">":
            return v > self.version
        if self.op == "<=":
            return v <= self.version
        if self.op == "<":
            return v < self.version
        raise ValueError(f"Unknown op {self.op!r}")

    def __repr__(self):
        return f"{self.op}{self.version}"


class ConstraintSet:
    """AND-combination of Constraint objects."""

    def __init__(self, constraints: List[Constraint], raw: str = ""):
        self.constraints = constraints
        self.raw = raw

    def satisfies(self, v: Version) -> bool:
        return all(c.satisfies(v) for c in self.constraints)

    def __repr__(self):
        return self.raw or " ".join(repr(c) for c in self.constraints)


_COMPARATOR_RE = re.compile(r"(>=|<=|>|<|=)\s*(\S+)")
_VERSION_ONLY_RE = re.compile(r"^(\d+\.\d+\.\d+.*)$")


def parse_constraint(spec: str) -> ConstraintSet:
    """
    Parse a version constraint string into a ConstraintSet.
    Supports: exact, ^, ~, >=, <=, >, <, = and space-separated AND ranges.
    """
    spec = spec.strip()
    raw = spec

    if spec.startswith("^"):
        v = Version.parse(spec[1:])
        constraints = _caret_constraints(v)
        return ConstraintSet(constraints, raw=raw)

    if spec.startswith("~"):
        v = Version.parse(spec[1:])
        constraints = _tilde_constraints(v)
        return ConstraintSet(constraints, raw=raw)

    # Space-separated comparators: >=1.0.0 <2.0.0
    parts = spec.split()
    all_constraints: List[Constraint] = []
    for part in parts:
        part = part.strip()
        if not part:
            continue
        m = _COMPARATOR_RE.match(part)
        if m:
            op, ver_str = m.group(1), m.group(2)
            all_constraints.append(Constraint(op, Version.parse(ver_str)))
        elif _VERSION_ONLY_RE.match(part):
            all_constraints.append(Constraint("=", Version.parse(part)))
        else:
            raise ValueError(f"Cannot parse constraint part: {part!r} in {spec!r}")

    return ConstraintSet(all_constraints, raw=raw)


def _caret_constraints(v: Version) -> List[Constraint]:
    """
    ^1.2.3 → >=1.2.3 <2.0.0
    ^0.2.3 → >=0.2.3 <0.3.0
    ^0.0.3 → =0.0.3
    """
    lower = Constraint(">=", v)
    if v.major > 0:
        upper = Constraint("<", Version(v.major + 1, 0, 0, raw=f"{v.major+1}.0.0"))
    elif v.minor > 0:
        upper = Constraint("<", Version(0, v.minor + 1, 0, raw=f"0.{v.minor+1}.0"))
    else:
        return [Constraint("=", v)]
    return [lower, upper]


def _tilde_constraints(v: Version) -> List[Constraint]:
    """~1.2.3 → >=1.2.3 <1.3.0"""
    lower = Constraint(">=", v)
    upper = Constraint("<", Version(v.major, v.minor + 1, 0, raw=f"{v.major}.{v.minor+1}.0"))
    return [lower, upper]


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------

class ResolutionError(Exception):
    pass


class CycleError(ResolutionError):
    pass


class ConflictError(ResolutionError):
    pass


class DependencyResolver:
    def __init__(self, db_path: str):
        self.db_path = db_path

    def _fetch_versions(self, name: str) -> List[Dict[str, Any]]:
        """Fetch all versions of a package from the registry DB."""
        return get_all_versions_for_name(self.db_path, name)

    def resolve(self, root_deps: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        """
        Resolve a list of {name, version (constraint)} dependencies.
        Returns a sorted lockfile list of {name, version, sha256, resolved_from_constraint}.
        Raises CycleError or ConflictError on failures.
        """
        # Map package_name → list of (constraint_set, path_string)
        constraints: Dict[str, List[Tuple[ConstraintSet, str]]] = {}
        # BFS queue: (name, constraint_str, path)
        queue: deque = deque()

        for dep in root_deps:
            name = dep["name"]
            spec = dep["version"]
            cs = parse_constraint(spec)
            queue.append((name, cs, spec, f"root→{name}"))

        # Track what we've already enqueued to avoid re-fetching indefinitely
        visited_with_version: Dict[str, str] = {}  # name → resolved version str

        # First pass: collect all constraints via BFS
        # We need to iteratively expand deps until stable
        resolved: Dict[str, str] = {}  # name → version string
        sha_map: Dict[str, str] = {}   # name → sha256
        constraint_map: Dict[str, str] = {}  # name → original constraint string

        # Collect all constraints first
        all_constraints: Dict[str, List[Tuple[ConstraintSet, str]]] = {}
        # BFS to gather constraints
        seen_pkg_constraints: set = set()
        raw_queue: deque = deque()
        for dep in root_deps:
            raw_queue.append((dep["name"], dep["version"], f"root→{dep['name']}"))

        while raw_queue:
            name, spec, path = raw_queue.popleft()
            key = (name, spec)
            if key in seen_pkg_constraints:
                continue
            seen_pkg_constraints.add(key)

            cs = parse_constraint(spec)
            if name not in all_constraints:
                all_constraints[name] = []
            all_constraints[name].append((cs, path))

            # Find what version we'd pick and grab ITS deps too
            picked = self._pick_version(name, all_constraints[name])
            if picked:
                for sub_dep in picked.get("deps", []):
                    sub_path = f"{path}→{sub_dep['name']}"
                    raw_queue.append((sub_dep["name"], sub_dep["version"], sub_path))

        # Now do final resolution with all constraints known
        # Cycle detection on the resolution graph
        self._detect_cycles(root_deps)

        lockfile = []
        for pkg_name, csets in all_constraints.items():
            picked = self._pick_version(pkg_name, csets)
            if picked is None:
                # Build helpful error message
                available = [r["version"] for r in self._fetch_versions(pkg_name)]
                constraints_str = ", ".join(
                    f"{cs!r} (via {path})" for cs, path in csets
                )
                raise ConflictError(
                    f"No version of '{pkg_name}' satisfies all constraints: "
                    f"{constraints_str}. Available: {available}"
                )
            # Store the original constraint string (first one that pulled this in)
            orig_constraint = csets[0][0].raw
            lockfile.append({
                "name": pkg_name,
                "version": picked["version"],
                "sha256": picked["sha256"],
                "resolved_from_constraint": orig_constraint,
            })

        # Sort by name for determinism
        lockfile.sort(key=lambda x: x["name"])
        return lockfile

    def _pick_version(
        self, name: str, csets: List[Tuple[ConstraintSet, str]]
    ) -> Optional[Dict[str, Any]]:
        """
        Given a package name and list of (ConstraintSet, path) pairs,
        return the highest version satisfying all constraints, or None.
        """
        rows = self._fetch_versions(name)
        if not rows:
            return None

        # Parse and sort versions semantically, descending
        candidates = []
        for row in rows:
            try:
                v = Version.parse(row["version"])
                candidates.append((v, row))
            except ValueError:
                continue

        candidates.sort(key=lambda x: x[0], reverse=True)

        for version_obj, row in candidates:
            if all(cs.satisfies(version_obj) for cs, _ in csets):
                return row

        return None

    def _detect_cycles(self, root_deps: List[Dict[str, str]]) -> None:
        """
        DFS cycle detection on the dependency graph.
        Raises CycleError with the cycle path if one is found.
        """
        # Build adjacency: for each package, find deps of whatever version we'd pick
        # Use a simplified "best guess" version for cycle detection
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {}
        path: List[str] = []

        def get_deps_of(name: str) -> List[str]:
            rows = self._fetch_versions(name)
            if not rows:
                return []
            # Use latest version for cycle detection
            try:
                latest = max(rows, key=lambda r: Version.parse(r["version"]))
                return [d["name"] for d in latest.get("deps", [])]
            except Exception:
                return []

        def dfs(node: str):
            color[node] = GRAY
            path.append(node)
            for neighbor in get_deps_of(node):
                if color.get(neighbor) == GRAY:
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    raise CycleError(
                        f"Dependency cycle detected: {' → '.join(cycle)}"
                    )
                if color.get(neighbor, WHITE) == WHITE:
                    dfs(neighbor)
            path.pop()
            color[node] = BLACK

        for dep in root_deps:
            name = dep["name"]
            if color.get(name, WHITE) == WHITE:
                dfs(name)