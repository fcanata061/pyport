# /pyport/fakeroot.py
"""
Enhanced Fakeroot wrapper for PyPort
-----------------------------------

Goal: provide a robust, testable, and packager-friendly layer to run build and
install commands inside a simulated-root environment using fakeroot and/or
bubblewrap (bwrap). Additionally it collects a metadata snapshot of the
sandbox contents (ownership/permissions/timestamps) which is required later
by the packager to produce packages with correct metadata.

Features:
 - Class-based API (Fakerunner) to hold options and run commands
 - Strong detection of required tools with configurable fallbacks
 - run() and run_and_check() with captured stdout/stderr and optional streaming
 - install_into_sandbox() that handles DESTDIR and common install flags
 - snapshot_sandbox_metadata() to collect file metadata into JSON (for packaging)
 - normalize_permissions() helper to set default permissions inside sandbox
 - dry_run and debug modes
 - CLI for manual testing and metadata export
"""

from __future__ import annotations
import os
import stat
import shutil
import subprocess
import shlex
import json
import time
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Any, Union, Tuple, Iterable
from pathlib import Path

DEFAULT_TIMEOUT = None

# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------

def which(prog: str) -> Optional[str]:
    """Cross-platform 'which' wrapper (uses shutil.which)."""
    return shutil.which(prog)

def safe_makedirs(path: Union[str, Path], mode: int = 0o755) -> None:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    try:
        p.chmod(mode)
    except Exception:
        pass

def _quote_list(lst: Iterable[str]) -> str:
    return " ".join(shlex.quote(str(x)) for x in lst)

# -----------------------------------------------------------------------------
# Data structures for metadata snapshot
# -----------------------------------------------------------------------------

@dataclass
class FileMeta:
    path: str           # relative to sandbox root
    is_dir: bool
    is_symlink: bool
    target: Optional[str] = None  # for symlink
    mode: int = 0
    uid: int = 0
    gid: int = 0
    size: int = 0
    mtime: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["mode"] = oct(self.mode)
        return d

# -----------------------------------------------------------------------------
# Exceptions
# -----------------------------------------------------------------------------

class FakerootError(RuntimeError):
    pass

class ToolMissingError(FakerootError):
    pass

# -----------------------------------------------------------------------------
# Main class
# -----------------------------------------------------------------------------

