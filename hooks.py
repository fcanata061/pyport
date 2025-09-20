#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hooks.py - Hook execution manager for PyPort

Features:
 - Supports all standard hooks:
   pre_configure, post_configure,
   pre_build, post_build,
   pre_install, post_install,
   pre_remove, post_remove,
   check
 - Hooks can be defined inline in Portfile.yaml or as scripts
 - Runs inside sandbox (fakeroot/chroot)
 - Logging and error capture
 - Timeout control
"""

import subprocess
import shlex
import signal
from pathlib import Path
from typing import Dict, Any, Optional

from pyport.logger import get_logger
from pyport.config import get_config
from pyport.sandbox import Sandbox

log = get_logger("pyport.hooks")

# ---------------- Hook Runner ----------------

class HookError(Exception):
    """Raised when a hook fails"""

def _run_shell(cmd: str, cwd: Path, timeout: int = 600) -> str:
    """Run a shell command with timeout, return stdout"""
    try:
        proc = subprocess.run(
            shlex.split(cmd),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout
        )
        if proc.returncode != 0:
            raise HookError(f"Hook falhou: {cmd}\n{proc.stderr}")
        return proc.stdout
    except subprocess.TimeoutExpired:
        raise HookError(f"Hook expirou: {cmd}")

# ---------------- Public API ----------------

def run_hook(
    hook: str,
    port: Dict[str, Any],
    sandbox: Optional[Sandbox] = None,
    timeout: int = 600
) -> bool:
    """
    Run a given hook for a port.
    - hook: name of the hook (pre_install, post_remove, etc.)
    - port: port metadata (from Portfile.yaml)
    - sandbox: sandbox instance (if provided, run inside it)
    """
    cfg = get_config()
    portname = port.get("name", "unknown")
    hooks_dir = Path(port.get("path", ".")) / "hooks"

    # Priority: inline in Portfile.yaml > script in hooks/ > none
    inline_hooks = port.get("hooks", {})
    cmd = inline_hooks.get(hook)

    if not cmd:
        # Look for script file
        candidate = hooks_dir / f"{hook}.sh"
        if candidate.exists():
            cmd = f"sh {candidate}"
        else:
            candidate = hooks_dir / f"{hook}.py"
            if candidate.exists():
                cmd = f"python3 {candidate}"

    if not cmd:
        log.debug(f"[{portname}] Nenhum hook '{hook}' definido")
        return True

    log.info(f"[{portname}] Executando hook: {hook} -> {cmd}")

    try:
        if sandbox:
            # Run inside sandbox
            out = sandbox.run(cmd, timeout=timeout)
        else:
            # Run locally (fallback)
            out = _run_shell(cmd, cwd=Path(port.get("path", ".")), timeout=timeout)

        if out:
            log.debug(f"[{portname}] Saída do hook {hook}:\n{out.strip()}")

        return True

    except HookError as e:
        log.error(f"[{portname}] Hook {hook} falhou: {e}")
        return False

# ---------------- CLI (debug) ----------------

def _cli():
    import argparse, yaml, sys

    parser = argparse.ArgumentParser(description="Run PyPort hooks")
    parser.add_argument("portfile", help="Path to Portfile.yaml")
    parser.add_argument("hook", help="Hook name (pre_install, post_remove, etc.)")
    parser.add_argument("--sandbox", action="store_true", help="Run inside sandbox")
    args = parser.parse_args()

    port = {}
    try:
        with open(args.portfile, "r", encoding="utf-8") as f:
            port = yaml.safe_load(f) or {}
    except Exception as e:
        log.error(f"Erro ao carregar Portfile: {e}")
        sys.exit(1)

    sb = Sandbox(get_config()["paths"]["sandbox"]) if args.sandbox else None

    ok = run_hook(args.hook, port, sandbox=sb)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    _cli()
