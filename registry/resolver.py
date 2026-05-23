"""
Custom semver parser and deterministic dependency resolver.

Supported constraints:
- exact: 1.2.3
- caret: ^1.2.3
- tilde: ~1.2.3
- comparator ranges: >=1.0.0 <2.0.0

No semver resolver library is used.
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Dict, List, Optional, Tuple

from metadata import get_all_versions_for_name


_SEMVER_RE = re.compile(
    r"^(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z.-]+))?"
    r"(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)
_COMPARATOR_RE = re.compile(r"^(>=|<=|>|<|=)\s*(\S+)$")
_VERSION_ONLY_RE = re.compile(
    r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$"
)


class Version:
    """Comparable semver version."""

    __slots__ = ("major", "minor", "patch", "pre", "raw")

    def __init__(
        self,
        major: int,
        minor: int,
        patch: int,
        pre: Optional[str] = None,
        raw: str = "",
    ):
        self.major = major
        self.minor = minor
        self.patch = patch
        self.pre = pre
        self.raw = raw

    @classmethod
    def parse(cls, value: str) -> "Version":
        match = _SEMVER_RE.match(value.strip())
        if not match:
            raise ValueError(f"Invalid semver: {value!r}")
        return cls(
            int(match.group("major")),
            int(match.group("minor")),
            int(match.group("patch")),
            match.group("pre"),
            raw=value.strip(),
        )

    def __eq__(self, other):
        return (
            self.major,
            self.minor,
            self.patch,
            self.pre,
        ) == (
            other.major,
            other.minor,
            other.patch,
            other.pre,
        )

    def __lt__(self, other):
        left = (self.major, self.minor, self.patch)
        right = (other.major, other.minor, other.patch)
        if left != right:
            return left < right
        if self.pre is not None and other.pre is None:
            return True
        if self.pre is None and other.pre is not None:
            return False
        if self.pre is not None and other.pre is not None:
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


class Constraint:
    def __init__(self, op: str, version: Version):
        self.op = op
        self.version = version

    def satisfies(self, version: Version) -> bool:
        if self.op == "=":
            return version == self.version
        if self.op == ">=":
            return version >= self.version
        if self.op == ">":
            return version > self.version
        if self.op == "<=":
            return version <= self.version
        if self.op == "<":
            return version < self.version
        raise ValueError(f"Unknown comparator: {self.op}")

    def __repr__(self):
        return f"{self.op}{self.version}"


class ConstraintSet:
    def __init__(self, constraints: List[Constraint], raw: str):
        self.constraints = constraints
        self.raw = raw

    def satisfies(self, version: Version) -> bool:
        return all(constraint.satisfies(version) for constraint in self.constraints)

    def __repr__(self):
        return self.raw


def parse_constraint(spec: str) -> ConstraintSet:
    spec = spec.strip()
    if not spec:
        raise ValueError("Empty version constraint")

    if spec.startswith("^"):
        version = Version.parse(spec[1:])
        return ConstraintSet(_caret_constraints(version), spec)

    if spec.startswith("~"):
        version = Version.parse(spec[1:])
        return ConstraintSet(_tilde_constraints(version), spec)

    constraints: List[Constraint] = []
    for part in spec.split():
        match = _COMPARATOR_RE.match(part)
        if match:
            constraints.append(Constraint(match.group(1), Version.parse(match.group(2))))
        elif _VERSION_ONLY_RE.match(part):
            constraints.append(Constraint("=", Version.parse(part)))
        else:
            raise ValueError(f"Cannot parse constraint part: {part!r} in {spec!r}")

    return ConstraintSet(constraints, spec)


def _caret_constraints(version: Version) -> List[Constraint]:
    lower = Constraint(">=", version)
    if version.major > 0:
        upper = Constraint(
            "<",
            Version(version.major + 1, 0, 0, raw=f"{version.major + 1}.0.0"),
        )
        return [lower, upper]
    if version.minor > 0:
        upper = Constraint(
            "<",
            Version(0, version.minor + 1, 0, raw=f"0.{version.minor + 1}.0"),
        )
        return [lower, upper]
    return [Constraint("=", version)]


def _tilde_constraints(version: Version) -> List[Constraint]:
    return [
        Constraint(">=", version),
        Constraint(
            "<",
            Version(version.major, version.minor + 1, 0, raw=f"{version.major}.{version.minor + 1}.0"),
        ),
    ]


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
        return get_all_versions_for_name(self.db_path, name)

    def resolve(self, root_deps: List[Dict[str, str]]) -> List[Dict[str, Any]]:
        all_constraints: Dict[str, List[Tuple[ConstraintSet, str]]] = {}
        queued_constraints: set = set()
        pending: deque = deque()
        for dep in root_deps:
            pending.append((dep["name"], dep["version"], f"root->{dep['name']}"))

        selected: Dict[str, Dict[str, Any]] = {}
        graph: Dict[str, List[str]] = {}

        while pending:
            name, spec, path = pending.popleft()
            key = (name, spec, path)
            if key in queued_constraints:
                continue
            queued_constraints.add(key)

            constraint = parse_constraint(spec)
            all_constraints.setdefault(name, []).append((constraint, path))

            picked = self._pick_version(name, all_constraints[name])
            if picked is None:
                available = [row["version"] for row in self._fetch_versions(name)]
                constraints = ", ".join(
                    f"{item!r} via {constraint_path}"
                    for item, constraint_path in all_constraints[name]
                )
                raise ConflictError(
                    f"No version of '{name}' satisfies all constraints: "
                    f"{constraints}. Available: {available}"
                )

            previous = selected.get(name, {}).get("version")
            selected[name] = picked
            graph[name] = [dep["name"] for dep in picked.get("deps", [])]

            if previous != picked["version"]:
                for sub_dep in sorted(picked.get("deps", []), key=lambda item: item["name"]):
                    pending.append(
                        (
                            sub_dep["name"],
                            sub_dep["version"],
                            f"{path}->{sub_dep['name']}",
                        )
                    )

        self._detect_cycles(graph)

        lockfile = []
        for name, picked in selected.items():
            first_constraint = all_constraints[name][0][0].raw
            lockfile.append(
                {
                    "name": name,
                    "version": picked["version"],
                    "sha256": picked["sha256"],
                    "resolved_from_constraint": first_constraint,
                }
            )

        lockfile.sort(key=lambda item: item["name"])
        return lockfile

    def _pick_version(
        self,
        name: str,
        constraints: List[Tuple[ConstraintSet, str]],
    ) -> Optional[Dict[str, Any]]:
        candidates = []
        for row in self._fetch_versions(name):
            try:
                candidates.append((Version.parse(row["version"]), row))
            except ValueError:
                continue

        candidates.sort(key=lambda item: item[0], reverse=True)
        for version, row in candidates:
            if all(constraint.satisfies(version) for constraint, _ in constraints):
                return row
        return None

    def _detect_cycles(self, graph: Dict[str, List[str]]) -> None:
        white, gray, black = 0, 1, 2
        color: Dict[str, int] = {}
        path: List[str] = []

        def dfs(node: str) -> None:
            color[node] = gray
            path.append(node)
            for neighbor in graph.get(node, []):
                if color.get(neighbor) == gray:
                    cycle_start = path.index(neighbor)
                    cycle = path[cycle_start:] + [neighbor]
                    raise CycleError(f"Dependency cycle detected: {' -> '.join(cycle)}")
                if color.get(neighbor, white) == white:
                    dfs(neighbor)
            path.pop()
            color[node] = black

        for name in sorted(graph):
            if color.get(name, white) == white:
                dfs(name)
