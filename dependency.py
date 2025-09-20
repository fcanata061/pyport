# dependency.py
"""
Dependency graph manager for PyPort
----------------------------------

Features:
 - Represent packages and dependency edges as a directed graph.
 - Support for version constraints using `packaging` (if available) or a
   simple fallback parser.
 - Topological sort (install order) with cycle detection (Kahn algorithm).
 - Reverse dependency queries (packages that depend on X).
 - Uninstall order (reverse topological) to remove safely.
 - Conflict detection (incompatible version constraints).
 - Export/import graph as JSON.
 - Export graph as Graphviz DOT.
 - Simple CLI for common actions: build graph from portfiles, show orders,
   find cycles, visualize.
 - Integration hooks for reading an "installed DB" (optional).
"""

from __future__ import annotations
import os
import sys
import json
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set, Iterable, Any
from collections import defaultdict, deque, namedtuple

# Try to use packaging for robust version & specifier handling
try:
    from packaging.version import Version, InvalidVersion
    from packaging.specifiers import SpecifierSet, InvalidSpecifier
    _HAS_PACKAGING = True
except Exception:
    _HAS_PACKAGING = False

# Data structures
Dependency = namedtuple("Dependency", ["name", "constraint", "optional"])  # constraint: string like ">=1.2,<2.0"
NodeInfo = Dict[str, Any]  # arbitrary metadata e.g. version, category, source_url


# Exceptions
class DependencyError(Exception):
    pass

class CycleError(DependencyError):
    def __init__(self, cycle: List[str]):
        super().__init__(f"Dependency cycle detected: {' -> '.join(cycle)}")
        self.cycle = cycle

class UnsatisfiableError(DependencyError):
    def __init__(self, package: str, constraint: str):
        super().__init__(f"Unsatisfiable constraint for {package}: {constraint}")
        self.package = package
        self.constraint = constraint

class VersionParseError(DependencyError):
    pass


# --- Version helpers ---------------------------------------------------------

def parse_version(v: str):
    """Parse a version string; use packaging.Version if available, else return raw string."""
    if v is None:
        return None
    if _HAS_PACKAGING:
        try:
            return Version(str(v))
        except InvalidVersion as e:
            raise VersionParseError(f"Invalid version '{v}': {e}")
    else:
        # fallback: return raw string (comparisons will be simplistic)
        return str(v)

def parse_constraint(spec: Optional[str]):
    """
    Return a SpecifierSet if packaging available; else return the raw string.
    Spec examples: '>=1.2,<2.0', '==1.4'
    """
    if not spec:
        return None
    if _HAS_PACKAGING:
        try:
            return SpecifierSet(spec)
        except InvalidSpecifier as e:
            raise VersionParseError(f"Invalid version specifier '{spec}': {e}")
    else:
        # fallback: we only support equality "==x" or simple ">=x" lexicographic comparisons
        return str(spec)


def satisfies_version(ver: Optional[str], constraint) -> bool:
    """Check if version satisfies constraint. If packaging not available do best-effort."""
    if constraint is None:
        return True
    if _HAS_PACKAGING:
        if ver is None:
            # no version known -> cannot guarantee, assume ok (could be conservative)
            return True
        try:
            v = Version(str(ver))
        except InvalidVersion:
            return True
        return v in constraint  # SpecifierSet membership
    else:
        # naive fallback: support "==", and prefix ">=" ",<"
        if ver is None:
            return True
        v = str(ver)
        s = str(constraint)
        # split on comma
        parts = [p.strip() for p in s.split(",")]
        for p in parts:
            if p.startswith("=="):
                if v != p[2:]:
                    return False
            elif p.startswith(">="):
                if v < p[2:]:
                    return False
            elif p.startswith("<="):
                if v > p[2:]:
                    return False
            elif p.startswith(">"):
                if v <= p[1:]:
                    return False
            elif p.startswith("<"):
                if v >= p[1:]:
                    return False
            else:
                # unknown constraint form -> ignore
                continue
        return True


