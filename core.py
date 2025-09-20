# core.py - Core API for PyPort (initial implementation)
# Provides basic stubs and utility functions used by the CLI.
# This module is intentionally conservative: it performs file-system
# operations and prints status messages, but does not execute network
# operations without explicit tools available (git/curl). It is suitable
# to run on a developer machine and to be extended iteratively.
#
# NOTE: This is a starter implementation. It focuses on the public API:
#   install(), remove(), upgrade(), search(), info(), sync(), update(),
#   toolchain_*(), get_config()
#
# The implementation tries to be robust if run on systems that don't have
# /usr/ports present (it will simply report nothing found). It will create
# standard directories under /pyport when possible.

from __future__ import annotations
import os
import sys
import subprocess
import shutil
import glob
import sqlite3
import datetime
from pathlib import Path
from typing import Dict, Any, List, Optional

# Try to use YAML if available; else fall back to a tiny parser
try:
    import yaml  # type: ignore
    _yaml_load = lambda s: yaml.safe_load(s)
except Exception:
    def _yaml_load(s: str) -> Dict[str, Any]:
        # VERY small YAML-lite loader supporting simple "key: value" and lists "- item"
        result: Dict[str, Any] = {}
        current_key: Optional[str] = None
        for raw in s.splitlines():
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('- '):
                if current_key:
                    result.setdefault(current_key, []).append(line[2:].strip())
            elif ':' in line:
                k, v = line.split(':', 1)
                k = k.strip()
                v = v.strip()
                current_key = k
                if v == '':
                    # start of a list or nested mapping; represent as empty list for now
                    result[k] = []
                else:
                    # try to convert to int/bool
                    val: Any = v
                    if v.lower() in ('true', 'false'):
                        val = (v.lower() == 'true')
                    else:
                        try:
                            val = int(v)
                        except Exception:
                            pass
                    result[k] = val
            else:
                # unknown line, ignore
                continue
        return result

# Defaults and paths
DEFAULT_CONFIG = {
    "sandbox_mode": "both",  # fakeroot | bwrap | both
    "build_root": "/pyport/build",
    "log_dir": "/pyport/logs",
    "packagedir": "/pyport/packages",
    "dbpath": "/var/lib/pyport/db.sqlite",
    "ports_dir": "/usr/ports",
    "notify": True,
    "auto_update_ports": False,
    "patch_dir": "patches",
    "toolchain_dir": "/mnt/tools"
}

# Ensure basic directories exist
def _ensure_dirs(cfg: Dict[str, Any]) -> None:
    for d in (cfg.get("build_root"), cfg.get("log_dir"), cfg.get("packagedir")):
        if not d:
            continue
        try:
            os.makedirs(d, exist_ok=True)
        except Exception:
            pass
    # ensure db dir
    dbp = Path(cfg.get("dbpath"))
    if dbp:
        try:
            dbp.parent.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

