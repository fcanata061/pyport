#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
toolchain.py - criação e gerenciamento de toolchains e chroots em /mnt/tools

Funcionalidades:
 - criar diretório de toolchain em /mnt/tools/<name>
 - bootstrap Debian/Ubuntu via debootstrap (se debootstrap disponível)
 - criar toolchain "minimal" copiando BusyBox e libs do host
 - preparar chroot: montar proc/sys/dev, copiar /etc/resolv.conf, montar dev/pts
 - desmontar chroot e limpar
 - snapshot metadata (permissions, files list)
 - utilitários: list, remove, export tarball
 - CLI: create, prepare-chroot, unprepare-chroot, list, remove, snapshot, export
"""

from __future__ import annotations
import os
import sys
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import Optional, Dict, Any, List

# configuration
TOOLS_ROOT = Path("/mnt/tools")
LOG_DIR = Path("/pyport/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

def _log(msg: str):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    line = f"[toolchain] [{ts}] {msg}"
    print(line)
    try:
        with open(LOG_DIR / "toolchain.log", "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def _run(cmd: List[str], check=True, capture=False, env=None):
    _log("running: " + " ".join(cmd))
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, env=env)

def ensure_root():
    if os.geteuid() != 0:
        raise PermissionError("Operation requires root privileges. Re-run as root or with sudo.")

# ---------- core functions ---------------------------------------------------

def create_toolchain_dir(name: str, mode: int = 0o755) -> Path:
    """Create the base toolchain directory /mnt/tools/<name>"""
    TOOLS_ROOT.mkdir(parents=True, exist_ok=True)
    dest = TOOLS_ROOT / name
    dest.mkdir(parents=True, exist_ok=True)
    dest.chmod(mode)
    _log(f"created toolchain directory {dest}")
    return dest

def remove_toolchain(name: str, force: bool = False) -> bool:
    """Remove a toolchain directory. If force, remove even if non-empty."""
    dest = TOOLS_ROOT / name
    if not dest.exists():
        _log(f"toolchain {name} not found")
        return False
    if not force and any(dest.iterdir()):
        _log(f"toolchain {name} not empty; use force=True to remove")
        return False
    try:
        shutil.rmtree(dest)
        _log(f"removed toolchain {name}")
        return True
    except Exception as e:
        _log(f"failed to remove {name}: {e}")
        return False

def list_toolchains() -> List[Dict[str,Any]]:
    out = []
    if not TOOLS_ROOT.exists():
        return out
    for p in sorted(TOOLS_ROOT.iterdir()):
        if p.is_dir():
            stat = p.stat()
            out.append({
                "name": p.name,
                "path": str(p),
                "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(stat.st_mtime)),
                "size_bytes": _du_bytes(p)
            })
    return out

def _du_bytes(path: Path) -> int:
    total = 0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except Exception:
            pass
    return total

# ---------- bootstrapping methods -------------------------------------------

def bootstrap_debian(name: str, suite: str = "stable", mirror: str = "http://deb.debian.org/debian", arch: str = "amd64", extra_args: Optional[List[str]] = None) -> Path:
    """
    Bootstrap a Debian/Ubuntu filesystem via debootstrap.
    Requires 'debootstrap' installed on host.
    """
    ensure_root()
    dest = create_toolchain_dir(name)
    if not shutil.which("debootstrap"):
        raise RuntimeError("debootstrap not found on host; install debootstrap to use this function")
    args = ["debootstrap", "--variant=minbase", "--arch", arch]
    if extra_args:
        args += extra_args
    args += [suite, str(dest), mirror]
    _log("running debootstrap ...")
    _run(args, check=True)
    _log(f"debootstrap finished for {name} at {dest}")
    return dest

def bootstrap_minimal_busybox(name: str, copy_binaries: Optional[List[str]] = None) -> Path:
    """
    Create a minimal toolchain by copying busybox and necessary libraries from host.
    This is a pragmatic minimal fallback when debootstrap not available.
    """
    ensure_root()
    dest = create_toolchain_dir(name)
    # find busybox
    busy = shutil.which("busybox")
    if not busy:
        raise RuntimeError("busybox not found on host; install busybox or use debootstrap")
    _log(f"using busybox at {busy}")
    bin_dir = dest / "bin"
    lib_dir = dest / "lib"
    lib64_dir = dest / "lib64"
    for d in (bin_dir, lib_dir, lib64_dir):
        d.mkdir(parents=True, exist_ok=True)
    # copy busybox
    shutil.copy2(busy, bin_dir / "busybox")
    os.chmod(bin_dir / "busybox", 0o755)
    # create symlinks for common utilities provided by busybox
    utilities = ["sh","ls","cp","mv","rm","mkdir","mount","umount","cat","echo","ln","mountpoint"]
    for u in utilities:
        link = bin_dir / u
        try:
            if link.exists():
                link.unlink()
        except Exception:
            pass
        try:
            os.symlink("busybox", link)
        except Exception:
            pass
    # copy minimal libraries required by busybox (ldd)
    try:
        out = subprocess.check_output(["ldd", busy], text=True)
        for line in out.splitlines():
            parts = line.strip().split("=>")
            if len(parts) == 2:
                libpath = parts[1].strip().split()[0]
                if os.path.exists(libpath):
                    target = lib_dir / Path(libpath).name
                    try:
                        shutil.copy2(libpath, target)
                    except Exception:
                        try:
                            shutil.copy2(libpath, lib64_dir / Path(libpath).name)
                        except Exception:
                            pass
    except Exception:
        _log("ldd failed or not available; libraries may be missing")
    _log(f"minimal busybox toolchain created at {dest}")
    return dest

def bootstrap_from_tarball(name: str, tarball: Path) -> Path:
    """Extract a prepared toolchain tarball into /mnt/tools/<name>"""
    ensure_root()
    dest = create_toolchain_dir(name)
    if not tarball.exists():
        raise FileNotFoundError(tarball)
    _log(f"extracting {tarball} into {dest}")
    subprocess.run(["tar","-xf", str(tarball), "-C", str(dest)], check=True)
    return dest

# ---------- chroot preparation / mounting -----------------------------------

def prepare_chroot(root: Path, mounts: Optional[Dict[str,str]] = None, copy_resolv: bool = True, bind_ro: Optional[List[Path]] = None):
    """
    Prepare a chroot environment: bind-mount /proc, /sys, /dev, optionally bind host paths read-only.
    Copies /etc/resolv.conf to chroot.
    Requires root privileges.
    """
    ensure_root()
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(root)
    _log(f"preparing chroot at {root}")
    # mounts map: host->chroot-relpath
    default = {"/proc": str(root / "proc"), "/sys": str(root / "sys"), "/dev": str(root / "dev")}
    mounts = mounts or default
    # create mount points
    for host_src, target in mounts.items():
        tgt = Path(target)
        tgt.mkdir(parents=True, exist_ok=True)
        # bind mount
        _run(["mount","--bind", host_src, str(tgt)], check=True)
    # mount devpts
    dev_pt = root / "dev" / "pts"
    dev_pt.mkdir(parents=True, exist_ok=True)
    try:
        _run(["mount","-t","devpts","devpts", str(dev_pt)], check=True)
    except Exception:
        _log("could not mount devpts (may already be mounted)")

    # bind read-only chroot-ro directories if requested
    if bind_ro:
        for hostpath in bind_ro:
            hostpath = Path(hostpath)
            if hostpath.exists():
                target = root / hostpath.relative_to("/")
                target.parent.mkdir(parents=True, exist_ok=True)
                _run(["mount","--bind", str(hostpath), str(target)], check=True)
                _run(["mount","-o","remount,ro,bind", str(target)], check=True)

    # copy resolv.conf
    if copy_resolv:
        try:
            shutil.copy2("/etc/resolv.conf", str(root / "etc" / "resolv.conf"))
        except Exception:
            _log("failed copying /etc/resolv.conf")

    _log(f"chroot prepared at {root}")

def unprepare_chroot(root: Path, bind_ro: Optional[List[Path]] = None):
    """
    Undo what prepare_chroot did: unmount devpts, proc, sys, dev and any bind_ro
    """
    ensure_root()
    root = Path(root)
    _log(f"unpreparing chroot at {root}")
    # unmount in reverse order to be safe
    try:
        # try common mounts
        for p in ["dev/pts", "proc", "sys", "dev"]:
            m = root / p
            if m.exists():
                try:
                    _run(["umount", "-l", str(m)], check=False)
                except Exception:
                    pass
        # unmount bind_ro if any
        if bind_ro:
            for hostpath in bind_ro:
                target = root / Path(hostpath).relative_to("/")
                try:
                    _run(["umount", "-l", str(target)], check=False)
                except Exception:
                    pass
    except Exception as e:
        _log(f"error during unprepare: {e}")
    _log(f"chroot unprepared at {root}")

# ---------- snapshot metadata ------------------------------------------------

def snapshot_metadata(root: Path, out_file: Optional[Path] = None, skip: Optional[List[str]] = None) -> List[Dict[str,Any]]:
    """Walk root tree and produce a metadata snapshot (paths, mode, uid, gid, size, mtime)"""
    root = Path(root)
    if not root.exists():
        raise FileNotFoundError(root)
    skip = skip or []
    out = []
    for p in root.rglob("*"):
        try:
            rel = p.relative_to(root)
            if any(str(rel).startswith(s) for s in skip):
                continue
            st = p.lstat()
            entry = {
                "path": str(rel),
                "is_dir": p.is_dir(),
                "is_symlink": p.is_symlink(),
                "mode": oct(st.st_mode & 0o7777),
                "uid": st.st_uid,
                "gid": st.st_gid,
                "size": st.st_size,
                "mtime": st.st_mtime
            }
            if p.is_symlink():
                entry["target"] = os.readlink(str(p))
            out.append(entry)
        except Exception:
            continue
    if out_file:
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text(json.dumps({"generated_at": time.time(), "entries": out}, indent=2))
    return out

# ---------- export/import ---------------------------------------------------

def export_toolchain_tar(name: str, dest_tar: Path):
    """Create a tarball of /mnt/tools/<name> (careful: can be large)"""
    src = TOOLS_ROOT / name
    if not src.exists():
        raise FileNotFoundError(src)
    dest_tar.parent.mkdir(parents=True, exist_ok=True)
    _log(f"exporting {src} to {dest_tar}")
    _run(["tar","-C", str(TOOLS_ROOT), "-czf", str(dest_tar), name], check=True)
    _log(f"exported {dest_tar}")

# ---------- CLI --------------------------------------------------------------

def _cli():
    import argparse
    parser = argparse.ArgumentParser(prog="pyport-toolchain", description="Manage /mnt/tools toolchains and chroots")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create", help="create toolchain dir")
    p_create.add_argument("name")
    p_create.add_argument("--method", choices=["debootstrap","busybox","tarball"], default="busybox")
    p_create.add_argument("--suite", default="stable", help="debootstrap suite (if method=debootstrap)")
    p_create.add_argument("--mirror", default="http://deb.debian.org/debian", help="debootstrap mirror")
    p_create.add_argument("--tarball", help="tarball path (if method=tarball)")
    p_create.add_argument("--arch", default="amd64")
    p_create.add_argument("--force", action="store_true")

    p_list = sub.add_parser("list", help="list toolchains")

    p_remove = sub.add_parser("remove", help="remove toolchain")
    p_remove.add_argument("name")
    p_remove.add_argument("--force", action="store_true")

    p_prepare = sub.add_parser("prepare-chroot", help="prepare chroot mounts for toolchain")
    p_prepare.add_argument("name")
    p_prepare.add_argument("--no-resolv", dest="copy_resolv", action="store_false")
    p_prepare.add_argument("--bind-ro", nargs="*", help="host paths to bind read-only into chroot")

    p_unprepare = sub.add_parser("unprepare-chroot", help="undo chroot mounts")
    p_unprepare.add_argument("name")
    p_export = sub.add_parser("export", help="export toolchain to tar.gz")
    p_export.add_argument("name")
    p_export.add_argument("dest")

    p_snapshot = sub.add_parser("snapshot", help="snapshot metadata for toolchain")
    p_snapshot.add_argument("name")
    p_snapshot.add_argument("--out", default=None)

    args = parser.parse_args()

    if args.cmd == "create":
        try:
            if args.method == "debootstrap":
                ensure_root()
                bootstrap_debian(args.name, suite=args.suite, mirror=args.mirror, arch=args.arch)
            elif args.method == "tarball":
                if not args.tarball:
                    print("tarball required for method=tarball")
                    sys.exit(2)
                bootstrap_from_tarball(args.name, Path(args.tarball))
            else:
                ensure_root()
                bootstrap_minimal_busybox(args.name)
            print("done")
        except Exception as e:
            print("error:", e)
            sys.exit(1)

    elif args.cmd == "list":
        for t in list_toolchains():
            print(f"{t['name']:20} {t['path']:40} size={t['size_bytes']} mtime={t['mtime']}")

    elif args.cmd == "remove":
        ok = remove_toolchain(args.name, force=args.force)
        print("removed" if ok else "failed")

    elif args.cmd == "prepare-chroot":
        try:
            root = TOOLS_ROOT / args.name
            if not root.exists():
                print("toolchain not found:", args.name); sys.exit(2)
            bind_ro = [Path(x) for x in args.bind_ro] if args.bind_ro else None
            prepare_chroot(root, copy_resolv=args.copy_resolv, bind_ro=bind_ro)
            print("prepared")
        except Exception as e:
            print("error:", e); sys.exit(1)

    elif args.cmd == "unprepare-chroot":
        try:
            root = TOOLS_ROOT / args.name
            unprepare_chroot(root)
            print("unprepared")
        except Exception as e:
            print("error:", e); sys.exit(1)

    elif args.cmd == "export":
        try:
            export_toolchain_tar(args.name, Path(args.dest))
            print("exported to", args.dest)
        except Exception as e:
            print("error:", e); sys.exit(1)

    elif args.cmd == "snapshot":
        try:
            root = TOOLS_ROOT / args.name
            if not root.exists():
                print("toolchain not found:", args.name); sys.exit(2)
            outp = Path(args.out) if args.out else (LOG_DIR / f"{args.name}-snapshot.json")
            snap = snapshot_metadata(root, out_file=outp)
            print("snapshot written to", outp)
        except Exception as e:
            print("error:", e); sys.exit(1)

if __name__ == "__main__":
    _cli()
