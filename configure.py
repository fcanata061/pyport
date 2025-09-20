#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
configure.py - Advanced build configuration module for PyPort

Features:
 - Detect and run correct build system (autotools, cmake, meson, cargo, python, java, custom)
 - Auto-detect build system if not specified
 - Support toolchains (/mnt/tools) and cross-compilation
 - Configure environment (CFLAGS, LDFLAGS, PKG_CONFIG_PATH)
 - Run inside sandbox with timeout
 - Pre/post configure hooks and configure_env hook
 - Cache to skip repeated configuration
"""

import subprocess
import os
import hashlib
from pathlib import Path
from typing import Dict, Any, Optional, List

from pyport.logger import get_logger
from pyport.sandbox import Sandbox
from pyport.hooks import run_hook

log = get_logger("pyport.configure")

class ConfigureError(Exception):
    """Raised when configure step fails"""

# ---------------- Utils ----------------

def _hash_env(env: Dict[str, str]) -> str:
    """Generate a hash of env vars to detect changes"""
    env_str = "|".join(f"{k}={v}" for k, v in sorted(env.items()))
    return hashlib.sha256(env_str.encode()).hexdigest()

def _load_env(port: Dict[str, Any]) -> Dict[str, str]:
    """Prepare build environment"""
    env = os.environ.copy()

    # Toolchain integration
    toolchain = port.get("toolchain", "/mnt/tools")
    if Path(toolchain).exists():
        env["PATH"] = f"{toolchain}/bin:" + env["PATH"]
        env["LD_LIBRARY_PATH"] = f"{toolchain}/lib:" + env.get("LD_LIBRARY_PATH", "")
        env["PKG_CONFIG_PATH"] = f"{toolchain}/lib/pkgconfig:" + env.get("PKG_CONFIG_PATH", "")

    # Flags
    for var in ["CFLAGS", "CXXFLAGS", "LDFLAGS"]:
        if port.get(var.lower()):
            env[var] = port[var.lower()]

    return env

def _detect_system(build_dir: Path) -> str:
    """Detect build system from files in source tree"""
    if (build_dir / "configure").exists():
        return "autotools"
    elif (build_dir / "CMakeLists.txt").exists():
        return "cmake"
    elif (build_dir / "meson.build").exists():
        return "meson"
    elif (build_dir / "Cargo.toml").exists():
        return "cargo"
    elif (build_dir / "setup.py").exists():
        return "python"
    elif list(build_dir.glob("*.java")):
        return "java"
    else:
        return "custom"

def _run_command(cmd: List[str], cwd: Path, env: Dict[str, str], sandbox: Optional[Sandbox], timeout: int = 1800):
    """Run command inside sandbox or directly"""
    log.info(f"Executando: {' '.join(cmd)} (em {cwd})")
    try:
        if sandbox:
            sandbox.run(" ".join(cmd), cwd=cwd, env=env, timeout=timeout)
        else:
            subprocess.run(cmd, cwd=cwd, env=env, check=True, timeout=timeout)
    except subprocess.CalledProcessError as e:
        raise ConfigureError(f"Comando falhou: {cmd} ({e})")
    except subprocess.TimeoutExpired:
        raise ConfigureError(f"Configuração excedeu o tempo limite ({timeout}s)")

# ---------------- Build System Runners ----------------

def _run_autotools(build_dir: Path, env: Dict[str, str], sandbox: Optional[Sandbox], extra_args: List[str]):
    cmd = ["./configure", "--prefix=/usr"] + extra_args
    _run_command(cmd, build_dir, env, sandbox)

def _run_cmake(build_dir: Path, env: Dict[str, str], sandbox: Optional[Sandbox], extra_args: List[str]):
    cmd = ["cmake", ".", "-DCMAKE_INSTALL_PREFIX=/usr"] + extra_args
    _run_command(cmd, build_dir, env, sandbox)

def _run_meson(build_dir: Path, env: Dict[str, str], sandbox: Optional[Sandbox], extra_args: List[str]):
    build_subdir = build_dir / "build"
    build_subdir.mkdir(exist_ok=True)
    cmd = ["meson", "setup", str(build_subdir), "--prefix=/usr"] + extra_args
    _run_command(cmd, build_dir, env, sandbox)

def _run_cargo(build_dir: Path, env: Dict[str, str], sandbox: Optional[Sandbox], extra_args: List[str]):
    cmd = ["cargo", "build", "--release"] + extra_args
    _run_command(cmd, build_dir, env, sandbox)

def _run_python(build_dir: Path, env: Dict[str, str], sandbox: Optional[Sandbox], extra_args: List[str]):
    cmd = ["python3", "setup.py", "build"] + extra_args
    _run_command(cmd, build_dir, env, sandbox)

def _run_java(build_dir: Path, env: Dict[str, str], sandbox: Optional[Sandbox], extra_args: List[str]):
    java_files = [str(f) for f in build_dir.glob("*.java")]
    if not java_files:
        raise ConfigureError("Nenhum arquivo .java encontrado")
    cmd = ["javac"] + java_files + extra_args
    _run_command(cmd, build_dir, env, sandbox)

# ---------------- Public API ----------------

def configure(port: Dict[str, Any], build_dir: Path, sandbox: Optional[Sandbox] = None, force: bool = False):
    """
    Run the configure step for the given port.
    """
    portname = port.get("name", "unknown")
    system = port.get("build_system")
    if not system:
        system = _detect_system(build_dir)
    extra_args = port.get("configure_args", [])
    env = _load_env(port)

    cache_file = build_dir / ".configure_done"
    env_hash = _hash_env(env)

    # Skip if already configured
    if cache_file.exists() and not force:
        prev_hash = cache_file.read_text().strip()
        if prev_hash == env_hash:
            log.info(f"[{portname}] Configuração já feita, pulando")
            return

    log.info(f"[{portname}] Configurando build system: {system}")

    # Run pre-configure hooks
    run_hook(port, "configure_env", build_dir, sandbox)
    run_hook(port, "pre_configure", build_dir, sandbox)

    if system == "autotools":
        _run_autotools(build_dir, env, sandbox, extra_args)
    elif system == "cmake":
        _run_cmake(build_dir, env, sandbox, extra_args)
    elif system == "meson":
        _run_meson(build_dir, env, sandbox, extra_args)
    elif system == "cargo":
        _run_cargo(build_dir, env, sandbox, extra_args)
    elif system == "python":
        _run_python(build_dir, env, sandbox, extra_args)
    elif system == "java":
        _run_java(build_dir, env, sandbox, extra_args)
    elif system == "custom":
        log.info(f"[{portname}] Usando configuração custom (via hooks)")
    else:
        raise ConfigureError(f"Sistema de build desconhecido: {system}")

    # Run post-configure hooks
    run_hook(port, "post_configure", build_dir, sandbox)

    # Save cache
    cache_file.write_text(env_hash)

    log.info(f"[{portname}] Configuração concluída")

# ---------------- CLI (debug) ----------------

def _cli():
    import argparse, yaml, sys

    parser = argparse.ArgumentParser(description="Configure a port build system")
    parser.add_argument("portfile", help="Path to Portfile.yaml")
    parser.add_argument("--build-dir", required=True, help="Build directory")
    parser.add_argument("--force", action="store_true", help="Force reconfigure")
    args = parser.parse_args()

    try:
        with open(args.portfile, "r", encoding="utf-8") as f:
            port = yaml.safe_load(f) or {}
    except Exception as e:
        log.error(f"Erro ao carregar Portfile: {e}")
        sys.exit(1)

    try:
        configure(port, Path(args.build_dir), force=args.force)
        print("Configuração OK")
    except Exception as e:
        log.error(f"Configuração falhou: {e}")
        sys.exit(1)

if __name__ == "__main__":
    _cli()
