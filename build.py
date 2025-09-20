#!/usr/bin/env python3
"""
build.py - PyPort build orchestrator (evoluído, completo e funcional)

Principais responsabilidades:
 - Resolver dependências com dependency.DependencyGraph (ordem topológica)
 - Para cada pacote (dependências + alvo): construir no sandbox com sandbox.build_port
 - Empacotar com packager.package_from_metadata
 - Atualizar DB de instalados (/pyport/db/installed.json) com arquivos, versão e pacote
 - Gerar logs detalhados por pacote em /pyport/logs/build-<pkg>.log
 - Oferecer opções: keep_build, dry_run, force, toolchain_dir, chroot
"""

from __future__ import annotations
import os
import sys
import json
import shutil
import argparse
import traceback
import time
from pathlib import Path
from typing import Dict, Any, List, Optional

# imports dos outros módulos do pyport (assume que estão no PYTHONPATH)
try:
    import yaml  # type: ignore
except Exception:
    yaml = None  # parser fallback handled below

try:
    import sandbox  # module with build_port(...)
except Exception:
    sandbox = None

try:
    import packager  # module with package_from_metadata(meta_path)
except Exception:
    packager = None

try:
    import dependency  # dependency.DependencyGraph
except Exception:
    dependency = None

# Paths/config
PORTFILES_ROOT = Path("/usr/ports")
DB_DIR = Path("/pyport/db")
LOG_DIR = Path("/pyport/logs")
PACKAGES_DIR = Path("/pyport/packages")

DB_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)
PACKAGES_DIR.mkdir(parents=True, exist_ok=True)

INSTALLED_DB = DB_DIR / "installed.json"