@dataclass
class Fakerunner:
    use_bwrap: bool = True
    use_fakeroot: bool = True
    require_tools: bool = False
    debug: bool = False
    dry_run: bool = False
    extra_bwrap_robinds: List[str] = field(default_factory=list)
    bind_host_paths: List[str] = field(default_factory=list)
    timeout: Optional[int] = DEFAULT_TIMEOUT

    def __post_init__(self):
        self._detect_tools()

    def _detect_tools(self) -> None:
        self._have_fakeroot = bool(which("fakeroot"))
        self._have_bwrap = bool(which("bwrap"))
        if self.debug:
            print(f"[fakeroot] detect: fakeroot={self._have_fakeroot}, bwrap={self._have_bwrap}")
        if self.require_tools:
            if self.use_fakeroot and not self._have_fakeroot:
                raise ToolMissingError("fakeroot requested but not found")
            if self.use_bwrap and not self._have_bwrap:
                raise ToolMissingError("bwrap requested but not found")

    # ---------------------------
    # Command construction helpers
    # ---------------------------

    def _bwrap_prefix(self, sandbox_dir: str) -> List[str]:
        # Build a conservative bwrap prefix. We mount common system dirs RO
        prefix = ["bwrap", "--unshare-all", "--share-net", "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", "/run"]
        for p in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc"):
            if Path(p).exists():
                prefix += ["--ro-bind", p, p]
        for p in self.extra_bwrap_robinds:
            if Path(p).exists():
                prefix += ["--ro-bind", p, p]
        for p in self.bind_host_paths:
            if Path(p).exists():
                prefix += ["--bind", p, p]
        # sandbox will be visible inside as /install
        prefix += ["--bind", str(Path(sandbox_dir).resolve()), "/install"]
        return prefix

    def _fakeroot_prefix(self) -> List[str]:
        if not self._have_fakeroot:
            if self.require_tools:
                raise ToolMissingError("fakeroot requested but not found")
            return []
        return ["fakeroot"]

    # ---------------------------
    # Execution primitives
    # ---------------------------

    def _construct_command(self, cmd: Union[str, List[str]], cwd: Optional[str], sandbox_dir: Optional[str], shell: bool) -> List[str]:
        parts: List[str] = []
        # bwrap prefix
        if self.use_bwrap:
            if not self._have_bwrap:
                if self.require_tools:
                    raise ToolMissingError("bwrap requested but not found")
                else:
                    if self.debug:
                        print("[fakeroot] warning: bwrap not available; continuing without it")
            else:
                if sandbox_dir is None:
                    raise ValueError("sandbox_dir is required when use_bwrap=True")
                parts += self._bwrap_prefix(sandbox_dir)
        # fakeroot prefix
        if self.use_fakeroot:
            if self._have_fakeroot:
                parts += self._fakeroot_prefix()
            else:
                if self.require_tools:
                    raise ToolMissingError("fakeroot requested but not found")
                else:
                    if self.debug:
                        print("[fakeroot] warning: fakeroot not available; running without simulated root")
        # payload
        if shell:
            # pass to sh -c to preserve quoting / env assignments in a single string
            if isinstance(cmd, list):
                payload = _quote_list(cmd)
            else:
                payload = cmd
            parts += ["sh", "-c", payload]
        else:
            if isinstance(cmd, str):
                payload_list = shlex.split(cmd)
            else:
                payload_list = list(cmd)
            parts += [str(x) for x in payload_list]
        return parts

    def run(self,
            cmd: Union[str, List[str]],
            cwd: Optional[str] = None,
            env: Optional[Dict[str, str]] = None,
            sandbox_dir: Optional[str] = None,
            shell: bool = False,
            check: bool = False,
            stream_output: bool = False
            ) -> subprocess.CompletedProcess:
        """
        Run a command with the configured fakeroot/bwrap options.

        - stream_output: if True, stream stdout/stderr live (useful for long builds)
        - check: if True, raise CalledProcessError on non-zero exit
        - env: merged onto os.environ for the child
        """
        if self.dry_run:
            print(f"[fakeroot][dry-run] would run: cmd={cmd} cwd={cwd} sandbox={sandbox_dir} shell={shell}")
            # build a dummy CompletedProcess
            cp = subprocess.CompletedProcess(args=cmd, returncode=0)
            return cp

        final_cmd = self._construct_command(cmd, cwd, sandbox_dir, shell)

        if self.debug:
            print(f"[fakeroot] running: {' '.join(shlex.quote(x) for x in final_cmd)}")
            if env:
                print(f"[fakeroot] env additions: {env}")
            if cwd:
                print(f"[fakeroot] cwd: {cwd}")

        proc_env = os.environ.copy()
        if env:
            for k, v in env.items():
                proc_env[str(k)] = str(v)

        # Execution: streaming vs capture
        if stream_output:
            p = subprocess.Popen(final_cmd, cwd=cwd, env=proc_env)
            rc = p.wait()
            if check and rc != 0:
                raise subprocess.CalledProcessError(rc, final_cmd)
            return subprocess.CompletedProcess(final_cmd, rc)
        else:
            completed = subprocess.run(final_cmd, cwd=cwd, env=proc_env, capture_output=True, text=True, timeout=self.timeout)
            if self.debug:
                print("[fakeroot] stdout:", completed.stdout)
                print("[fakeroot] stderr:", completed.stderr)
            if check and completed.returncode != 0:
                # raise with captured output
                raise subprocess.CalledProcessError(completed.returncode, final_cmd, output=completed.stdout, stderr=completed.stderr)
            return completed

    def run_and_check(self, *args, **kwargs) -> subprocess.CompletedProcess:
        return self.run(*args, check=True, **kwargs)

    # ---------------------------
    # Install helpers
    # ---------------------------

    def install_into_sandbox(self,
                             install_cmd: Union[str, List[str]],
                             build_dir: Optional[str],
                             sandbox_dir: str,
                             shell: bool = False,
                             extra_env: Optional[Dict[str, str]] = None,
                             stream_output: bool = False) -> subprocess.CompletedProcess:
        """
        Run an install command targeting sandbox_dir. If bwrap is enabled the sandbox
        will be exposed inside as '/install' and we append 'DESTDIR=/install' if the
        install_cmd does not already provide a DESTDIR-like token.

        If bwrap is disabled, we export DESTDIR as env var pointing to sandbox_dir.
        """
        # ensure sandbox exists
        safe_makedirs(sandbox_dir)
        # detect tokens
        def _contains_dest_like(c: Union[str, List[str]]) -> bool:
            s = " ".join(c) if isinstance(c, list) else c
            return "DESTDIR=" in s or "--root=" in s or "--prefix=" in s and "/install" in s

        if self.use_bwrap and self._have_bwrap:
            # use /install inside container
            if not _contains_dest_like(install_cmd):
                if isinstance(install_cmd, str):
                    full = f"{install_cmd} DESTDIR=/install"
                else:
                    full = list(install_cmd) + ["DESTDIR=/install"]
                shell_flag = isinstance(full, str)
            else:
                full = install_cmd
                shell_flag = shell
            return self.run_and_check(full, cwd=build_dir, env=extra_env, sandbox_dir=sandbox_dir, shell=shell_flag, stream_output=stream_output)
        else:
            # export DESTDIR env var
            env = dict(extra_env or {})
            env["DESTDIR"] = str(Path(sandbox_dir).resolve())
            # if install_cmd is string and user expects shell, run with shell True
            shell_flag = shell or isinstance(install_cmd, str)
            return self.run_and_check(install_cmd, cwd=build_dir, env=env, sandbox_dir=None, shell=shell_flag, stream_output=stream_output)

    # ---------------------------
    # Sandbox metadata snapshot
    # ---------------------------

    def snapshot_sandbox_metadata(self, sandbox_dir: str, skip_patterns: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        """
        Walk sandbox_dir and return a list of metadata dicts for each file/dir/symlink.

        Each dict contains: path (relative), is_dir, is_symlink, target, mode, uid, gid, size, mtime
        This output is JSON-serializable and intended for packager to create correct metadata.
        """
        root = Path(sandbox_dir).resolve()
        if not root.exists():
            raise FileNotFoundError(f"Sandbox not found: {sandbox_dir}")
        skip_patterns = skip_patterns or []
        out: List[Dict[str, Any]] = []
        for p in root.rglob("*"):
            rel = p.relative_to(root)
            rel_str = str(rel)
            # skip patterns
            if any(pat and pat in rel_str for pat in skip_patterns):
                continue
            try:
                st = p.lstat()
                is_dir = p.is_dir()
                is_link = p.is_symlink()
                target = None
                if is_link:
                    try:
                        target = os.readlink(str(p))
                    except Exception:
                        target = None
                meta = FileMeta(
                    path=rel_str,
                    is_dir=is_dir,
                    is_symlink=is_link,
                    target=target,
                    mode=st.st_mode & 0o7777,
                    uid=st.st_uid,
                    gid=st.st_gid,
                    size=st.st_size,
                    mtime=st.st_mtime
                )
                out.append(meta.to_dict())
            except Exception:
                # continue on permission errors or transient filesystem issues
                continue
        return out

    def write_metadata_json(self, sandbox_dir: str, dest_file: str, skip_patterns: Optional[List[str]] = None) -> None:
        data = self.snapshot_sandbox_metadata(sandbox_dir, skip_patterns=skip_patterns)
        safe_makedirs(Path(dest_file).parent)
        with open(dest_file, "w", encoding="utf-8") as f:
            json.dump({"generated_at": time.time(), "entries": data}, f, indent=2)

    # ---------------------------
    # Helpers for normalizing sandbox
    # ---------------------------

    def normalize_permissions(self, sandbox_dir: str, default_file_mode: int = 0o644, default_dir_mode: int = 0o755) -> None:
        """
        Walk sandbox and ensure files/dirs have sensible default permissions,
        for example remove world-writable bits unless explicitly set.
        """
        root = Path(sandbox_dir).resolve()
        if not root.exists():
            return
        for p in root.rglob("*"):
            try:
                if p.is_symlink():
                    continue
                st = p.stat()
                if p.is_dir():
                    desired = default_dir_mode
                else:
                    desired = default_file_mode
                # keep executable bit if any owner execute present
                if st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH):
                    desired |= (st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH))
                # apply conservative mask: remove world-writable unless already set
                new_mode = st.st_mode
                # set desired lower 9 bits
                new_mode = (new_mode & ~0o777) | (desired & 0o777)
                try:
                    p.chmod(new_mode)
                except Exception:
                    pass
            except Exception:
                continue

    def make_executable(self, path: str) -> None:
        """Set owner executable bit on `path` inside sandbox."""
        try:
            p = Path(path)
            if p.exists() and not p.is_symlink():
                mode = p.stat().st_mode
                p.chmod(mode | stat.S_IXUSR)
        except Exception:
            pass

    def create_device_placeholder(self, sandbox_dir: str, relpath: str) -> None:
        """
        DO NOT create real device nodes as that requires root. Instead create a
        placeholder file that the packager can interpret (for example a JSON
        instruction to create the device on install). This keeps the sandbox
        non-privileged and safe.
        """
        dest = Path(sandbox_dir) / relpath
        safe_makedirs(dest.parent)
        with open(dest.with_suffix(".device-placeholder"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"path": str(relpath), "note": "device placeholder - create real device on target install if needed"}))

    # ---------------------------
    # Binding helpers (for chroot / toolchain)
    # ---------------------------

    def ensure_bind_paths(self, sandbox_dir: str, binds: List[Tuple[str, str]]) -> List[Tuple[str, str, bool]]:
        """
        Ensure bind mounts (source, dest) exist as directories and return list of
        attempted mounts with a flag if mount was requested. Note: this function
        does not perform privileged mount; it only prepares directories and returns
        what should be mounted by the caller (or by a helper that has privileges).
        """
        results = []
        for src, dst in binds:
            dst_path = Path(sandbox_dir) / dst.lstrip("/")
            safe_makedirs(dst_path)
            results.append((src, str(dst_path), True))
        return results

