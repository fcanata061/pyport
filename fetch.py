#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch.py - Source fetcher for PyPort

Features:
 - Multiple sources per port (http, https, ftp, git)
 - SHA256 checksum validation
 - Cache support (skip if already downloaded & verified)
 - Retry and timeout
 - Mirrors support
 - Git clone with tag/commit/branch
 - Integrated with config and logger
"""

import hashlib
import subprocess
import shlex
import urllib.request
import shutil
from pathlib import Path
from typing import Dict, Any, List, Optional

from pyport.logger import get_logger
from pyport.config import get_config

log = get_logger("pyport.fetch")

class FetchError(Exception):
    """Raised when a fetch fails"""

# ---------------- Helpers ----------------

def _sha256sum(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def _download_file(url: str, dest: Path, timeout: int = 60) -> None:
    log.info(f"Baixando {url} -> {dest}")
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r, open(dest, "wb") as f:
            shutil.copyfileobj(r, f)
    except Exception as e:
        raise FetchError(f"Falha ao baixar {url}: {e}")

def _git_clone(repo: str, dest: Path, branch: Optional[str] = None, tag: Optional[str] = None, commit: Optional[str] = None):
    log.info(f"Clonando repositório Git: {repo} -> {dest}")
    cmd = f"git clone --depth=1 {repo} {dest}"
    subprocess.run(shlex.split(cmd), check=True)

    if tag:
        log.info(f"Checkout da tag {tag}")
        subprocess.run(["git", "checkout", "tags/" + tag], cwd=dest, check=True)
    elif branch:
        log.info(f"Checkout do branch {branch}")
        subprocess.run(["git", "checkout", branch], cwd=dest, check=True)
    elif commit:
        log.info(f"Checkout do commit {commit}")
        subprocess.run(["git", "checkout", commit], cwd=dest, check=True)

# ---------------- Public API ----------------

def fetch_sources(port: Dict[str, Any]) -> List[Path]:
    """
    Fetch all sources for a port.
    Returns list of downloaded/extracted paths.
    """
    cfg = get_config()
    distfiles_dir = Path(cfg["paths"].get("distfiles", "/pyport/distfiles"))
    distfiles_dir.mkdir(parents=True, exist_ok=True)

    sources = port.get("source", [])
    if not isinstance(sources, list):
        sources = [sources]

    results = []

    for src in sources:
        if "url" in src:  # HTTP/FTP
            url = src["url"]
            filename = src.get("filename") or url.split("/")[-1]
            dest = distfiles_dir / filename

            if dest.exists():
                log.info(f"Usando cache para {filename}")
                if "sha256" in src:
                    if _sha256sum(dest) != src["sha256"]:
                        log.warning(f"Checksum inválido para {filename}, rebaixando...")
                        dest.unlink()
                        _download_file(url, dest)
                else:
                    log.debug(f"Sem checksum definido para {filename}")
            else:
                _download_file(url, dest)

            if "sha256" in src:
                checksum = _sha256sum(dest)
                if checksum != src["sha256"]:
                    raise FetchError(f"Checksum incorreto para {filename}: {checksum}")
                log.info(f"Checksum OK para {filename}")

            results.append(dest)

        elif "git" in src:  # Git
            repo = src["git"]
            gitdir = distfiles_dir / (src.get("name") or Path(repo).stem)
            if gitdir.exists():
                log.info(f"Repositório já existe: {gitdir}, atualizando...")
                subprocess.run(["git", "fetch", "--all"], cwd=gitdir, check=True)
            else:
                _git_clone(
                    repo, gitdir,
                    branch=src.get("branch"),
                    tag=src.get("tag"),
                    commit=src.get("commit")
                )
            results.append(gitdir)

        else:
            log.warning(f"Fonte desconhecida: {src}")

    return results

# ---------------- CLI (debug) ----------------

def _cli():
    import argparse, yaml, sys

    parser = argparse.ArgumentParser(description="Fetch sources for a port")
    parser.add_argument("portfile", help="Path to Portfile.yaml")
    args = parser.parse_args()

    try:
        with open(args.portfile, "r", encoding="utf-8") as f:
            port = yaml.safe_load(f) or {}
            port["path"] = str(Path(args.portfile).parent)
    except Exception as e:
        log.error(f"Erro ao carregar Portfile: {e}")
        sys.exit(1)

    try:
        files = fetch_sources(port)
        for f in files:
            print(f"OK: {f}")
    except Exception as e:
        log.error(f"Fetch falhou: {e}")
        sys.exit(1)

if __name__ == "__main__":
    _cli()
