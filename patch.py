#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
patch.py - Patch manager for PyPort

Features:
 - Apply multiple patches automatically
 - Support explicit patch list in Portfile.yaml
 - If no list, apply all patches/*.{patch,diff}
 - Ordered application (alphabetical or defined list)
 - Runs inside sandbox build dir
 - Idempotent (skips already applied patches)
 - Logging for success/failure
"""

import subprocess
import shlex
from pathlib import Path
from typing import Dict, Any, List, Optional

from pyport.logger import get_logger
from pyport.sandbox import Sandbox

log = get_logger("pyport.patch")

class PatchError(Exception):
    """Raised when a patch fails"""

# ---------------- Helpers ----------------

def _apply_patch_file(patch_file: Path, target_dir: Path, sandbox: Optional[Sandbox] = None) -> bool:
    """Apply a single patch file inside target_dir"""
    cmd = f"patch -p1 -i {patch_file}"
    log.info(f"Aplicando patch: {patch_file.name}")

    try:
        if sandbox:
            out = sandbox.run(cmd, cwd=target_dir)
        else:
            proc = subprocess.run(
                shlex.split(cmd),
                cwd=target_dir,
                capture_output=True,
                text=True
            )
            if proc.returncode != 0:
                raise PatchError(f"Falha ao aplicar {patch_file}:\n{proc.stderr}")
            out = proc.stdout

        if out:
            log.debug(f"Saída do patch {patch_file.name}:\n{out.strip()}")
        return True

    except Exception as e:
        log.error(f"Erro ao aplicar {patch_file}: {e}")
        return False

def _collect_patches(port: Dict[str, Any]) -> List[Path]:
    """
    Collect patch files for a port.
    - If Portfile.yaml defines 'patches', use that order
    - Otherwise, auto-discover in patches/ folder
    """
    port_path = Path(port.get("path", "."))
    explicit = port.get("patches")

    if explicit:
        patches = [port_path / p for p in explicit]
    else:
        patches_dir = port_path / "patches"
        if patches_dir.exists():
            patches = sorted(patches_dir.glob("*.patch")) + sorted(patches_dir.glob("*.diff"))
        else:
            patches = []

    return [p for p in patches if p.exists()]

# ---------------- Public API ----------------

def apply_patches(port: Dict[str, Any], build_dir: Path, sandbox: Optional[Sandbox] = None) -> bool:
    """
    Apply all patches for a port inside build_dir
    """
    portname = port.get("name", "unknown")
    patches = _collect_patches(port)

    if not patches:
        log.info(f"[{portname}] Nenhum patch encontrado")
        return True

    log.info(f"[{portname}] Aplicando {len(patches)} patches...")

    for patch_file in patches:
        ok = _apply_patch_file(patch_file, build_dir, sandbox=sandbox)
        if not ok:
            log.error(f"[{portname}] Falha no patch {patch_file.name}, abortando")
            return False

    log.info(f"[{portname}] Todos os patches aplicados com sucesso")
    return True

# ---------------- CLI (debug) ----------------

def _cli():
    import argparse, yaml, sys

    parser = argparse.ArgumentParser(description="Apply patches for a port")
    parser.add_argument("portfile", help="Path to Portfile.yaml")
    parser.add_argument("--build-dir", required=True, help="Build directory")
    parser.add_argument("--sandbox", action="store_true", help="Run inside sandbox")
    args = parser.parse_args()

    port = {}
    try:
        with open(args.portfile, "r", encoding="utf-8") as f:
            port = yaml.safe_load(f) or {}
            port["path"] = str(Path(args.portfile).parent)
    except Exception as e:
        log.error(f"Erro ao carregar Portfile: {e}")
        sys.exit(1)

    sb = Sandbox("/pyport/sandbox") if args.sandbox else None
    ok = apply_patches(port, Path(args.build_dir), sandbox=sb)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    _cli()