# --- Graph class -------------------------------------------------------------

class DependencyGraph:
    """
    Graph representation:
      - nodes: dict name -> NodeInfo (metadata)
      - edges: adjacency dict name -> list of Dependency (outgoing edges: package -> its deps)
    """

    def __init__(self):
        self.nodes: Dict[str, NodeInfo] = {}
        self.edges: Dict[str, List[Dependency]] = defaultdict(list)
        self._reverse: Optional[Dict[str, Set[str]]] = None  # lazy reverse index

    # Node operations
    def add_node(self, name: str, info: Optional[NodeInfo] = None):
        if name not in self.nodes:
            self.nodes[name] = info or {}
        else:
            if info:
                # merge metadata
                self.nodes[name].update(info)

    def remove_node(self, name: str):
        if name in self.nodes:
            del self.nodes[name]
        if name in self.edges:
            del self.edges[name]
        # remove incoming edges
        for k, deps in list(self.edges.items()):
            newdeps = [d for d in deps if d.name != name]
            self.edges[k] = newdeps
        self._reverse = None

    # Edge operations
    def add_dependency(self, package: str, dep_name: str, constraint: Optional[str] = None, optional: bool = False):
        self.add_node(package)
        self.add_node(dep_name)
        dep = Dependency(dep_name, constraint, optional)
        # replace existing dependency to same name if present
        deps = [d for d in self.edges[package] if d.name != dep_name]
        deps.append(dep)
        self.edges[package] = deps
        self._reverse = None

    def remove_dependency(self, package: str, dep_name: str):
        if package in self.edges:
            self.edges[package] = [d for d in self.edges[package] if d.name != dep_name]
            self._reverse = None

    def get_dependencies(self, package: str) -> List[Dependency]:
        return list(self.edges.get(package, []))

    def get_node_info(self, package: str) -> NodeInfo:
        return self.nodes.get(package, {})

    # Reverse index
    def _build_reverse(self):
        rev: Dict[str, Set[str]] = defaultdict(set)
        for pkg, deps in self.edges.items():
            for d in deps:
                rev[d.name].add(pkg)
        self._reverse = rev

    def reverse_dependencies(self, package: str) -> Set[str]:
        """Return set of packages that (directly) depend on the given package."""
        if self._reverse is None:
            self._build_reverse()
        return set(self._reverse.get(package, set()))

    # Transitive reverse (who depends on X recursively)
    def reverse_dependencies_transitive(self, package: str) -> Set[str]:
        result: Set[str] = set()
        q = deque([package])
        if self._reverse is None:
            self._build_reverse()
        while q:
            p = q.popleft()
            for parent in self._reverse.get(p, set()):
                if parent not in result:
                    result.add(parent)
                    q.append(parent)
        return result

    # Topological sort (Kahn), returning ordered list of nodes for installation:
    # dependencies come before dependents. Raises CycleError if cycle detected.
    def install_order(self, targets: Optional[Iterable[str]] = None, include_optional: bool = True) -> List[str]:
        """
        Compute installation order for the given targets (or all nodes if None).
        If include_optional=False optional dependencies are treated as absent.
        """
        # Build reduced graph
        if targets is None:
            nodes = set(self.nodes.keys())
        else:
            # include targets plus all their transitive deps
            nodes = set()
            for t in targets:
                if t not in self.nodes:
                    raise DependencyError(f"Unknown package: {t}")
                self._collect_deps_recursive(t, nodes, include_optional)
        # compute in-degree
        indeg: Dict[str, int] = {n: 0 for n in nodes}
        adj: Dict[str, List[str]] = {n: [] for n in nodes}
        for pkg in nodes:
            for d in self.edges.get(pkg, []):
                if (not include_optional) and d.optional:
                    continue
                if d.name in nodes:
                    indeg[d.name] += 1
                    adj[pkg].append(d.name)

        # Kahn's algorithm BUT note topological order we want deps before dependents
        # So edges should be dep->pkg. We built edges pkg->dep, so invert.
        # Instead compute incoming counts based on reversed edges:
        # Recompute indeg_rev: count incoming edges from deps (i.e., number of deps for each node)
        indeg_rev = {n: 0 for n in nodes}
        rev_adj = {n: [] for n in nodes}  # dep -> list of packages that depend on it
        for pkg in nodes:
            for d in self.edges.get(pkg, []):
                if (not include_optional) and d.optional:
                    continue
                if d.name in nodes:
                    indeg_rev[pkg] += 1
                    rev_adj.setdefault(d.name, []).append(pkg)

        q = deque([n for n, deg in indeg_rev.items() if deg == 0])
        order: List[str] = []
        while q:
            n = q.popleft()
            order.append(n)
            for dependent in rev_adj.get(n, []):
                indeg_rev[dependent] -= 1
                if indeg_rev[dependent] == 0:
                    q.append(dependent)

        if len(order) != len(nodes):
            # find cycle (simple heuristic using DFS)
            cycle = self._find_cycle(nodes, include_optional)
            raise CycleError(cycle)
        return order

    def _collect_deps_recursive(self, pkg: str, out_set: Set[str], include_optional: bool):
        if pkg in out_set:
            return
        out_set.add(pkg)
        for d in self.edges.get(pkg, []):
            if (not include_optional) and d.optional:
                continue
            self._collect_deps_recursive(d.name, out_set, include_optional)

    def uninstall_order(self, targets: Iterable[str]) -> List[str]:
        """
        Determine a safe uninstall order for the given targets:
        remove dependents before dependencies (reverse of install order of full affected set).
        """
        # Build full affected set: targets + their transitive reverse deps (i.e. packages that depend on them)
        affected: Set[str] = set(targets)
        for t in list(targets):
            affected |= self.reverse_dependencies_transitive(t)

        # Compute an install order for affected set, then reverse it for uninstall
        order = self.install_order(targets=affected, include_optional=True)
        # uninstall dependents first -> reverse
        return list(reversed(order))

    def detect_conflicts(self) -> List[Tuple[str, List[Dependency]]]:
        """
        Find cases where multiple packages require conflicting constraints on same package.
        Returns list of tuples (dep_name, [Dependency entries from different packages])
        """
        conflicts: List[Tuple[str, List[Dependency]]] = []
        # gather constraints per dep
        per_dep: Dict[str, List[Dependency]] = defaultdict(list)
        for pkg, deps in self.edges.items():
            for d in deps:
                per_dep[d.name].append(Dependency(pkg + "->" + d.name, d.constraint, d.optional))
        # check pairwise compatibility
        for dep_name, reqs in per_dep.items():
            # if only one requester -> no conflict
            if len(reqs) <= 1:
                continue
            # naive compatibility check: try to find any version satisfying all constraints
            # If packaging available, we can check intersection of SpecifierSets
            if _HAS_PACKAGING:
                # start with open SpecifierSet
                combined = None
                compatible = True
                for r in reqs:
                    spec = r.constraint
                    if spec is None:
                        continue
                    sset = SpecifierSet(spec)
                    if combined is None:
                        combined = sset
                    else:
                        # intersection cannot be tested directly; we test by sampling a set of candidate versions?
                        # Simpler heuristic: test arbitrary versions? instead detect if combined & sset empty via strings -> best-effort
                        combined = SpecifierSet(str(combined) + "," + str(sset))
                # We won't try to sample versions; assume compatible unless obviously contradictory equality
                # Check for direct contradictions like "==1.0" and "==2.0"
                eqs = set()
                for r in reqs:
                    c = r.constraint
                    if c and "==" in c:
                        parts = [p.strip() for p in c.split(",") if p.strip().startswith("==")]
                        for p in parts:
                            eqs.add(p[2:])
                if len(eqs) > 1:
                    conflicts.append((dep_name, reqs))
            else:
                # fallback: if there are differing equality constraints treat as conflict
                eqs = set()
                for r in reqs:
                    c = r.constraint
                    if c and c.startswith("=="):
                        eqs.add(c[2:])
                if len(eqs) > 1:
                    conflicts.append((dep_name, reqs))
        return conflicts

    # Utilities: find a cycle (DFS)
    def _find_cycle(self, nodes_subset: Optional[Set[str]] = None, include_optional: bool = True) -> List[str]:
        if nodes_subset is None:
            nodes_subset = set(self.nodes.keys())
        visited: Set[str] = set()
        stack: List[str] = []

        def dfs(node: str, path: List[str], visiting: Set[str]) -> Optional[List[str]]:
            visiting.add(node)
            path.append(node)
            for d in self.edges.get(node, []):
                if (not include_optional) and d.optional:
                    continue
                if d.name not in nodes_subset:
                    continue
                if d.name in visiting:
                    # cycle found
                    idx = path.index(d.name) if d.name in path else 0
                    return path[idx:] + [d.name]
                if d.name not in visited:
                    res = dfs(d.name, path, visiting)
                    if res:
                        return res
            visiting.remove(node)
            visited.add(node)
            path.pop()
            return None

        for n in list(nodes_subset):
            if n in visited:
                continue
            res = dfs(n, [], set())
            if res:
                return res
        return []

    # Export / Import
    def to_dict(self) -> Dict[str, Any]:
        return {"nodes": self.nodes, "edges": {k: [{"name": d.name, "constraint": d.constraint, "optional": d.optional} for d in v] for k, v in self.edges.items()}}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DependencyGraph":
        g = cls()
        for n, info in (data.get("nodes") or {}).items():
            g.add_node(n, info)
        for pkg, deps in (data.get("edges") or {}).items():
            for d in deps:
                g.add_dependency(pkg, d["name"], d.get("constraint"), d.get("optional", False))
        return g

    def save_json(self, path: Path):
        path.write_text(json.dumps(self.to_dict(), indent=2))

    @classmethod
    def load_json(cls, path: Path) -> "DependencyGraph":
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    # Graphviz dot export
    def to_dot(self, include_optional: bool = True) -> str:
        lines = ["digraph dependency_graph {", "  rankdir=LR;"]
        for n in self.nodes:
            label = n
            info = self.nodes.get(n) or {}
            v = info.get("version")
            if v:
                label = f"{n}\\n{v}"
            lines.append(f'  "{n}" [label="{label}"];')
        for pkg, deps in self.edges.items():
            for d in deps:
                if (not include_optional) and d.optional:
                    continue
                lbl = d.constraint or ""
                style = "dashed" if d.optional else "solid"
                lines.append(f'  "{pkg}" -> "{d.name}" [label="{lbl}", style="{style}"];')
        lines.append("}")
        return "\n".join(lines)


