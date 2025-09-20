"""
pyport fakeroot module - enhanced and functional

Provides robust utilities to run build/install commands inside a simulated root
environment using fakeroot and optionally isolated with bubblewrap (bwrap).

Features:
 - Detects availability of 'fakeroot' and 'bwrap'
 - Builds safe bwrap command lines with configurable read-only binds
 - Runs commands given as list or string, with optional shell execution
 - Option to require tools (raise helpful error if missing)
 - Better environment handling (merging, setting DESTDIR when needed)
 - install_into_sandbox convenience that appends/exports DESTDIR correctly
 - run_and_check wrapper to raise CalledProcessError on non-zero exit
 - Helpful logging and debug output
 - CLI for quick manual testing

Important:
 - This module constructs the command but does not perform privileged mounts.
 - When using bwrap, sandbox_dir is bound inside the container as /install.
 - When bwrap is not used, DESTDIR is passed via the environment.
"""

from __future__ import annotations
import shutil
import subprocess
import shlex
import os
import stat
from typing import Optional, List, Dict, Any, Union
import tempfile
import textwrap

DEFAULT_TIMEOUT = None

class ToolMissingError(RuntimeError):
    pass

def available_tools() -> Dict[str, bool]:
    """Return availability of fakeroot and bwrap in PATH."""
    return {
        "fakeroot": shutil.which("fakeroot") is not None,
        "bwrap": shutil.which("bwrap") is not None
    }

def _quote_arg(a: str) -> str:
    # Quote for shell display (not for passing as args list)
    return shlex.quote(a)

def _build_bwrap_base(sandbox_dir: str, extra_ro_binds: Optional[List[str]] = None,
                      bind_host_paths: Optional[List[str]] = None) -> List[str]:
    """
    Construct the base bwrap command that provides a minimal environment.
    - sandbox_dir will be bound to /install inside bwrap.
    - extra_ro_binds: host paths to --ro-bind into same path inside bwrap.
    - bind_host_paths: host paths to --bind (rw) into same path inside bwrap.
    """
    bwrap_cmd = [
        "bwrap",
        "--unshare-all",
        "--share-net",  # keep network inside namespace
        "--proc", "/proc",
        "--dev", "/dev",
        "--tmpfs", "/tmp",
        "--dir", "/run",
    ]

    # Common system dirs mounted read-only
    for p in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc"):
        if os.path.exists(p):
            bwrap_cmd += ["--ro-bind", p, p]

    # Extra ro binds if requested
    if extra_ro_binds:
        for p in extra_ro_binds:
            if os.path.exists(p):
                bwrap_cmd += ["--ro-bind", p, p]

    # Bind host paths read-write
    if bind_host_paths:
        for p in bind_host_paths:
            if os.path.exists(p):
                bwrap_cmd += ["--bind", p, p]

    # Bind the sandbox as /install inside the container
    # Ensure absolute path
    sandbox_dir = os.path.abspath(sandbox_dir)
    bwrap_cmd += ["--bind", sandbox_dir, "/install"]

    return bwrap_cmd

def _ensure_sandbox_dir(sandbox_dir: str) -> None:
    p = os.path.abspath(sandbox_dir)
    os.makedirs(p, exist_ok=True)
    # ensure reasonable permissions
    try:
        os.chmod(p, 0o755)
    except Exception:
        pass

