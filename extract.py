#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract.py - Source extractor for PyPort

Features:
 - Detect and extract multiple archive formats
 - Run extraction inside sandbox build dir
 - Handles multiple distfiles per port
 - Skips if already extracted
 - Logs each step
"""

import tarfile
import zipfile
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional

from pyport.logger import get_logger
from pyport.config import get_config
from pyport.sandbox import Sandbox

log = get_logger("pyport.extract")

class ExtractError(Exception):
    """Raised when extraction fails"""

# ---------------- Helpers ----------------

def _extract_tar(archive: Path, dest: Path):
    with tarfile.open(archive, "r:*") as tar:
        tar.extractall(dest)

def _extract_zip(archive: Path, dest: Path):
    with zipfile.ZipFile(archive, "r") as z:
        z.extractall(dest)

def _extract_xz(archive: Path, dest: Path):
    # fallback to system xz
    subprocess.run(["tar", "xf", str(archive), "-C", str(dest)], check=True)

def _detect_format(archive: Path) -> str:
    name = archive.name
    if name.endswith((".tar.gz", ".tgz")):
        return "tar.gz"
    elif name.endswith((".tar.xz", ".txz")):
        return "tar.xz"
    elif name.endswith((".tar.bz2", ".tbz2")):
        return "tar.bz2"
    elif name.endswith(".zip"):
        return "zip"
    elif name.endswith(".gz"):
        return "gz"
    elif name.endswith(".xz"):
        return "xz"
    else:
        return "unknown"

# ---------------- Public API ----------------

def extract_sources(
    port: Dict[str, Any],
    distfiles: List[Path],
    sandbox: Optional[Sandbox] = None,
    force: bool = False
) -> Path:
    """
    Extract sources into sandbox build directory.
    Returns the main extracted directory.
    """
    cfg = get_config()
    build_root = Path(cfg["paths"].get("build", "/pyport/build"))
    build_root.mkdir(parents=True, exist_ok=True)

    portname = port.get("name", "unknown")
    build_dir = build_root / portname

    if build_dir.exists() and not force:
        log.info(f"[{portname}] Já extraído em {build_dir}, pulando")
        return build_dir
    elif build_dir.exists():
        shutil.rmtree(build_dir)

    build_dir.mkdir(parents=True, exist_ok=True)
    log.info(f"[{portname}] Extraindo fontes para {build_dir}")

    for archive in distfiles:
        fmt = _detect_format(archive)
        log.debug(f"[{portname}] Extraindo {archive.name} ({fmt})")

        try:
            if sandbox:
                sandbox.run(f"tar xf {archive} -C {build_dir}")
            else:
                if fmt.startswith("tar"):
                    _extract_tar(archive, build_dir)
                elif fmt == "zip":
                    _extract_zip(archive, build_dir)
                elif fmt in ["gz", "xz"]:
                    _extract_xz(archive, build_dir)
                else:
                    raise ExtractError(f"Formato não suportado: {archive.name}")
        except Exception as e:
            raise ExtractError(f"Falha ao extrair {archive}: {e}")

    log.info(f"[{portname}] Extração concluída -> {build_dir}")
    return build_dir

# ---------------- CLI (debug) ----------------

def _cli():
    import argparse, yaml, sys

    parser = argparse.ArgumentParser(description="Extract sources for a port")
    parser.add_argument("portfile", help="Path to Portfile.yaml")
    parser.add_argument("--distfiles", nargs="+", required=True, help="Source archives")
    parser.add_argument("--force", action="store_true", help="Force re-extraction")
    args = parser.parse_args()

    try:
        with open(args.portfile, "r", encoding="utf-8") as f:
            port = yaml.safe_load(f) or {}
            port["path"] = str(Path(args.portfile).parent)
    except Exception as e:
        log.error(f"Erro ao carregar Portfile: {e}")
        sys.exit(1)

    files = [Path(f) for f in args.distfiles]

    try:
        build_dir = extract_sources(port, files, force=args.force)
        print(f"OK: {build_dir}")
    except Exception as e:
        log.error(f"Extração falhou: {e}")
        sys.exit(1)

if __name__ == "__main__":
    _cli()