# --- Utilities for building graph from portfile dir --------------------------

def build_graph_from_portfiles(ports_root: Path, pattern: str = "**/portfile.yaml") -> DependencyGraph:
    """
    Walk ports_root and construct a dependency graph using fields in each portfile.
    Expected portfile keys: name, version, depends (list of strings or dicts {name, constraint, optional})
    """
    g = DependencyGraph()
    for pf in ports_root.glob(pattern):
        try:
            raw = json.loads(Path(pf).read_text()) if pf.suffix.lower() == ".json" else Path(pf).read_text(encoding="utf-8")
            try:
                # try YAML parse if available
                import yaml as _yaml  # type: ignore
                meta = _yaml.safe_load(raw)
            except Exception:
                # fallback to simple key: value parser? assume JSON-like already handled
                meta = {}
        except Exception:
            # try YAML directly
            try:
                import yaml as _yaml  # type: ignore
                meta = _yaml.safe_load(pf.read_text(encoding="utf-8"))
            except Exception:
                continue
        if not meta:
            continue
        name = meta.get("name") or pf.parent.name
        version = meta.get("version") or meta.get("pkgver")
        g.add_node(name, {"version": version, "portfile": str(pf)})
        deps = meta.get("depends") or []
        # normalise depends: strings or dicts
        for d in deps:
            if isinstance(d, str):
                # allow "pkgname" or "pkgname>=1.2"
                if any(op in d for op in [">=", "<=", "==", ">", "<"]):
                    # crude split: first token is name until comparator
                    import re
                    m = re.match(r"^([A-Za-z0-9_\-+.]+)\s*(.*)$", d)
                    if m:
                        pkgname = m.group(1)
                        constraint = m.group(2).strip()
                        g.add_dependency(name, pkgname, constraint, False)
                    else:
                        g.add_dependency(name, d, None, False)
                else:
                    g.add_dependency(name, d, None, False)
            elif isinstance(d, dict):
                depname = d.get("name")
                constraint = d.get("constraint") or d.get("version") or None
                optional = bool(d.get("optional", False))
                g.add_dependency(name, depname, constraint, optional)
    return g