def run(cmd: Union[str, List[str]],
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
        use_bwrap: bool = False,
        sandbox_dir: Optional[str] = None,
        use_fakeroot: bool = True,
        timeout: Optional[int] = DEFAULT_TIMEOUT,
        extra_bwrap_robinds: Optional[List[str]] = None,
        bind_host_paths: Optional[List[str]] = None,
        check: bool = False,
        shell: bool = False,
        require_tools: bool = False,
        debug: bool = False) -> subprocess.CompletedProcess:
    """
    Execute a command optionally wrapped by bwrap and/or fakeroot.

    Parameters:
      cmd: command string or list
      cwd: working directory
      env: extra environment variables to merge (string keys->values)
      use_bwrap: if True, use bubblewrap (requires sandbox_dir)
      sandbox_dir: required when use_bwrap True
      use_fakeroot: if True, prefix with fakeroot when available
      timeout: seconds or None
      extra_bwrap_robinds: list of paths for --ro-bind
      bind_host_paths: list of host paths for --bind (rw)
      check: if True, raise CalledProcessError on non-zero exit
      shell: if True, run via shell (dangerous with untrusted input)
      require_tools: if True, raise ToolMissingError when requested tools are absent
      debug: if True, print constructed command to stdout

    Returns CompletedProcess
    """
    tools = available_tools()
    if require_tools:
        if use_bwrap and not tools.get("bwrap"):
            raise ToolMissingError("bwrap requested but not found in PATH")
        if use_fakeroot and not tools.get("fakeroot"):
            raise ToolMissingError("fakeroot requested but not found in PATH")

    final_cmd: List[str] = []

    # When using bwrap, construct the bwrap prefix
    if use_bwrap:
        if not sandbox_dir:
            raise ValueError("sandbox_dir is required when use_bwrap=True")
        if not tools.get("bwrap"):
            # fallback: either raise or proceed without bwrap
            if require_tools:
                raise ToolMissingError("bwrap requested but not found")
            else:
                print("[fakeroot] warning: bwrap not available, proceeding without bwrap")
        else:
            final_cmd.extend(_build_bwrap_base(sandbox_dir, extra_bwrap_robinds, bind_host_paths))

    # When using fakeroot, prefix
    if use_fakeroot:
        if tools.get("fakeroot"):
            final_cmd.append("fakeroot")
        else:
            if require_tools:
                raise ToolMissingError("fakeroot requested but not found")
            else:
                print("[fakeroot] warning: fakeroot not found; commands will run without simulated root")

    # Prepare the actual command payload
    if shell:
        # run via sh -c inside bwrap/fakeroot
        if isinstance(cmd, list):
            cmd_str = " ".join(shlex.quote(str(x)) for x in cmd)
        else:
            cmd_str = cmd  # type: ignore
        final_cmd.extend(["sh", "-c", cmd_str])
    else:
        if isinstance(cmd, str):
            cmd_list = shlex.split(cmd)
        else:
            cmd_list = cmd
        final_cmd.extend([str(x) for x in cmd_list])  # type: ignore

    # Merge environment
    proc_env = os.environ.copy()
    if env:
        proc_env.update({str(k): str(v) for k, v in env.items()})

    if debug:
        print("[fakeroot] constructed command:", " ".join(_quote_arg(x) for x in final_cmd))
        if cwd:
            print("[fakeroot] cwd:", cwd)
        if sandbox_dir:
            print("[fakeroot] sandbox_dir:", sandbox_dir)

    # Execute
    try:
        completed = subprocess.run(final_cmd, cwd=cwd, env=proc_env, timeout=timeout, check=check)
        return completed
    except subprocess.CalledProcessError:
        # re-raise to caller if check=True; otherwise return CompletedProcess-like via exception
        raise
    except FileNotFoundError as e:
        # helpful message for missing binary in constructed command
        raise FileNotFoundError(f"Executable not found when running command. Details: {e}")
    except Exception:
        raise

def run_and_check(*args, **kwargs) -> subprocess.CompletedProcess:
    """Convenience wrapper setting check=True so non-zero exit raises CalledProcessError."""
    return run(*args, check=True, **kwargs)