# -----------------------------------------------------------------------------
# Minimal CLI for testing and metadata generation
# -----------------------------------------------------------------------------

def _cli_main():
    import argparse
    parser = argparse.ArgumentParser(prog="pyport-fakeroot", description="Test enhanced fakeroot module")
    parser.add_argument("cmd", nargs="*", help="Command to run (if omitted, only metadata ops run)")
    parser.add_argument("--sandbox", "-s", required=True, help="Sandbox path (will be created if absent)")
    parser.add_argument("--no-bwrap", dest="use_bwrap", action="store_false", help="Do not use bwrap")
    parser.add_argument("--no-fakeroot", dest="use_fakeroot", action="store_false", help="Do not use fakeroot")
    parser.add_argument("--stream", action="store_true", help="Stream output instead of capturing")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--snapshot", help="Write sandbox metadata to this file (JSON)")
    args = parser.parse_args()

    runner = Fakerunner(use_bwrap=args.use_bwrap, use_fakeroot=args.use_fakeroot, debug=args.debug, dry_run=args.dry_run)
    safe_makedirs(args.sandbox)
    if args.cmd:
        cmd = " ".join(args.cmd)
        try:
            cp = runner.run_and_check(cmd, cwd=None, sandbox_dir=args.sandbox, shell=True, stream_output=args.stream)
            print(f"Command exit: {cp.returncode}")
        except subprocess.CalledProcessError as e:
            print("Command failed:", e)
    if args.snapshot:
        runner.write_metadata_json(args.sandbox, args.snapshot)
        print("Snapshot written to", args.snapshot)

if __name__ == "__main__":
    _cli_main()
