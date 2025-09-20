#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
dependency.py - Advanced dependency manager for PyPort

Improvements included:
 - Robust version handling using packaging.version when available
 - Constraint parsing (==, !=, >=, <=, >, <) and simple satisfiability checks
 - Nodes support metadata: version(s), provides, conflicts, replaces, source, port_path
 - Topological sorting with cycle detection and explicit cycle paths
 - Reverse dependency queries (immediate and recursive)
 - Resolve function that attempts to produce an install order honoring constraints
 - Load ports tree (Portfile.yaml) scanning for common dependency keys
 - Atomic persistence with simple file locking to /pyport/db/deps.json
 - Export to DOT and JSON
 - CLI with multiple useful commands
"""

from __future__ import annotations
import os
import json
import time
import tempfile
import shutil
import fcntl
from pathlib import Path
from typing import Dict, List, Tuple, Optional, Set, Any, Iterable
from collections import defaultdict, deque

# Logging: prefer pyport.logger if available
try:
    from pyport.logger import get_logger
    LOG = get_logger("pyport.dependency")
except Exception:
    import logging
    logging.basicConfig(level=logging.INFO)
    LOG = logging.getLogger("pyport.dependency")

# Config: try to get ports path, db path
try:
    from pyport.config import get_config
    _CFG = get_config()
    PORTS_ROOT_DEFAULT = _CFG.get("paths", {}).get("ports", "/usr/ports")
    DB_DIR = Path(_CFG.get("paths", {}).get("db", "/pyport/db"))
except Exception:
    PORTS_ROOT_DEFAULT = "/usr/ports"
    DB_DIR = Path("/pyport/db")

DB_DIR.mkdir(parents=True, exist_ok=True)
PERSIST_FILE = DB_DIR / "deps.json"
LOCK_FILE = DB_DIR / "deps.lock"

# YAML parser if available
try:
    import yaml
except Exception:
    yaml = None

# Prefer packaging.version for robust version comparison
try:
    from packaging.version import Version as PV, InvalidVersion
    def _parse_ver(s):
        try:
            return PV(str(s))
        except InvalidVersion:
            return None
except Exception:
    PV = None
    def _parse_ver(s):
        # fallback: convert numeric-dot separated strings to tuple
        if s is None:
            return None
        try:
            parts = []
            for p in str(s).split("."):
                if p.isdigit():
                    parts.append(int(p))
                else:
                    parts.append(p)
            return tuple(parts)
        except Exception:
            return str(s)

# ----------------- Utility: constraint parsing & checking -----------------

def parse_requirement(req: Optional[str]) -> Optional[Tuple[str, str]]:
    """
    Parse simple requirement strings:
      ">=1.2.3" -> (">=", "1.2.3")
      "1.2.3"   -> ("==", "1.2.3")
      None -> None
    """
    if req is None:
        return None
    s = str(req).strip()
    for op in (">=", "<=", "==", "!=", ">", "<"):
        if s.startswith(op):
            return (op, s[len(op):].strip())
    # no operator => equality
    return ("==", s)

def satisfies(candidate_version: Optional[str], requirement: Optional[str]) -> bool:
    """
    Check whether candidate_version satisfies requirement.
    candidate_version and requirement are strings or None.
    """
    if requirement is None:
        return True
    if candidate_version is None:
        return False
    r = parse_requirement(requirement)
    if r is None:
        return True
    op, rval = r
    # attempt parsing
    pv_c = _parse_ver(candidate_version)
    pv_r = _parse_ver(rval)
    if pv_c is None or pv_r is None:
        # fallback: string compare if not parseable
        if op == "==":
            return str(candidate_version) == rval
        if op == "!=":
            return str(candidate_version) != rval
        # other ops: be conservative -> False
        return False
    # if using packaging.Version, comparisons are natural
    try:
        if op == "==":
            return pv_c == pv_r
        if op == "!=":
            return pv_c != pv_r
        if op == ">":
            return pv_c > pv_r
        if op == "<":
            return pv_c < pv_r
        if op == ">=":
            return pv_c >= pv_r
        if op == "<=":
            return pv_c <= pv_r
    except Exception:
        return False
    return False

# ----------------- Core DependencyGraph -----------------

class DependencyGraph:
    """
    Directed dependency graph.
    adj: pkg -> list of (dep_pkg, requirement_str or None)
    meta: pkg -> metadata dict (version, provides, conflicts, replaces, source, port_path, available_versions)
    """

    def __init__(self, persist: bool = True):
        self.adj: Dict[str, List[Tuple[str, Optional[str]]]] = defaultdict(list)
        self.meta: Dict[str, Dict[str, Any]] = {}
        self.persist = persist
        # reverse cache
        self._reverse_cache: Optional[Dict[str, Set[str]]] = None
        if persist:
            self._load()

    # --------------- persistence with simple file lock ----------------
    def _acquire_lock(self, blocking: bool = True):
        # lock with fcntl on LOCK_FILE
        lf = open(str(LOCK_FILE), "a+")
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            lf.close()
            raise
        return lf

    def _release_lock(self, lf):
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
        finally:
            lf.close()

    def _load(self):
        if not PERSIST_FILE.exists():
            return
        lf = None
        try:
            lf = self._acquire_lock()
            text = PERSIST_FILE.read_text(encoding="utf-8")
            j = json.loads(text)
            self.adj = defaultdict(list, {k: [(d, req) for d, req in v] for k, v in j.get("adj", {}).items()})
            self.meta = j.get("meta", {})
            self._reverse_cache = None
            LOG.debug("DependencyGraph loaded from persist")
        except Exception as e:
            LOG.warning(f"Failed to load deps persistence: {e}")
        finally:
            if lf:
                self._release_lock(lf)

    def _save(self):
        # atomic write with tmpfile, under file lock
        lf = None
        try:
            lf = self._acquire_lock()
            tmp = Path(str(PERSIST_FILE) + ".tmp")
            data = {"adj": {k: v for k, v in self.adj.items()}, "meta": self.meta, "saved_at": time.time()}
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(PERSIST_FILE)
            LOG.debug("DependencyGraph persisted atomically")
        except Exception as e:
            LOG.warning(f"Failed to persist deps: {e}")
        finally:
            if lf:
                self._release_lock(lf)

    # --------------- graph modifications ----------------
    def add_node(self, name: str, version: Optional[str] = None, provides: Optional[List[str]] = None,
                 conflicts: Optional[List[str]] = None, replaces: Optional[List[str]] = None,
                 source: Optional[str] = None, port_path: Optional[str] = None, available_versions: Optional[List[str]] = None):
        if name not in self.adj:
            self.adj[name] = []
        m = self.meta.setdefault(name, {})
        if version is not None:
            m["version"] = version
        if provides:
            m.setdefault("provides", []).extend(x for x in provides if x not in m.get("provides", []))
        if conflicts:
            m.setdefault("conflicts", []).extend(x for x in conflicts if x not in m.get("conflicts", []))
        if replaces:
            m.setdefault("replaces", []).extend(x for x in replaces if x not in m.get("replaces", []))
        if source:
            m["source"] = source
        if port_path:
            m["port_path"] = port_path
        if available_versions:
            m["available_versions"] = available_versions
        self._reverse_cache = None
        if self.persist:
            self._save()

    def add_edge(self, pkg: str, dep: str, requirement: Optional[str] = None):
        self.add_node(pkg)
        self.add_node(dep)
        # avoid exact duplicates
        for (d, req) in self.adj[pkg]:
            if d == dep and req == requirement:
                return
        self.adj[pkg].append((dep, requirement))
        self._reverse_cache = None
        if self.persist:
            self._save()

    def remove_node(self, name: str):
        if name in self.adj:
            del self.adj[name]
        for k in list(self.adj.keys()):
            self.adj[k] = [(d, r) for (d, r) in self.adj[k] if d != name]
        if name in self.meta:
            del self.meta[name]
        self._reverse_cache = None
        if self.persist:
            self._save()

    def remove_edge(self, pkg: str, dep: str):
        if pkg in self.adj:
            self.adj[pkg] = [(d, r) for (d, r) in self.adj[pkg] if d != dep]
        self._reverse_cache = None
        if self.persist:
            self._save()

    # --------------- queries ----------------
    def nodes(self) -> List[str]:
        return list(self.adj.keys())

    def edges(self) -> List[Tuple[str, str, Optional[str]]]:
        out = []
        for n, lst in self.adj.items():
            for (d, r) in lst:
                out.append((n, d, r))
        return out

    # reverse dependencies (immediate or recursive)
    def _build_reverse_cache(self):
        r = defaultdict(set)
        for src, deps in self.adj.items():
            for dst, _ in deps:
                r[dst].add(src)
        self._reverse_cache = r

    def reverse_dependencies(self, name: str, recursive: bool = True) -> List[str]:
        if self._reverse_cache is None:
            self._build_reverse_cache()
        out = set()
        q = deque([name])
        while q:
            cur = q.popleft()
            for p in self._reverse_cache.get(cur, set()):
                if p not in out:
                    out.add(p)
                    if recursive:
                        q.append(p)
        return sorted(out)

    # --------------- topological sort + cycle detection ----------------
    def topological_sort(self, subset: Optional[Iterable[str]] = None) -> Dict[str, Any]:
        nodes = set(subset) if subset is not None else set(self.adj.keys())
        indeg = {n: 0 for n in nodes}
        missing = set()
        for n in nodes:
            for (dst, _) in self.adj.get(n, []):
                if dst not in nodes:
                    missing.add(dst)
                    continue
                indeg[dst] = indeg.get(dst, 0) + 1
        q = deque([n for n, d in indeg.items() if d == 0])
        order = []
        while q:
            n = q.popleft()
            order.append(n)
            for (dst, _) in self.adj.get(n, []):
                if dst not in nodes:
                    continue
                indeg[dst] -= 1
                if indeg[dst] == 0:
                    q.append(dst)
        if len(order) != len(nodes):
            cycles = self._find_cycles(nodes)
            return {"order": order, "cycles": cycles, "missing": sorted(list(missing))}
        return {"order": order, "cycles": [], "missing": sorted(list(missing))}

    def _find_cycles(self, nodes: Set[str]) -> List[List[str]]:
        visited = set()
        onstack = set()
        stack = []
        cycles = []

        def dfs(u):
            visited.add(u)
            onstack.add(u)
            stack.append(u)
            for (v, _) in self.adj.get(u, []):
                if v not in nodes:
                    continue
                if v not in visited:
                    dfs(v)
                elif v in onstack:
                    # extract cycle
                    try:
                        idx = stack.index(v)
                        cycles.append(stack[idx:].copy())
                    except ValueError:
                        pass
            stack.pop()
            onstack.remove(u)

        for n in nodes:
            if n not in visited:
                dfs(n)
        return cycles

    # --------------- conflict detection ----------------
    def detect_conflicts(self, subset: Optional[Iterable[str]] = None) -> List[Dict[str, Any]]:
        nodes = set(subset) if subset is not None else set(self.adj.keys())
        conflicts = []
        for n in nodes:
            # collect requirements on n from parents inside nodes
            reqs = []
            for parent in nodes:
                for (dst, req) in self.adj.get(parent, []):
                    if dst == n:
                        reqs.append((parent, req))
            # if multiple different requirements that cannot be satisfied simultaneously, record conflict
            for i in range(len(reqs)):
                for j in range(i + 1, len(reqs)):
                    r1 = reqs[i][1]; r2 = reqs[j][1]
                    if r1 and r2 and r1 != r2:
                        conflicts.append({"node": n, "requirements": [reqs[i], reqs[j]]})
        return conflicts

    # --------------- resolution (attempt to choose versions and order) ----------------
    def resolve(self, root: str, include_optional: bool = False) -> Dict[str, Any]:
        """
        Attempt to resolve dependencies for 'root' and return installation order.
        Returns dict:
          - ok: bool
          - order: list of packages in install order (root's dependencies first)
          - missing: referenced but absent packages
          - cycles: list of cycles if any
          - conflicts: list of conflicts if any
          - details: deeper diagnostics
        """
        if root not in self.adj:
            return {"ok": False, "message": f"root '{root}' not in graph", "order": [], "missing": [], "cycles": [], "conflicts": []}

        # gather reachable nodes
        visited = set()
        stack = [root]
        while stack:
            n = stack.pop()
            if n in visited:
                continue
            visited.add(n)
            for (dep, _) in self.adj.get(n, []):
                stack.append(dep)

        # detect cycles
        topo = self.topological_sort(subset=visited)
        cycles = topo.get("cycles", [])
        missing = topo.get("missing", [])
        if cycles:
            return {"ok": False, "message": "cycles detected", "order": topo.get("order", []), "missing": missing, "cycles": cycles, "conflicts": []}

        # detect conflicts
        conflicts = self.detect_conflicts(subset=visited)
        if conflicts:
            return {"ok": False, "message": "conflicts detected", "order": topo.get("order", []), "missing": missing, "cycles": [], "conflicts": conflicts}

        # If here, we can produce order: topo.order but we want dependencies first => reverse
        order = topo.get("order", [])
        # topological_sort returns nodes where edges point to dependencies (pkg -> dep). The order from Kahn puts packages before deps? We used indegree count as edges into dst -> so order yields nodes with zero indegree first: roots before deps. We want install deps before dependents: reverse order.
        install_order = list(reversed(order))
        return {"ok": True, "order": install_order, "missing": missing, "cycles": [], "conflicts": []}

    # --------------- ports tree import ----------------
    def _parse_dep_item(self, dep) -> Tuple[Optional[str], Optional[str]]:
        """
        Parse dependency item from portfile:
         - dict form: {"name": "pkg", "version": ">=1.2"}
         - string form: "pkg>=1.2" or "pkg"
        Returns (depname, requirement)
        """
        if isinstance(dep, dict):
            name = dep.get("name") or dep.get("pkg") or dep.get("package")
            req = dep.get("version") or dep.get("requirement")
            return (name, req)
        s = str(dep)
        for op in (">=", "<=", "==", "!=", ">", "<"):
            if op in s:
                parts = s.split(op, 1)
                return (parts[0].strip(), op + parts[1].strip())
        return (s.strip(), None)

    def load_ports_tree(self, ports_root: Optional[str] = None, pattern: str = "Portfile.yaml"):
        """
        Scan ports tree and load node metadata and edges.
        Supports common keys: name, version, depends, depends_build, depends_run, depends_lib, provides, conflicts, replaces
        """
        root = Path(ports_root or PORTS_ROOT_DEFAULT)
        if not root.exists():
            LOG.warning(f"ports root not found: {root}")
            return

        if yaml is None:
            LOG.warning("pyyaml not available: cannot parse Portfile.yaml")
            return

        count = 0
        for pf in root.rglob(pattern):
            try:
                data = yaml.safe_load(pf.read_text(encoding="utf-8")) or {}
                name = data.get("name") or data.get("pkgname") or pf.parent.name
                version = data.get("version") or data.get("pkgver")
                provides = data.get("provides") or data.get("provides_list") or []
                conflicts = data.get("conflicts") or []
                replaces = data.get("replaces") or []
                self.add_node(name, version=version, provides=provides, conflicts=conflicts,
                              replaces=replaces, source=data.get("homepage"), port_path=str(pf.parent))
                # collect dependency lists
                for key in ("depends", "depends_build", "depends_run", "depends_lib", "depends_pkg"):
                    deps = data.get(key, []) or []
                    if isinstance(deps, str):
                        deps = [deps]
                    for dep in deps:
                        depname, req = self._parse_dep_item(dep)
                        if depname:
                            self.add_edge(name, depname, requirement=req)
                count += 1
            except Exception as e:
                LOG.debug(f"failed parsing {pf}: {e}")
        LOG.info(f"Loaded {count} portfiles into dependency graph")
        if self.persist:
            self._save()

    # --------------- export ----------------
    def export_dot(self, out_file: Path, subset: Optional[Iterable[str]] = None):
        nodes = set(subset) if subset is not None else set(self.adj.keys())
        lines = ["digraph pyport_deps {"]
        for n in sorted(nodes):
            label = n
            ver = self.meta.get(n, {}).get("version")
            if ver:
                lines.append(f'  "{n}" [label="{label}\\n{ver}"];')
            else:
                lines.append(f'  "{n}";')
        for n in sorted(nodes):
            for (dst, req) in self.adj.get(n, []):
                if dst not in nodes:
                    continue
                attr = f' [label="{req}"]' if req else ""
                lines.append(f'  "{n}" -> "{dst}"{attr};')
        lines.append("}")
        out_file.write_text("\n".join(lines), encoding="utf-8")
        LOG.info(f"DOT written to {out_file}")

    def export_json(self, out_file: Path):
        data = {"adj": {k: v for k, v in self.adj.items()}, "meta": self.meta}
        out_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
        LOG.info(f"JSON export written to {out_file}")

    # --------------- utility & diagnostics ----------------
    def find_providers(self, virtual_name: str) -> List[str]:
        """
        Return list of nodes that provide virtual_name (search 'provides' metadata).
        """
        out = []
        for n, meta in self.meta.items():
            prov = meta.get("provides", [])
            if virtual_name in prov:
                out.append(n)
        return out

    def find_best_version(self, pkg: str, requirement: Optional[str]) -> Optional[str]:
        """
        Given pkg and requirement (string like '>=1.2'), pick best available version
        from meta[pkg].get('available_versions') or meta[pkg].get('version').
        Returns selected version string or None if not satisfiable.
        """
        meta = self.meta.get(pkg, {})
        candidates = []
        if "available_versions" in meta:
            candidates = list(meta["available_versions"])
        elif "version" in meta:
            candidates = [meta["version"]]
        if not candidates:
            return None
        # sort candidates descending and pick first that satisfies requirement
        try:
            # parse and sort using PV if available
            if PV:
                parsed = [(PV(v), v) for v in candidates]
                parsed.sort(reverse=True)
                for pv, v in parsed:
                    if satisfies(v, requirement):
                        return v
            else:
                # fallback: try numeric tuple parse
                def keyf(s):
                    p = _parse_ver(s)
                    return p
                candidates.sort(key=keyf, reverse=True)
                for v in candidates:
                    if satisfies(v, requirement):
                        return v
        except Exception:
            # last-resort linear scan
            for v in candidates:
                if satisfies(v, requirement):
                    return v
        return None

    # --------------- self-test (basic sanity) ----------------
    def self_test(self) -> bool:
        # simple tests: add nodes, edges, detect cycle, topo order
        try:
            g = DependencyGraph(persist=False)
            g.add_node("A"); g.add_node("B"); g.add_node("C")
            g.add_edge("A", "B"); g.add_edge("B", "C")
            res = g.topological_sort()
            if not res or "cycles" in res and res["cycles"]:
                LOG.error("self_test topo failed: unexpected cycle")
                return False
            # create cycle
            g.add_edge("C", "A")
            res2 = g.topological_sort()
            if not res2.get("cycles"):
                LOG.error("self_test failed to detect cycle")
                return False
            LOG.info("dependency.self_test passed")
            return True
        except Exception as e:
            LOG.error(f"self_test error: {e}")
            return False

# ----------------- CLI -----------------

def _cli():
    import argparse
    parser = argparse.ArgumentParser(prog="pyport-deps", description="Advanced dependency manager for PyPort")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_res = sub.add_parser("resolve", help="Resolve dependencies for a package")
    p_res.add_argument("pkg")

    p_rev = sub.add_parser("reverse", help="Show reverse dependencies")
    p_rev.add_argument("pkg")

    p_top = sub.add_parser("topo", help="Topological sort (global or subset)")
    p_top.add_argument("--subset", nargs="*", help="Nodes subset (optional)")

    p_add = sub.add_parser("add-port", help="Add node with metadata")
    p_add.add_argument("name")
    p_add.add_argument("--version")
    p_add.add_argument("--provides", nargs="*", default=[])
    p_add.add_argument("--available-versions", nargs="*", default=[])
    p_add.add_argument("--source")
    p_add.add_argument("--port-path")

    p_edge = sub.add_parser("add-edge", help="Add dependency edge: A depends_on B")
    p_edge.add_argument("pkg")
    p_edge.add_argument("dep")
    p_edge.add_argument("--req", help="requirement string like >=1.2")

    p_load = sub.add_parser("load-ports", help="Load ports tree into graph")
    p_load.add_argument("--root", help="Ports root folder (default from config)")

    p_vis = sub.add_parser("visualize", help="Export DOT file")
    p_vis.add_argument("out")

    p_export = sub.add_parser("export-json", help="Export graph to JSON file")
    p_export.add_argument("out")

    p_test = sub.add_parser("self-test", help="Run internal self-tests")

    args = parser.parse_args()
    dg = DependencyGraph(persist=True)

    if args.cmd == "resolve":
        res = dg.resolve(args.pkg)
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif args.cmd == "reverse":
        r = dg.reverse_dependencies(args.pkg)
        print(json.dumps(r, indent=2, ensure_ascii=False))
    elif args.cmd == "topo":
        subset = args.subset if args.subset else None
        res = dg.topological_sort(subset)
        print(json.dumps(res, indent=2, ensure_ascii=False))
    elif args.cmd == "add-port":
        dg.add_node(args.name, version=args.version, provides=args.provides,
                    available_versions=args.available_versions, source=args.source, port_path=args.port_path)
        print("ok")
    elif args.cmd == "add-edge":
        dg.add_edge(args.pkg, args.dep, requirement=args.req)
        print("ok")
    elif args.cmd == "load-ports":
        dg.load_ports_tree(ports_root=args.root)
        print("ok")
    elif args.cmd == "visualize":
        dg.export_dot(Path(args.out))
        print("ok")
    elif args.cmd == "export-json":
        dg.export_json(Path(args.out))
        print("ok")
    elif args.cmd == "self-test":
        ok = dg.self_test()
        print("self-test ok" if ok else "self-test failed")
        exit(0 if ok else 2)

if __name__ == "__main__":
    _cli()
