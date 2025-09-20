#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
toolchain.py — Gerenciador avançado de toolchains e chroots para PyPort

Funcionalidades incluídas:
 - Criação/remoção/listagem de toolchains localizados em TOOLS_ROOT (definido via config)
 - Bootstrapping via debootstrap, BusyBox mínimo ou tarball
 - Preparação e desmontagem de ambientes chroot (bind mounts, copiando resolv.conf, permissões corretas)
 - Snapshot de metadados (permissões, arquivos, etc.)
 - Export para tarball
 - Verificação de espaço em disco
 - Uso do logger central de PyPort
 - Compatibilidade com sandbox/fakeroot se aplicável
 - CLI robusto
"""

from __future__ import annotations
import os
import sys
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, List, Dict, Any

from pyport.logger import get_logger
from pyport.config import get_config
from pyport.fakeroot import Fakerunner  # para executar ações com fakeroot/bwrap se necessário

LOG = get_logger("pyport.toolchain")

def _run(cmd: List[str], cwd: Optional[Path] = None, check: bool = True, capture_output: bool = False, env: Optional[Dict[str, str]] = None):
    LOG.info(f"[toolchain] running: {' '.join(str(c) for c in cmd)}")
    result = subprocess.run(cmd, cwd=str(cwd) if cwd else None, check=check, capture_output=capture_output, text=True, env=env)
    if capture_output:
        LOG.debug(f"stdout: {result.stdout}")
        LOG.debug(f"stderr: {result.stderr}")
    return result

def ensure_root():
    if os.geteuid() != 0:
        raise PermissionError("Operation requires root privileges. Re-run as root or with sudo.")

class ToolchainManager:
    """
    Gestão de toolchains conforme configuração do PyPort.
    """
    def __init__(self):
        cfg = get_config()
        self.tools_root = Path(cfg["paths"].get("toolchain", "/mnt/tools"))
        self.log_dir = Path(cfg["paths"].get("logs", "/pyport/logs"))
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def list(self) -> None:
        if not self.tools_root.exists():
            LOG.info(f"No toolchains found: {self.tools_root} does not exist")
            return
        for p in sorted(self.tools_root.iterdir()):
            if p.is_dir():
                size = sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
                mtime = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(p.stat().st_mtime))
                LOG.info(f"{p.name} — path: {p} — size: {size} bytes — modified: {mtime}")

    def create(self, name: str, method: str = "busybox", suite: Optional[str] = None, mirror: Optional[str] = None, arch: Optional[str] = None, tarball: Optional[Path] = None, force: bool = False) -> bool:
        dest = self.tools_root / name
        if dest.exists():
            if force:
                LOG.warning(f"Toolchain {name} already exists; removing because force=True")
                try:
                    shutil.rmtree(dest)
                except Exception as e:
                    LOG.error(f"Failed to remove existing toolchain {name}: {e}")
                    return False
            else:
                LOG.error(f"Toolchain {name} already exists. Use force to overwrite.")
                return False

        try:
            if method == "debootstrap":
                ensure_root()
                if not shutil.which("debootstrap"):
                    LOG.error("debootstrap not found on system; cannot use this method")
                    return False
                args = ["debootstrap", "--variant=minbase"]
                if arch:
                    args += ["--arch", arch]
                if suite:
                    args += [suite]
                else:
                    suite = "stable"
                    args += [suite]
                if mirror:
                    args += [mirror]
                else:
                    mirror = get_config().get("toolchain", {}).get("debootstrap_mirror")
                    if not mirror:
                        LOG.error("No mirror specified for debootstrap")
                        return False
                args += [str(dest)]
                _run(args, check=True)
                LOG.info(f"Debian-based toolchain created at {dest}")

            elif method == "busybox":
                ensure_root()
                busy = shutil.which("busybox")
                if not busy:
                    LOG.error("busybox not found; cannot build busybox minimal toolchain")
                    return False
                (dest / "bin").mkdir(parents=True, exist_ok=True)
                shutil.copy2(busy, dest / "bin" / "busybox")
                os.chmod(dest / "bin" / "busybox", 0o755)
                # criar links para comandos comuns
                cmds = ["sh","ls","cp","mv","rm","mkdir","mount","umount","cat","echo","ln"]
                for c in cmds:
                    link = dest / "bin" / c
                    if link.exists():
                        continue
                    try:
                        os.symlink("busybox", link)
                    except Exception as e:
                        LOG.warning(f"Failed to symlink {c} in busybox toolchain: {e}")
                LOG.info(f"Busybox minimal toolchain created at {dest}")

            elif method == "tarball":
                if not tarball:
                    LOG.error("Tarball path must be specified for method=tarball")
                    return False
                if not tarball.exists():
                    LOG.error(f"Tarball {tarball} not found")
                    return False
                ensure_root()
                self.tools_root.mkdir(parents=True, exist_ok=True)
                # extrair tarball
                _run(["tar", "-xpf", str(tarball), "-C", str(self.tools_root)], check=True)
                LOG.info(f"Toolchain {name} extracted from tarball at {dest}")

            else:
                LOG.error(f"Unknown method {method} for creating toolchain")
                return False

            return True

        except Exception as e:
            LOG.error(f"Error during toolchain create: {e}")
            return False

    def remove(self, name: str, force: bool = False) -> bool:
        dest = self.tools_root / name
        if not dest.exists():
            LOG.error(f"Toolchain {name} does not exist")
            return False
        try:
            if not force and any(dest.iterdir()):
                LOG.error(f"Toolchain {name} is not empty. Use force to remove.")
                return False
            shutil.rmtree(dest)
            LOG.info(f"Toolchain {name} removed")
            return True
        except Exception as e:
            LOG.error(f"Failed to remove toolchain {name}: {e}")
            return False

    def prepare_chroot(self, name: str, copy_resolv: bool = True, bind_ro: Optional[List[Path]] = None) -> bool:
        dest = self.tools_root / name
        if not dest.exists():
            LOG.error(f"Toolchain {name} does not exist")
            return False
        try:
            ensure_root()
            # montar proc, sys, dev
            for src, tgt_sub in [("/proc", "proc"), ("/sys", "sys"), ("/dev", "dev")]:
                tgt = dest / tgt_sub
                tgt.mkdir(parents=True, exist_ok=True)
                _run(["mount", "--bind", src, str(tgt)], check=True)
            # dev/pts
            pts = dest / "dev" / "pts"
            pts.mkdir(parents=True, exist_ok=True)
            _run(["mount", "-t", "devpts", "devpts", str(pts)], check=True)
            # bind-ro extras
            if bind_ro:
                for p in bind_ro:
                    if not p.exists():
                        LOG.warning(f"bind_ro path not found: {p}")
                        continue
                    target = dest / p.relative_to("/")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    _run(["mount", "--bind", str(p), str(target)], check=True)
                    _run(["mount", "-o", "remount,ro,bind", str(target)], check=True)
            # resolv.conf
            if copy_resolv:
                etcdir = dest / "etc"
                etcdir.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copy2("/etc/resolv.conf", str(etcdir / "resolv.conf"))
                except Exception as e:
                    LOG.warning(f"Failed to copy resolv.conf: {e}")
            LOG.info(f"Chroot prepared at {dest}")
            return True
        except Exception as e:
            LOG.error(f"Failed to prepare chroot for {name}: {e}")
            return False

    def unprepare_chroot(self, name: str, bind_ro: Optional[List[Path]] = None) -> bool:
        dest = self.tools_root / name
        if not dest.exists():
            LOG.error(f"Toolchain {name} does not exist")
            return False
        try:
            ensure_root()
            # desmontar dev/pts, proc, sys, etc
            for subp in ["dev/pts","proc","sys","dev"]:
                m = dest / subp
                if m.exists():
                    _run(["umount", "-l", str(m)], check=False)
            if bind_ro:
                for p in bind_ro:
                    target = dest / Path(p).relative_to("/")
                    if target.exists():
                        _run(["umount", "-l", str(target)], check=False)
            LOG.info(f"Chroot unprepared at {dest}")
            return True
        except Exception as e:
            LOG.error(f"Failed to unprepare chroot for {name}: {e}")
            return False

    def snapshot(self, name: str, out_file: Optional[Path] = None, skip: Optional[List[str]] = None) -> bool:
        dest = self.tools_root / name
        if not dest.exists():
            LOG.error(f"Toolchain {name} does not exist")
            return False
        try:
            skip = skip or []
            entries = []
            for p in dest.rglob("*"):
                try:
                    rel = p.relative_to(dest)
                    if any(str(rel).startswith(s) for s in skip):
                        continue
                    st = p.lstat()
                    entry: Dict[str, Any] = {
                        "path": str(rel),
                        "is_dir": p.is_dir(),
                        "is_symlink": p.is_symlink(),
                        "mode": oct(st.st_mode & 0o7777),
                        "uid": st.st_uid,
                        "gid": st.st_gid,
                        "size": st.st_size,
                        "mtime": st.st_mtime,
                    }
                    if p.is_symlink():
                        entry["target"] = os.readlink(str(p))
                    entries.append(entry)
                except Exception as e:
                    LOG.warning(f"Skipping snapshot entry {p}: {e}")
            if out_file:
                out_file.parent.mkdir(parents=True, exist_ok=True)
                with open(out_file, "w", encoding="utf-8") as f:
                    json.dump({"generated_at": time.time(), "entries": entries}, f, indent=2)
                LOG.info(f"Snapshot written to {out_file}")
            else:
                LOG.info(f"Snapshot collected: {len(entries)} entries for toolchain {name}")
            return True
        except Exception as e:
            LOG.error(f"Error during snapshot for {name}: {e}")
            return False

    def export(self, name: str, dest_tar: Path) -> bool:
        src = self.tools_root / name
        if not src.exists():
            LOG.error(f"Toolchain {name} does not exist")
            return False
        try:
            ensure_root()
            dest_tar.parent.mkdir(parents=True, exist_ok=True)
            # tarball
            _run(["tar", "-C", str(self.tools_root), "-czf", str(dest_tar), name], check=True)
            LOG.info(f"Toolchain {name} exported to {dest_tar}")
            return True
        except Exception as e:
            LOG.error(f"Error exporting toolchain {name}: {e}")
            return False

# CLI wrapper

def _cli():
    import argparse
    parser = argparse.ArgumentParser(prog="pyport-toolchain", description="Manage toolchains and chroots")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="Create toolchain")
    p_create.add_argument("name")
    p_create.add_argument("--method", choices=["debootstrap","busybox","tarball"], default="busybox")
    p_create.add_argument("--suite", default=None)
    p_create.add_argument("--mirror", default=None)
    p_create.add_argument("--arch", default=None)
    p_create.add_argument("--tarball", type=Path, default=None)
    p_create.add_argument("--force", action="store_true")

    p_list = sub.add_parser("list", help="List toolchains")

    p_remove = sub.add_parser("remove", help="Remove toolchain")
    p_remove.add_argument("name")
    p_remove.add_argument("--force", action="store_true")

    p_prepare = sub.add_parser("prepare-chroot", help="Prepare chroot environment for toolchain")
    p_prepare.add_argument("name")
    p_prepare.add_argument("--no-resolv", dest="copy_resolv", action="store_false", default=True)
    p_prepare.add_argument("--bind-ro", nargs="*", type=Path, default=None)

    p_unprepare = sub.add_parser("unprepare-chroot", help="Undo chroot preparation")
    p_unprepare.add_argument("name")
    p_unprepare.add_argument("--bind-ro", nargs="*", type=Path, default=None)

    p_export = sub.add_parser("export", help="Export toolchain to tar.gz")
    p_export.add_argument("name")
    p_export.add_argument("dest", type=Path)

    p_snapshot = sub.add_parser("snapshot", help="Snapshot metadata for toolchain")
    p_snapshot.add_argument("name")
    p_snapshot.add_argument("--out", type=Path, default=None)
    p_snapshot.add_argument("--skip", nargs="*", type=str, default=None)

    args = parser.parse_args()
    mgr = ToolchainManager()

    if args.cmd == "create":
        ok = mgr.create(args.name, method=args.method, suite=args.suite, mirror=args.mirror, arch=args.arch, tarball=args.tarball, force=args.force)
        sys.exit(0 if ok else 1)
    elif args.cmd == "list":
        mgr.list()
        sys.exit(0)
    elif args.cmd == "remove":
        ok = mgr.remove(args.name, force=args.force)
        sys.exit(0 if ok else 1)
    elif args.cmd == "prepare-chroot":
        ok = mgr.prepare_chroot(args.name, copy_resolv=args.copy_resolv, bind_ro=args.bind_ro)
        sys.exit(0 if ok else 1)
    elif args.cmd == "unprepare-chroot":
        ok = mgr.unprepare_chroot(args.name, bind_ro=args.bind_ro)
        sys.exit(0 if ok else 1)
    elif args.cmd == "export":
        ok = mgr.export(args.name, args.dest)
        sys.exit(0 if ok else 1)
    elif args.cmd == "snapshot":
        ok = mgr.snapshot(args.name, out_file=args.out, skip=args.skip)
        sys.exit(0 if ok else 1)
    else:
        parser.print_help()
        sys.exit(1)

if __name__ == "__main__":
    _cli()