# Simple logging helper
def _log(cfg: Dict[str, Any], name: str, message: str) -> None:
    logdir = cfg.get("log_dir") or DEFAULT_CONFIG["log_dir"]
    os.makedirs(logdir, exist_ok=True)
    ts = datetime.datetime.now().isoformat()
    fname = os.path.join(logdir, f"{name}.log")
    with open(fname, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")

# Load configuration (merge system and user)
def get_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    # system config
    syscfg = "/etc/pyport/config.yaml"
    usercfg = os.path.expanduser("~/.config/pyport/config.yaml")
    for path in (syscfg, usercfg):
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = _yaml_load(f.read())
                    if isinstance(data, dict):
                        cfg.update(data)
        except Exception:
            # ignore malformed config for now
            continue
    _ensure_dirs(cfg)
    return cfg

# Minimal parser for a portfile.yaml (returns dict with at least name/version/category/source)
def _parse_portfile(path: Path) -> Dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(str(path))
    content = path.read_text(encoding="utf-8")
    try:
        data = _yaml_load(content) or {}
    except Exception:
        data = {}
    # normalize fields
    if "name" not in data:
        data["name"] = path.parent.name
    if "version" not in data:
        data["version"] = data.get("pkgver") or "0.0.0"
    if "category" not in data:
        # infer category from parent of parent if possible
        p = path.parent.parent
        if p and p.name:
            data["category"] = p.name
        else:
            data["category"] = "unknown"
    return data

# Find a portfile given a target string (category/name or name)
def _find_portfile(target: str, cfg: Dict[str, Any]) -> Optional[Path]:
    portsdir = Path(cfg.get("ports_dir", DEFAULT_CONFIG["ports_dir"]))
    target = target.strip()
    if "/" in target:
        candidate = portsdir / target / "portfile.yaml"
        if candidate.exists():
            return candidate
        return None
    else:
        pattern = str(portsdir / "**" / target / "portfile.yaml")
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return Path(matches[0])
        return None

# --- Public API stubs ---
def install(target: str, options: Dict[str, Any] | None = None) -> None:
    cfg = get_config()
    pf = _find_portfile(target, cfg)
    if not pf:
        print(f"Portfile for '{target}' not found under {cfg.get('ports_dir')}")
        return
    meta = _parse_portfile(pf)
    name, version = meta.get("name"), meta.get("version")
    build_root = Path(cfg["build_root"]) / meta.get("category", "misc") / name / str(version)
    sandbox_dir = build_root / "sandbox"
    print(f"[pyport] Installing {name}-{version} (portfile: {pf})")
    build_root.mkdir(parents=True, exist_ok=True)
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    _log(cfg, f"{name}-{version}", "Install started")
    print(f"[pyport] (stub) fetching sources -> {build_root}")
    print(f"[pyport] (stub) extracting sources")
    print(f"[pyport] (stub) building with {meta.get('build_system','custom')}")
    print(f"[pyport] (stub) installing into sandbox {sandbox_dir}")
    _log(cfg, f"{name}-{version}", "Install complete")

def remove(targets: List[str] | str, options: Dict[str, Any] | None = None) -> None:
    cfg = get_config()
    if isinstance(targets, str):
        targets = [targets]
    for t in targets:
        print(f"[pyport] Removing {t} (stub)")
        _log(cfg, f"remove-{t}", "Remove requested")

def upgrade(targets: List[str] | None = None, options: Dict[str, Any] | None = None) -> None:
    cfg = get_config()
    print(f"[pyport] Upgrade requested for: {targets or 'all'} (stub)")
    _log(cfg, "upgrade", f"Upgrade called")

def search(query: str, options: Dict[str, Any] | None = None) -> None:
    cfg = get_config()
    portsdir = Path(cfg.get("ports_dir"))
    print(f"[pyport] Searching for '{query}' in {portsdir} ...")
    pattern = str(portsdir / "**" / "portfile.yaml")
    matches = glob.glob(pattern, recursive=True)
    for p in matches:
        meta = _parse_portfile(Path(p))
        if query.lower() in meta.get("name","").lower():
            print(f"{meta.get('category')}/{meta.get('name')} - {meta.get('version')} ({p})")

def info(target: str, options: Dict[str, Any] | None = None) -> None:
    cfg = get_config()
    pf = _find_portfile(target, cfg)
    if not pf:
        print(f"Port '{target}' not found.")
        return
    meta = _parse_portfile(pf)
    print("---- Port Information ----")
    for k, v in meta.items():
        print(f"{k}: {v}")
    print("--------------------------")

def sync(options: Dict[str, Any] | None = None) -> None:
    cfg = get_config()
    portsdir = Path(cfg.get("ports_dir"))
    if (portsdir / ".git").exists():
        print(f"[pyport] Running 'git pull' in {portsdir}")
        subprocess.run(["git", "-C", str(portsdir), "pull"])

def update(check_only: bool = True, options: Dict[str, Any] | None = None) -> None:
    cfg = get_config()
    print("[pyport] Checking for updates (stub)")

# --- Toolchain helpers ---
def toolchain_init() -> None:
    cfg = get_config()
    tc = Path(cfg.get("toolchain_dir"))
    for sub in ("bin", "lib", "include", "share"):
        (tc / sub).mkdir(parents=True, exist_ok=True)
    print(f"[pyport] Toolchain initialized at {tc}")

def toolchain_chroot() -> None:
    cfg = get_config()
    tc = Path(cfg.get("toolchain_dir"))
    (tc / "etc").mkdir(parents=True, exist_ok=True)
    shutil.copy("/etc/resolv.conf", tc / "etc/resolv.conf")
    for src in ["/dev","/proc","/sys","/run"]:
        subprocess.run(["mount","--bind",src,str(tc/Path(src).name)],check=False)
    print("[pyport] chroot ready. Use 'pyport toolchain enter'")

def toolchain_enter() -> None:
    cfg = get_config()
    tc = Path(cfg.get("toolchain_dir"))
    os.execvp("chroot", ["chroot", str(tc), "/bin/bash"])

def toolchain_leave() -> None:
    cfg = get_config()
    tc = Path(cfg.get("toolchain_dir"))
    for sub in ["run","sys","proc","dev"]:
        subprocess.run(["umount", str(tc/sub)], check=False)
    print("[pyport] chroot unmounted")
