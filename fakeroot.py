#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fakeroot.py — Versão evoluída do módulo de raiz simulada para PyPort

Funcionalidades:
 - use_fakeroot, use_bwrap ou ambos conforme configurado
 - Regras de fallback se ferramentas faltando
 - run(), run_and_check(), streaming ou captura
 - install_into_sandbox que detecta DESTDIR / --root / --prefix, etc
 - snapshot_sandbox_metadata com filtros
 - normalize_permissions
 - handles binds padrão vindos de config
 - CLI para debug, metadados, etc
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
from pathlib import Path
from typing import Optional, List, Dict, Any, Union, Iterable

# integração com logger e config
from pyport.logger import get_logger
from pyport.config import get_config

LOG = get_logger("pyport.fakeroot")

DEFAULT_TIMEOUT = None

def which(prog: str) -> Optional[str]:
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

@dataclass
class FileMeta:
    path: str  # relative to sandbox root
    is_dir: bool
    is_symlink: bool
    target: Optional[str] = None
    mode: int = 0
    uid: int = 0
    gid: int = 0
    size: int = 0
    mtime: float = 0.0

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["mode"] = oct(self.mode)
        return d

class FakerootError(RuntimeError):
    pass

class ToolMissingError(FakerootError):
    pass

@dataclass
class Fakerunner:
    use_bwrap: bool = True
    use_fakeroot: bool = True
    require_tools: bool = False
    debug: bool = False
    dry_run: bool = False
    timeout: Optional[int] = None
    extra_bwrap_robinds: List[str] = field(default_factory=list)
    bind_host_paths: List[str] = field(default_factory=list)

    def __post_init__(self):
        cfg = get_config()
        # pegar binds padrão da config, se existir
        binds = cfg.get("fakeroot", {}).get("bind_host_paths", [])
        if isinstance(binds, list):
            self.bind_host_paths.extend(binds)
        robinds = cfg.get("fakeroot", {}).get("extra_bwrap_robinds", [])
        if isinstance(robinds, list):
            self.extra_bwrap_robinds.extend(robinds)
        # timeout da config, se definido
        to = cfg.get("fakeroot", {}).get("timeout")
        if to is not None:
            try:
                self.timeout = int(to)
            except ValueError:
                pass
        else:
            # manter o valor padrão ou None
            pass

        self._detect_tools()

    def _detect_tools(self) -> None:
        self._have_fakeroot = bool(which("fakeroot"))
        self._have_bwrap = bool(which("bwrap"))
        if self.debug:
            LOG.info(f"[fakeroot] detect: fakeroot={self._have_fakeroot}, bwrap={self._have_bwrap}")
        if self.require_tools:
            if self.use_fakeroot and not self._have_fakeroot:
                raise ToolMissingError("fakeroot requested but not found")
            if self.use_bwrap and not self._have_bwrap:
                raise ToolMissingError("bwrap requested but not found")

    def _bwrap_prefix(self, sandbox_dir: str) -> List[str]:
        """ prefix para bubblewrap (bwrap) com binds padrão e mounts RO """
        prefix: List[str] = ["bwrap", "--unshare-all", "--share-net"]
        prefix += ["--proc", "/proc", "--dev", "/dev"]
        prefix += ["--tmpfs", "/tmp"]
        # ro-bind do system dirs
        for p in ("/usr", "/lib", "/lib64", "/bin", "/sbin", "/etc"):
            if Path(p).exists():
                prefix += ["--ro-bind", p, p]
        # binds adicionais
        for p in self.extra_bwrap_robinds:
            if Path(p).exists():
                prefix += ["--ro-bind", p, p]
        for p in self.bind_host_paths:
            if Path(p).exists():
                prefix += ["--bind", p, p]
        # montar sandbox_dir como /install dentro
        prefix += ["--dir", "/install"]
        prefix += ["--bind", str(Path(sandbox_dir).resolve()), "/install"]
        return prefix

    def _fakeroot_prefix(self) -> List[str]:
        if not self._have_fakeroot:
            if self.require_tools:
                raise ToolMissingError("fakeroot requested but not found")
            else:
                if self.debug:
                    LOG.warning("[fakeroot] fakeroot tool not found; proceeding without it")
                return []
        return ["fakeroot"]

    def _construct_command(self,
                           cmd: Union[str, List[str]],
                           cwd: Optional[str],
                           sandbox_dir: Optional[str],
                           shell: bool) -> List[str]:
        parts: List[str] = []
        # bwrap prefix
        if self.use_bwrap:
            if not self._have_bwrap:
                if self.require_tools:
                    raise ToolMissingError("bwrap requested but not found")
                else:
                    LOG.warning("[fakeroot] bwrap requested but not found; skipping bwrap")
            else:
                if sandbox_dir is None:
                    raise ValueError("sandbox_dir is required for bwrap usage")
                parts += self._bwrap_prefix(sandbox_dir)
        # fakeroot prefix
        if self.use_fakeroot:
            if self._have_fakeroot:
                parts += self._fakeroot_prefix()
            else:
                if self.require_tools:
                    raise ToolMissingError("fakeroot requested but not found")
                else:
                    LOG.warning("[fakeroot] fakeroot requested but not found; skipping fakeroot")

        # payload
        if shell:
            # combinar lista ou string em string para sh -c
            if isinstance(cmd, list):
                payload = _quote_list(cmd)
            else:
                payload = cmd
            parts += ["sh", "-c", payload]
        else:
            if isinstance(cmd, str):
                payload = shlex.split(cmd)
            else:
                payload = list(cmd)
            parts += [str(x) for x in payload]
        return parts

    def run(self,
            cmd: Union[str, List[str]],
            cwd: Optional[str] = None,
            env: Optional[Dict[str, str]] = None,
            sandbox_dir: Optional[str] = None,
            shell: bool = False,
            check: bool = False,
            stream_output: bool = False) -> subprocess.CompletedProcess:
        """Run a command inside the sandbox environment."""
        if self.dry_run:
            LOG.info(f"[fakeroot][dry-run] cmd={cmd} cwd={cwd} sandbox={sandbox_dir} shell={shell}")
            return subprocess.CompletedProcess(args=[cmd], returncode=0)

        final_cmd = self._construct_command(cmd, cwd, sandbox_dir, shell)
        if self.debug:
            LOG.debug(f"[fakeroot] constructed: {' '.join(shlex.quote(x) for x in final_cmd)}")
        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)

        if stream_output:
            p = subprocess.Popen(final_cmd, cwd=cwd, env=proc_env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            # stream
            for line in p.stdout:
                LOG.info(f"[fakeroot][stdout] {line.rstrip()}")
            for line in p.stderr:
                LOG.error(f"[fakeroot][stderr] {line.rstrip()}")
            rc = p.wait()
            completed = subprocess.CompletedProcess(final_cmd, rc)
        else:
            completed = subprocess.run(final_cmd, cwd=cwd, env=proc_env,
                                       capture_output=True, text=True, timeout=self.timeout)
            if self.debug:
                LOG.debug(f"[fakeroot][stdout] {completed.stdout}")
                LOG.debug(f"[fakeroot][stderr] {completed.stderr}")

        if check and completed.returncode != 0:
            raise subprocess.CalledProcessError(completed.returncode, final_cmd, output=completed.stdout, stderr=completed.stderr)
        return completed

    def run_and_check(self, *args, **kwargs) -> subprocess.CompletedProcess:
        return self.run(*args, check=True, **kwargs)

    def install_into_sandbox(self,
                              install_cmd: Union[str, List[str]],
                              build_dir: Optional[str],
                              sandbox_dir: str,
                              shell: bool = False,
                              extra_env: Optional[Dict[str, str]] = None,
                              stream_output: bool = False) -> subprocess.CompletedProcess:
        """ Run install command targeting sandbox_dir.
            Detect DESTDIR / --root / --prefix if provided, else use DESTDIR.
        """
        safe_makedirs(sandbox_dir)
        cwd = build_dir or sandbox_dir

        use_cmd = install_cmd
        need_destdir = True
        # detectar se install_cmd já contém DESTDIR= ou --root= ou prefix com sandbox root
        cmd_str = install_cmd if isinstance(install_cmd, str) else _quote_list(install_cmd)
        lower = cmd_str.lower()
        if "destdir=" in lower or "--root=" in lower:
            need_destdir = False
        if ("--prefix=" in lower) and sandbox_dir in cmd_str:
            need_destdir = False

        if need_destdir:
            if isinstance(use_cmd, str):
                use_cmd = f"{install_cmd} DESTDIR={sandbox_dir}"
            else:
                use_cmd = list(use_cmd) + ["DESTDIR=" + sandbox_dir]

        return self.run_and_check(use_cmd, cwd=cwd, env=extra_env, sandbox_dir=sandbox_dir, shell=shell, stream_output=stream_output)

    def snapshot_sandbox_metadata(self,
                                   sandbox_dir: str,
                                   skip_patterns: Optional[List[str]] = None) -> List[Dict[str, Any]]:
        root = Path(sandbox_dir).resolve()
        if not root.exists():
            raise FileNotFoundError(f"Sandbox dir not found: {sandbox_dir}")
        skip = skip_patterns or []
        out: List[Dict[str, Any]] = []
        for p in root.rglob("*"):
            rel = p.relative_to(root)
            rel_str = str(rel)
            # ignorar padrões
            if any(pat in rel_str for pat in skip):
                continue
            try:
                st = p.lstat()
                is_link = p.is_symlink()
                target = None
                if is_link:
                    try:
                        target = os.readlink(str(p))
                    except Exception:
                        target = None
                meta = FileMeta(path=rel_str,
                                is_dir=p.is_dir(),
                                is_symlink=is_link,
                                target=target,
                                mode=(st.st_mode & 0o7777),
                                uid=st.st_uid,
                                gid=st.st_gid,
                                size=st.st_size,
                                mtime=st.st_mtime)
                out.append(meta.to_dict())
            except Exception as e:
                LOG.warning(f"[fakeroot] snapshot skipping {rel_str}: {e}")
                continue
        return out

    def write_metadata_json(self,
                            sandbox_dir: str,
                            dest_file: str,
                            skip_patterns: Optional[List[str]] = None) -> None:
        data = self.snapshot_sandbox_metadata(sandbox_dir, skip_patterns=skip_patterns)
        safe_makedirs(Path(dest_file).parent)
        with open(dest_file, "w", encoding="utf-8") as f:
            json.dump({"generated_at": time.time(), "entries": data}, f, indent=2)
        LOG.info(f"Sandbox metadata written to {dest_file}")

    def normalize_permissions(self,
                              sandbox_dir: str,
                              default_file_mode: int = 0o644,
                              default_dir_mode: int = 0o755) -> None:
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
                # manter bits executáveis existentes
                exec_bits = st.st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                new_mode = (st.st_mode & ~0o777) | (desired & 0o777) | exec_bits
                p.chmod(new_mode)
            except Exception:
                continue

    # ---------------- CLI ----------------
    def _cli_main():
        import argparse
        parser = argparse.ArgumentParser(prog="pyport-fakeroot", description="Test fakeroot module")
        parser.add_argument("--sandbox", "-s", required=True, help="Sandbox path (criado se necessário)")
        parser.add_argument("--no-bwrap", dest="use_bwrap", action="store_false", help="não usar bwrap")
        parser.add_argument("--no-fakeroot", dest="use_fakeroot", action="store_false", help="não usar fakeroot")
        parser.add_argument("--stream", action="store_true", help="streaming de output")
        parser.add_argument("--debug", action="store_true", help="modo debug")
        parser.add_argument("--dry-run", action="store_true")
        parser.add_argument("--snapshot", help="arquivo JSON para gravar metadata")
        parser.add_argument("cmd", nargs="*", help="Comando para executar no sandbox")
        args = parser.parse_args()

        runner = Fakerunner(use_bwrap=args.use_bwrap, use_fakeroot=args.use_fakeroot, debug=args.debug, dry_run=args.dry_run)
        safe_makedirs(args.sandbox)
        if args.cmd:
            cmd = " ".join(args.cmd)
            cp = runner.run_and_check(cmd, cwd=None, sandbox_dir=args.sandbox, shell=True, stream_output=args.stream)
            print(f"Command exit: {cp.returncode}")
        if args.snapshot:
            runner.write_metadata_json(args.sandbox, args.snapshot)
            print(f"Snapshot salva em {args.snapshot}")

if __name__ == "__main__":
    _cli_main()