def _now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _log(pkg: str, message: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    out = LOG_DIR / f"build-{pkg}.log"
    ts = _now_ts()
    line = f"[{ts}] {message}\n"
    with open(out, "a", encoding="utf-8") as f:
        f.write(line)
    print(f"[{pkg}] {message}")


def load_installed_db() -> Dict[str, Any]:
    try:
        if INSTALLED_DB.exists():
            return json.loads(INSTALLED_DB.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {}


def save_installed_db(db: Dict[str, Any]) -> None:
    INSTALLED_DB.write_text(json.dumps(db, indent=2), encoding="utf-8")


def find_portfile_for(name: str) -> Optional[Path]:
    """
    Localiza o portfile.yaml para um pacote (procura por name no /usr/ports/**/portfile.yaml).
    """
    if PORTFILES_ROOT.exists():
        for pf in PORTFILES_ROOT.glob("**/portfile.yaml"):
            try:
                if yaml:
                    with open(pf, encoding="utf-8") as f:
                        meta = yaml.safe_load(f)
                else:
                    # fallback: crude check by reading file content
                    txt = pf.read_text(encoding="utf-8")
                    meta = {}
                    for line in txt.splitlines():
                        if line.strip().startswith("name:"):
                            meta["name"] = line.split(":", 1)[1].strip()
                            break
                if not meta:
                    continue
                if meta.get("name") == name or pf.parent.name == name:
                    return pf
            except Exception:
                continue
    return None


def build_dependency_graph() -> "dependency.DependencyGraph":
    """
    Constrói o grafo de dependências a partir dos portfiles.
    Usa dependency.build_graph_from_portfiles se disponível, senão tenta implementação mínima.
    """
    if dependency and hasattr(dependency, "build_graph_from_portfiles"):
        return dependency.build_graph_from_portfiles(Path(PORTFILES_ROOT))
    # fallback minimal graph
    class _G:
        def __init__(self):
            self._dg = {}
        def add_node(self, n, info=None):
            self._dg.setdefault(n, [])
        def add_dependency(self, pkg, dep, constraint=None, optional=False):
            self._dg.setdefault(pkg, []).append(dep)
        def install_order(self, targets=None, include_optional=True):
            # very naive: return targets only
            if targets is None:
                return list(self._dg.keys())
            return list(targets)
    return _G()  # type: ignore


def resolve_install_order(target: str) -> List[str]:
    """
    Usa dependency graph para obter ordem de instalação (dependências primeiro).
    Retorna lista ordenada com todos os pacotes que precisam ser construídos (inclui target).
    """
    dg = build_dependency_graph()
    try:
        # dependency.DependencyGraph.install_order accepts targets list
        order = dg.install_order(targets=[target])
        # install_order returns deps before dependents; ensure target is last
        if target not in order:
            order.append(target)
        return order
    except Exception as e:
        # try to fall back to topological on whole graph or at least target-only build
        _log("core", f"warning: install_order failed: {e}")
        return [target]


def is_installed(name: str, version: Optional[str] = None) -> bool:
    db = load_installed_db()
    if name not in db:
        return False
    if version:
        return db[name].get("version") == version
    return True


def register_installed(name: str, version: str, metadata_path: str, package_files: List[str]) -> None:
    db = load_installed_db()
    db[name] = {
        "version": version,
        "metadata": metadata_path,
        "package_files": package_files,
        "installed_at": _now_ts()
    }
    save_installed_db(db)


def build_one(name: str, options: Dict[str, Any]) -> Dict[str, Any]:
    """
    Constrói um único pacote invocando sandbox.build_port e empacotando com packager.
    Retorna dicionário com status, metadata path, package paths.
    """
    log_prefix = name
    _log(log_prefix, f"build_one start: options={options}")
    if sandbox is None:
        _log(log_prefix, "sandbox module not available")
        return {"status": "error", "message": "sandbox module not available"}
    if packager is None:
        _log(log_prefix, "packager module not available")
        return {"status": "error", "message": "packager module not available"}

    # locate portfile
    pf = find_portfile_for(name)
    if not pf:
        _log(log_prefix, f"portfile for {name} not found")
        return {"status": "error", "message": f"portfile for {name} not found"}

    cfg_options = {
        "keep_build": options.get("keep_build", False),
        "dry_run": options.get("dry_run", False),
        "debug": options.get("debug", False),
        "toolchain_dir": options.get("toolchain_dir"),
        "chroot": options.get("chroot", False),
    }

    try:
        # call sandbox.build_port (expected to return metadata path)
        _log(log_prefix, f"invoking sandbox.build_port on {pf}")
        res = sandbox.build_port(pf, Path("/pyport/build"), keep_build=cfg_options["keep_build"])
    except TypeError:
        # some versions of sandbox.build_port may expect different signature: sandbox.build_port(target, options)
        try:
            res = sandbox.build_port(str(pf.parent), options=cfg_options)  # best-effort
        except Exception as e:
            _log(log_prefix, f"sandbox.build_port invocation failed: {e}")
            return {"status": "error", "message": f"sandbox.build_port failed: {e}"}
    except Exception as e:
        _log(log_prefix, f"sandbox.build_port failed: {e}")
        _log(log_prefix, traceback.format_exc())
        return {"status": "error", "message": f"sandbox.build_port failed: {e}"}

    if res.get("status") != "ok":
        _log(log_prefix, f"sandbox build failed: {res}")
        return {"status": "error", "message": "sandbox build failed", "detail": res}

    meta_path = Path(res.get("metadata"))
    if not meta_path.exists():
        _log(log_prefix, f"metadata json not found at {meta_path}")
        return {"status": "error", "message": "metadata.json missing", "detail": str(meta_path)}

    # package
    try:
        _log(log_prefix, f"packaging from metadata {meta_path}")
        # packager.package_from_metadata expects path object or string
        pkgs = packager.package_from_metadata(meta_path)
    except Exception as e:
        _log(log_prefix, f"packager failed: {e}")
        _log(log_prefix, traceback.format_exc())
        return {"status": "error", "message": f"packager failed: {e}"}

    package_paths: List[str] = []
    for k, v in (pkgs or {}).items():
        if v:
            package_paths.append(str(v))

    # register installed info (metadata may include name, version)
    try:
        md = json.loads(meta_path.read_text(encoding="utf-8"))
    except Exception:
        md = {}
    pkg_name = md.get("name", name)
    pkg_ver = md.get("version", md.get("pkgver", "0.0.0"))

    register_installed(pkg_name, pkg_ver, str(meta_path), package_paths)
    _log(log_prefix, f"build_one done: packages={package_paths}")

    return {"status": "ok", "metadata": str(meta_path), "packages": package_paths}


def build(target: str, options: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    Fluxo de build para target:
     - Resolve ordem de instalação
     - Para cada pacote na ordem: se não instalado (ou force) -> build_one
    """
    options = options or {}
    force = options.get("force", False)
    keep_build = options.get("keep_build", False)
    dry_run = options.get("dry_run", False)
    debug = options.get("debug", False)

    _log("core", f"build start target={target} force={force} keep_build={keep_build} dry_run={dry_run}")

    # Resolve install order (deps first)
    try:
        order = resolve_install_order(target)
    except Exception as e:
        _log("core", f"failed to resolve order: {e}")
        order = [target]

    built: List[str] = []
    failed: Dict[str, str] = {}

    for pkg in order:
        if not force and is_installed(pkg):
            _log("core", f"skipping {pkg} (already installed)")
            continue
        _log("core", f"building {pkg} ...")
        try:
            res = build_one(pkg, options)
            if res.get("status") == "ok":
                built.append(pkg)
            else:
                failed[pkg] = res.get("message") or "unknown"
                _log("core", f"build failed for {pkg}: {res}")
                # stop chain on failure (safe default)
                break
        except Exception as e:
            failed[pkg] = str(e)
            _log("core", f"exception building {pkg}: {e}")
            _log("core", traceback.format_exc())
            break

    result = {"status": "ok" if not failed else "error", "built": built, "failed": failed}
    _log("core", f"build result: {result}")
    return result


def cli_main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="pyport-build", description="PyPort build orchestrator")
    parser.add_argument("target", help="package name (category/name or name)")
    parser.add_argument("--keep-build", action="store_true", help="preserve build directories")
    parser.add_argument("--dry-run", action="store_true", help="dry run (no changes)")
    parser.add_argument("--force", action="store_true", help="force rebuild even if installed")
    parser.add_argument("--toolchain-dir", type=str, help="toolchain directory to make available in sandbox")
    parser.add_argument("--chroot", action="store_true", help="prepare chroot (bind mounts) for build")
    parser.add_argument("--debug", action="store_true", help="verbose debug")
    args = parser.parse_args(argv)

    options = {
        "keep_build": args.keep_build,
        "dry_run": args.dry_run,
        "force": args.force,
        "toolchain_dir": args.toolchain_dir,
        "chroot": args.chroot,
        "debug": args.debug,
    }

    try:
        out = build(args.target, options=options)
        print(json.dumps(out, indent=2))
        return 0 if out.get("status") == "ok" else 2
    except KeyboardInterrupt:
        print("cancelled by user")
        return 130
    except Exception as e:
        print("unexpected error:", e)
        _log("core", f"unexpected error: {e}")
        _log("core", traceback.format_exc())
        return 1


if __name__ == "__main__":
    sys.exit(cli_main())
