#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
compile.py - Advanced build module for PyPort

Features:
 - Auto-detect build system
 - Parallel builds with fallback
 - Real-time colored logs
 - Pre/post build hooks
 - Build caching & incremental detection
 - Retry mechanism
 - Sandbox & systemd integration
 - Resource stats (CPU, RAM, time)
"""

import subprocess
import os
import hashlib
import time
import shutil
import psutil
from pathlib import Path
from typing import Dict, Any, Optional, List

from pyport.logger import get_logger
from pyport.sandbox import Sandbox
from pyport.hooks import run_hook

log = get_logger("pyport.compile")

class CompileError(Exception):
    pass

# ---------------- Utils ----------------

def _hash_source(build_dir: Path) -> str:
    """Hash source files for caching (based on mtime + name)"""
    h = hashlib.sha256()
    for f in build_dir.rglob("*"):
        if f.is_file():
            h.update(f.name.encode())
            h.update(str(f.stat().st_mtime).encode())
    return h.hexdigest()

def _stream_logs(proc: subprocess.Popen, portname: str):
    """Stream logs in real-time with colors"""
    import sys
    import threading

    def reader(stream, prefix, color):
        for line in iter(stream.readline, b""):
            sys.stdout.write(f"\033[{color}m[{portname}] {prefix}: {line.decode().rstrip()}\033[0m\n")
            sys.stdout.flush()
        stream.close()

    threads = []
    threads.append(threading.Thread(target=reader, args=(proc.stdout, "OUT", "32")))
    threads.append(threading.Thread(target=reader, args=(proc.stderr, "ERR", "31")))
    for t in threads: t.start()
    for t in threads: t.join()

def _run_command(cmd: List[str], cwd: Path, sandbox: Optional[Sandbox], jobs: int, env: Dict[str, str], retries: int = 1):
    """Run a build command with retries and sandbox support"""
    if jobs > 1 and any("make" in c for c in cmd):
        cmd.append(f"-j{jobs}")
    log.info(f"Executando: {' '.join(cmd)} em {cwd}")

    for attempt in range(1, retries + 1):
        try:
            if sandbox:
                sandbox.run(" ".join(cmd), cwd=cwd, env=env)
            else:
                proc = subprocess.Popen(cmd, cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                _stream_logs(proc, cwd.name)
                proc.wait()
                if proc.returncode != 0:
                    raise subprocess.CalledProcessError(proc.returncode, cmd)
            return
        except subprocess.CalledProcessError as e:
            log.warning(f"Falha tentativa {attempt}/{retries}: {e}")
            if attempt == retries:
                raise CompileError(f"Falha definitiva compilando: {cmd}")

# ---------------- Auto-detect Build System ----------------

def detect_build_system(build_dir: Path) -> str:
    """Detect build system from files"""
    if (build_dir / "configure").exists():
        return "autotools"
    if (build_dir / "CMakeLists.txt").exists():
        return "cmake"
    if (build_dir / "meson.build").exists():
        return "meson"
    if (build_dir / "Cargo.toml").exists():
        return "cargo"
    if (build_dir / "setup.py").exists():
        return "python"
    if any(build_dir.glob("*.java")) or (build_dir / "pom.xml").exists():
        return "java"
    return "custom"

# ---------------- Build Runners ----------------

def _run_make(build_dir, sandbox, jobs, env): _run_command(["make"], build_dir, sandbox, jobs, env, retries=2)
def _run_ninja(build_dir, sandbox, jobs, env): _run_command(["ninja"], build_dir, sandbox, jobs, env, retries=2)
def _run_cargo(build_dir, sandbox, jobs, env): _run_command(["cargo", "build", "--release"], build_dir, sandbox, jobs, env)
def _run_python(build_dir, sandbox, jobs, env): _run_command(["python3", "setup.py", "build"], build_dir, sandbox, jobs, env)
def _run_java(build_dir, sandbox, jobs, env):
    java_files = [str(f) for f in build_dir.glob("*.java")]
    if not java_files:
        raise CompileError("Nenhum .java encontrado")
    _run_command(["javac"] + java_files, build_dir, sandbox, jobs, env)

# ---------------- Public API ----------------

def compile_port(
    port: Dict[str, Any],
    build_dir: Path,
    sandbox: Optional[Sandbox] = None,
    jobs: Optional[int] = None,
    force: bool = False
):
    """Compile port with sandbox, hooks, caching and retries"""
    portname = port.get("name", build_dir.name)
    system = port.get("build_system", "").lower()
    env = os.environ.copy()
    jobs = jobs or os.cpu_count() or 1

    if not system:
        system = detect_build_system(build_dir)
        log.info(f"[{portname}] Build system detectado: {system}")

    cache_file = build_dir / ".compile_done"
    src_hash = _hash_source(build_dir)

    if cache_file.exists() and not force:
        prev_hash = cache_file.read_text().strip()
        if prev_hash == src_hash:
            log.info(f"[{portname}] Build já feito, pulando")
            return

    run_hook(port, "pre_build", build_dir, sandbox)

    log.info(f"[{portname}] Iniciando compilação ({system}, -j{jobs})")
    start_time = time.time()

    try:
        if system in ["autotools", "cmake"]:
            _run_make(build_dir, sandbox, jobs, env)
        elif system == "meson":
            _run_ninja(build_dir / "build", sandbox, jobs, env)
        elif system == "cargo":
            _run_cargo(build_dir, sandbox, jobs, env)
        elif system == "python":
            _run_python(build_dir, sandbox, jobs, env)
        elif system == "java":
            _run_java(build_dir, sandbox, jobs, env)
        elif system == "custom":
            log.info(f"[{portname}] Usando hooks customizados")
        else:
            raise CompileError(f"Sistema não suportado: {system}")
    except CompileError:
        if jobs > 1:
            log.warning(f"[{portname}] Tentando fallback para -j1")
            _run_make(build_dir, sandbox, 1, env)
        else:
            raise

    elapsed = time.time() - start_time
    cache_file.write_text(src_hash)

    run_hook(port, "post_build", build_dir, sandbox)

    # Stats
    process = psutil.Process(os.getpid())
    mem = process.memory_info().rss / (1024**2)
    log.info(f"[{portname}] Build concluído em {elapsed:.1f}s | Memória usada: {mem:.1f} MB")

# ---------------- CLI ----------------

def _cli():
    import argparse, yaml, sys
    parser = argparse.ArgumentParser(description="Compile a port")
    parser.add_argument("portfile", help="Portfile.yaml")
    parser.add_argument("--build-dir", required=True)
    parser.add_argument("-j", "--jobs", type=int, default=None)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    try:
        with open(args.portfile, "r", encoding="utf-8") as f:
            port = yaml.safe_load(f) or {}
    except Exception as e:
        log.error(f"Erro carregando Portfile: {e}")
        sys.exit(1)

    try:
        compile_port(port, Path(args.build_dir), jobs=args.jobs, force=args.force)
    except Exception as e:
        log.error(f"Build falhou: {e}")
        sys.exit(1)

if __name__ == "__main__":
    _cli()