def install_into_sandbox(install_cmd: Union[str, List[str]],
                         build_dir: Optional[str],
                         sandbox_dir: str,
                         use_bwrap: bool = True,
                         use_fakeroot: bool = True,
                         extra_bwrap_robinds: Optional[List[str]] = None,
                         bind_host_paths: Optional[List[str]] = None,
                         timeout: Optional[int] = DEFAULT_TIMEOUT,
                         check: bool = True,
                         debug: bool = False) -> subprocess.CompletedProcess:
    """
    Run an installation command ensuring files end up in the sandbox.

    - If use_bwrap=True, the sandbox will be available as /install inside bwrap
      and the command will be run with DESTDIR=/install.
    - If use_bwrap=False, the environment variable DESTDIR will be set to sandbox_dir.

    install_cmd may already contain DESTDIR; we will not override if user supplied.
    """
    _ensure_sandbox_dir(sandbox_dir)

    # Detect if install_cmd already contains DESTDIR= or --root= style tokens
    def _contains_destdir(cmd_val: Union[str, List[str]]) -> bool:
        if isinstance(cmd_val, str):
            return "DESTDIR=" in cmd_val or "--root=" in cmd_val or "--prefix=" in cmd_val
        else:
            joined = " ".join(str(x) for x in cmd_val)
            return "DESTDIR=" in joined or "--root=" in joined or "--prefix=" in joined

    if use_bwrap:
        # inside bwrap sandbox is at /install
        if _contains_destdir(install_cmd):
            final_cmd = install_cmd
        else:
            if isinstance(install_cmd, str):
                final_cmd = f"{install_cmd} DESTDIR=/install"
            else:
                final_cmd = list(install_cmd) + ["DESTDIR=/install"]
        return run_and_check(final_cmd, cwd=build_dir, use_bwrap=True, sandbox_dir=sandbox_dir,
                             use_fakeroot=use_fakeroot, timeout=timeout,
                             extra_bwrap_robinds=extra_bwrap_robinds, bind_host_paths=bind_host_paths,
                             shell=isinstance(final_cmd, str), debug=debug)
    else:
        # pass DESTDIR via environment
        env = {"DESTDIR": os.path.abspath(sandbox_dir)}
        if _contains_destdir(install_cmd):
            final_cmd = install_cmd
            shell_flag = isinstance(final_cmd, str)
        else:
            if isinstance(install_cmd, str):
                final_cmd = install_cmd  # run with env DESTDIR
                shell_flag = True
            else:
                final_cmd = install_cmd
                shell_flag = False
        return run_and_check(final_cmd, cwd=build_dir, env=env, use_bwrap=False,
                             use_fakeroot=use_fakeroot, timeout=timeout, shell=shell_flag,
                             debug=debug)

# Small helpers to detect bwrap/fakeroot modes
def prefer_bwrap(cfg: Optional[Dict[str, Any]] = None) -> bool:
    if cfg and isinstance(cfg, dict):
        mode = cfg.get("sandbox_mode")
        return mode in ("bwrap", "both")
    return True

def prefer_fakeroot(cfg: Optional[Dict[str, Any]] = None) -> bool:
    if cfg and isinstance(cfg, dict):
        mode = cfg.get("sandbox_mode")
        return mode in ("fakeroot", "both")
    return True

# CLI for testing
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(prog="pyport-fakeroot", description="Test pyport fakeroot runner")
    parser.add_argument("cmd", nargs="+", help="command to run")
    parser.add_argument("--cwd", help="working directory")
    parser.add_argument("--sandbox", help="sandbox dir for bwrap (required for --bwrap)")
    parser.add_argument("--no-bwrap", dest="bwrap", action="store_false", help="do not use bwrap")
    parser.add_argument("--bwrap", dest="bwrap", action="store_true", help="use bwrap")
    parser.add_argument("--no-fakeroot", dest="fakeroot", action="store_false", help="do not use fakeroot")
    parser.add_argument("--fakeroot", dest="fakeroot", action="store_true", help="use fakeroot")
    parser.add_argument("--debug", action="store_true", help="show constructed command")
    args = parser.parse_args()
    cmd = " ".join(args.cmd)
    try:
        cp = run_and_check(cmd, cwd=args.cwd, use_bwrap=args.bwrap, sandbox_dir=args.sandbox,
                           use_fakeroot=args.fakeroot, debug=args.debug, shell=True)
        print("Exit code:", cp.returncode)
    except Exception as e:
        print("Error:", e)