# --- CLI for quick usage -----------------------------------------------------

def _cli():
    import argparse
    parser = argparse.ArgumentParser(prog="dependency.py", description="Dependency graph manager for PyPort")
    sub = parser.add_subparsers(dest="cmd")

    s_build = sub.add_parser("build", help="build graph from a ports directory")
    s_build.add_argument("ports_dir", nargs="?", default="/usr/ports")

    s_order = sub.add_parser("install-order", help="show install order for targets")
    s_order.add_argument("targets", nargs="*", help="target package names")
    s_order.add_argument("--graph-file", help="load graph JSON file")

    s_uninstall = sub.add_parser("uninstall-order", help="show uninstall order for targets")
    s_uninstall.add_argument("targets", nargs="+")
    s_cycles = sub.add_parser("cycles", help="detect cycles")
    s_cycles.add_argument("--graph-file", help="load graph JSON file")

    s_conf = sub.add_parser("conflicts", help="detect conflicts in constraints")
    s_conf.add_argument("--graph-file", help="load graph JSON file")

    s_dot = sub.add_parser("dot", help="export graph as DOT")
    s_dot.add_argument("--graph-file", help="load graph JSON file")
    s_dot.add_argument("--out", help="write to file")

    args = parser.parse_args()

    g: DependencyGraph
    if getattr(args, "graph_file", None):
        g = DependencyGraph.load_json(Path(args.graph_file))
    elif args.cmd == "build":
        g = build_graph_from_portfiles(Path(args.ports_dir))
        out = Path("dependency_graph.json")
        g.save_json(out)
        print("Saved graph to", out)
    else:
        # if not build and no file provided, try load default
        default = Path("dependency_graph.json")
        if default.exists():
            g = DependencyGraph.load_json(default)
        else:
            print("No graph file found. Run 'build' first or pass --graph-file")
            sys.exit(1)

    if args.cmd == "install-order":
        tars = args.targets or None
        try:
            order = g.install_order(targets=tars)
            print("Install order:")
            for o in order:
                info = g.get_node_info(o)
                ver = info.get("version")
                print(f"  {o}" + (f" ({ver})" if ver else ""))
        except CycleError as e:
            print("Cycle detected:", e)

    elif args.cmd == "uninstall-order":
        try:
            order = g.uninstall_order(args.targets)
            print("Uninstall order:")
            for o in order:
                print(" ", o)
        except Exception as e:
            print("Error:", e)

    elif args.cmd == "cycles":
        cyc = g._find_cycle()
        if cyc:
            print("Cycle:", " -> ".join(cyc))
        else:
            print("No cycles detected")

    elif args.cmd == "conflicts":
        cs = g.detect_conflicts()
        if not cs:
            print("No conflicts detected")
        else:
            for dep_name, reqs in cs:
                print(f"Conflict on {dep_name}:")
                for r in reqs:
                    print("  ", r)

    elif args.cmd == "dot":
        dot = g.to_dot()
        if args.out:
            Path(args.out).write_text(dot)
            print("Wrote DOT to", args.out)
        else:
            print(dot)

if __name__ == "__main__":
    _cli()
