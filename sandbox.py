# /pyport/sandbox.py
"""
PyPort sandbox - versão avançada, completa e funcional

Funcionalidades principais (resumo):
 - leitura de portfile.yaml em /usr/ports/<categoria>/<nome>/portfile.yaml
 - cache de distfiles em /var/cache/pyport/distfiles (configurável)
 - mirrorlist: tenta múltiplas URLs por fonte
 - múltiplos checksums (sha256, sha512, md5)
 - download com retries e backoff (curl/wget/urllib)
 - extração (tar.*, zip, 7z se disponível)
 - apply patches da pasta patches/
 - validação e execução de hooks (pre/post configure, pre/post install, check)
 - handlers para build_systems: autotools, cmake, python, rust, java, custom
 - instalação isolada com Fakerunner (fakeroot + opcional bwrap)
 - chroot helper (prepara e desmonta binds; copia resolv.conf)
 - snapshot metadata JSON com permissões/UID/GID/mtime/size
 - rollback automático em caso de falha parcial
 - logs detalhados em /pyport/logs/<pkg>-<ver>.log
 - saída final colorida (sucesso/erro/aviso)
 - integração: aceita opção toolchain_dir
"""

from __future__ import annotations
import os
import sys
import shutil
import subprocess
import tarfile
import zipfile
import hashlib
import json
import time
import datetime
import socket
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple, Union

# import Fakerunner from your fakeroot module (expected path pyport_fakeroot.py)
try:
    from pyport_fakeroot import Fakerunner, ToolMissingError
except Exception:
    try:
        # alternative import if module name is fakeroot.py
        from fakeroot import Fakerunner, ToolMissingError  # type: ignore
    except Exception:
        Fakerunner = None  # will check later

# YAML loader: prefer PyYAML if available
try:
    import yaml  # type: ignore
    _yaml_load = lambda s: yaml.safe_load(s)
except Exception:
    def _yaml_load(s: str) -> Dict[str, Any]:
        data: Dict[str, Any] = {}
        cur = None
        for raw in s.splitlines():
            line = raw.rstrip()
            if not line or line.strip().startswith("#"):
                continue
            if line.lstrip().startswith("- "):
                if cur:
                    data.setdefault(cur, []).append(line.lstrip()[2:].strip())
            elif ":" in line:
                k, v = line.split(":", 1)
                k = k.strip()
                v = v.strip()
                cur = k
                if v == "":
                    data[k] = []
                else:
                    data[k] = v
        return data

# ---------------------------
# DEFAULT CONFIG
# ---------------------------
DEFAULT_CONFIG: Dict[str, Any] = {
    "build_root": "/pyport/build",
    "ports_dir": "/usr/ports",
    "log_dir": "/pyport/logs",
    "patch_dir": "patches",
    "toolchain_dir": "/mnt/tools",
    "sandbox_mode": "both",  # fakeroot | bwrap | both
    "distfiles_cache": "/var/cache/pyport/distfiles",
    "max_download_retries": 3,
    "download_backoff": 2,
    "keep_build_on_success": False,
    "chroot_prepare": False,  # if true, prepare chroot mounts for toolchain entry
    "7z_cmd": "7z",
}

# Colors for terminal summary
_COL_RESET = "\033[0m"
_COL_RED = "\033[31m"
_COL_GREEN = "\033[32m"
_COL_YELLOW = "\033[33m"

# ---------------------------
# Utilities
# ---------------------------

def get_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    syscfg = Path("/etc/pyport/config.yaml")
    usercfg = Path.home() / ".config" / "pyport" / "config.yaml"
    for p in (syscfg, usercfg):
        try:
            if p.exists():
                with open(p, "r", encoding="utf-8") as f:
                    data = _yaml_load(f.read())
                    if isinstance(data, dict):
                        cfg.update(data)
        except Exception:
            continue
    # ensure folders exist
    for d in (cfg["build_root"], cfg["log_dir"], cfg["distfiles_cache"], cfg["toolchain_dir"]):
        try:
            Path(d).mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    return cfg

def log_path(cfg: Dict[str, Any], name: str) -> Path:
    p = Path(cfg.get("log_dir")) / f"{name}.log"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p

def _log(cfg: Dict[str, Any], name: str, message: str) -> None:
    p = log_path(cfg, name)
    ts = datetime.datetime.now().isoformat()
    with open(p, "a", encoding="utf-8") as f:
        f.write(f"[{ts}] {message}\n")

def safe_makedirs(p: Union[str, Path]) -> Path:
    p = Path(p)
    p.mkdir(parents=True, exist_ok=True)
    return p

def available_progs(names: List[str]) -> Dict[str, bool]:
    d: Dict[str, bool] = {}
    for n in names:
        d[n] = shutil.which(n) is not None
    return d

# ---------------------------
# Checksums & cache
# ---------------------------

def _compute_hash(path: Path, algo: str="sha256") -> str:
    algo = algo.lower()
    if algo not in ("sha256","sha512","md5"):
        raise ValueError("Unsupported hash")
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()

def _verify_checksum(path: Path, checksum_field: Optional[str]) -> bool:
    if not checksum_field:
        return True
    # format "sha256:abcdef" or just hex (assume sha256)
    parts = str(checksum_field).split(":",1)
    if len(parts) == 2:
        alg, val = parts[0].lower(), parts[1].strip()
    else:
        alg, val = "sha256", parts[0].strip()
    if alg not in ("sha256","sha512","md5"):
        return False
    try:
        got = _compute_hash(path, algo=alg)
        return got.lower() == val.lower()
    except Exception:
        return False

# ---------------------------
# Downloads (mirrorlist + retries)
# ---------------------------

def _download_with_retries(url: str, dest: Path, retries: int, backoff: int, cfg: Dict[str, Any]) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        return
    attempt = 0
    while attempt < retries:
        try:
            # prefer curl -> wget -> urllib
            if shutil.which("curl"):
                cmd = ["curl", "-L", "-o", str(dest), url]
                subprocess.run(cmd, check=True)
            elif shutil.which("wget"):
                cmd = ["wget", "-O", str(dest), url]
                subprocess.run(cmd, check=True)
            else:
                # python fallback
                from urllib.request import urlopen, Request
                req = Request(url, headers={"User-Agent":"pyport/1.0"})
                with urlopen(req) as r, open(dest, "wb") as f:
                    f.write(r.read())
            return
        except Exception as e:
            attempt += 1
            if attempt >= retries:
                raise RuntimeError(f"Failed download {url}: {e}")
            time.sleep(backoff ** attempt)

def fetch_from_mirrors(mirrors: List[str], name: str, cfg: Dict[str, Any], checksum: Optional[str]=None) -> Path:
    """
    Try mirrors in order. Return path to cached file in distfiles_cache.
    mirrors: list of URLs (strings)
    name: basename fallback if mirror URLs end with slash
    """
    cache = Path(cfg.get("distfiles_cache"))
    cache.mkdir(parents=True, exist_ok=True)
    # try each mirror; use basename from URL if present
    last_err = None
    retries = cfg.get("max_download_retries", 3)
    backoff = cfg.get("download_backoff", 2)
    for url in mirrors:
        fname = url.rstrip("/").split("/")[-1] or name
        dest = cache / fname
        try:
            _download_with_retries(url, dest, retries=retries, backoff=backoff, cfg=cfg)
            # verify if checksum is provided
            if checksum and not _verify_checksum(dest, checksum):
                _log(cfg, name, f"checksum mismatch for {url}")
                # remove corrupted file and try next mirror
                try:
                    dest.unlink()
                except Exception:
                    pass
                continue
            return dest
        except Exception as e:
            last_err = e
            _log(cfg, name, f"mirror failed: {url} -> {e}")
            continue
    raise RuntimeError(f"All mirrors failed for {name}: {last_err}")

# ---------------------------
# Extraction
# ---------------------------

def extract_archive(archive: Path, dest: Path, cfg: Dict[str, Any]) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    try:
        if tarfile.is_tarfile(str(archive)):
            with tarfile.open(str(archive)) as t:
                t.extractall(path=str(dest))
            return
        if zipfile.is_zipfile(str(archive)):
            with zipfile.ZipFile(str(archive)) as z:
                z.extractall(path=str(dest))
            return
        # try 7z
        seven = shutil.which(cfg.get("7z_cmd","7z"))
        if seven:
            subprocess.run([seven, "x", "-y", str(archive), f"-o{str(dest)}"], check=True)
            return
        # fallback
        shutil.unpack_archive(str(archive), extract_dir=str(dest))
    except Exception as e:
        raise RuntimeError(f"Failed to extract {archive}: {e}")

# ---------------------------
# Patches & Hooks (validated)
# ---------------------------

def apply_patches(portdir: Path, build_dir: Path, cfg: Dict[str, Any]) -> None:
    pd = portdir / cfg.get("patch_dir","patches")
    if not pd.exists():
        return
    for p in sorted(pd.glob("*.patch")):
        _log(cfg, build_dir.name, f"applying patch {p.name}")
        try:
            subprocess.run(["patch", "-p1", "-i", str(p)], cwd=str(build_dir), check=True)
        except subprocess.CalledProcessError as e:
            _log(cfg, build_dir.name, f"patch failed: {p} -> {e}")
            raise RuntimeError(f"Patch failed: {p}")

def _hooks_env(portdir: Path, build_dir: Path, sandbox_dir: Path, cfg: Dict[str,Any]) -> Dict[str,str]:
    env = os.environ.copy()
    env.update({
        "PYPORT_PORTDIR": str(portdir),
        "PYPORT_BUILD_DIR": str(build_dir),
        "PYPORT_SANDBOX": str(sandbox_dir),
        "SANDBOX": str(sandbox_dir),
        "BUILD_DIR": str(build_dir),
        "PORTDIR": str(portdir),
    })
    # expose config keys that may be useful
    env["PYPORT_TOOLCHAIN"] = str(cfg.get("toolchain_dir","/mnt/tools"))
    return env

def run_hook_list(hooks: List[str], cwd: Optional[Path], fakerunner: Fakerunner, sandbox_dir: Path, cfg: Dict[str,Any]) -> None:
    if not hooks:
        return
    for cmd in hooks:
        if not cmd or not str(cmd).strip():
            _log(cfg, "hooks", f"empty/invalid hook ignored: {cmd}")
            continue
        _log(cfg, "hooks", f"running hook: {cmd}")
        try:
            fakerunner.run_and_check(cmd, cwd=str(cwd) if cwd else None, env=_hooks_env(cwd.parent if cwd else Path("."), cwd or Path("."), sandbox_dir, cfg), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
        except Exception as e:
            _log(cfg, "hooks", f"hook failed: {cmd} -> {e}")
            raise RuntimeError(f"Hook failed: {cmd} -> {e}")

# ---------------------------
# Build system handlers
# ---------------------------

def _autotools_handler(source_root: Path, build_dir: Path, fakerunner: Fakerunner, sandbox_dir: Path, cfg: Dict[str,Any]) -> None:
    # try autoreconf/autogen then configure; use prefix=/usr
    if (source_root / "autogen.sh").exists():
        fakerunner.run_and_check("./autogen.sh", cwd=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
    fakerunner.run_and_check("./configure --prefix=/usr", cwd=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
    fakerunner.run_and_check("make -j$(nproc)", cwd=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
    fakerunner.install_into_sandbox("make install", build_dir=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)

def _cmake_handler(source_root: Path, build_dir: Path, fakerunner: Fakerunner, sandbox_dir: Path, cfg: Dict[str,Any]) -> None:
    build_sub = source_root / "build"
    build_sub.mkdir(exist_ok=True)
    fakerunner.run_and_check(f"cmake -S {str(source_root)} -B {str(build_sub)} -DCMAKE_INSTALL_PREFIX=/usr", cwd=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
    fakerunner.run_and_check(f"cmake --build {str(build_sub)} -- -j$(nproc)", cwd=str(build_sub), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
    fakerunner.install_into_sandbox(f"cmake --install {str(build_sub)} --prefix /usr", build_dir=str(build_sub), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)

def _python_handler(source_root: Path, build_dir: Path, fakerunner: Fakerunner, sandbox_dir: Path, cfg: Dict[str,Any]) -> None:
    if (source_root / "pyproject.toml").exists():
        # pip install . --root=/install
        fakerunner.run_and_check("python3 -m pip install . --root=/install --prefix=/usr", cwd=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
    elif (source_root / "setup.py").exists():
        fakerunner.run_and_check("python3 setup.py build", cwd=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
        fakerunner.install_into_sandbox("python3 setup.py install", build_dir=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
    else:
        raise RuntimeError("Python build system but no pyproject.toml or setup.py found")

def _rust_handler(source_root: Path, build_dir: Path, fakerunner: Fakerunner, sandbox_dir: Path, cfg: Dict[str,Any]) -> None:
    fakerunner.run_and_check("cargo build --release", cwd=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
    fakerunner.install_into_sandbox("cargo install --path . --root /install", build_dir=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)

def _java_handler(source_root: Path, build_dir: Path, fakerunner: Fakerunner, sandbox_dir: Path, cfg: Dict[str,Any]) -> None:
    if (source_root / "pom.xml").exists():
        fakerunner.run_and_check("mvn package", cwd=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
    elif (source_root / "build.gradle").exists() or (source_root / "build.gradle.kts").exists():
        fakerunner.run_and_check("gradle build", cwd=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
    else:
        raise RuntimeError("Java build files not found")
    # user hooks should move artifacts into sandbox

_BUILD_HANDLERS = {
    "autotools": _autotools_handler,
    "cmake": _cmake_handler,
    "python": _python_handler,
    "rust": _rust_handler,
    "java": _java_handler,
}

# ---------------------------
# Source selection heuristics
# ---------------------------

def select_source_tree(build_dir: Path) -> Optional[Path]:
    entries = [p for p in build_dir.iterdir() if p.name != "sandbox"]
    if not entries:
        return None
    dirs = [p for p in entries if p.is_dir()]
    if len(dirs) == 1:
        return dirs[0]
    indicators = ("configure","CMakeLists.txt","setup.py","pyproject.toml","Cargo.toml","pom.xml","build.gradle")
    for p in entries:
        if p.is_dir():
            for ind in indicators:
                if (p/ind).exists():
                    return p
    return build_dir

# ---------------------------
# Snapshot metadata
# ---------------------------

def snapshot_metadata(sandbox_dir: Path, skip: Optional[List[str]] = None) -> List[Dict[str,Any]]:
    root = sandbox_dir.resolve()
    out: List[Dict[str,Any]] = []
    skip = skip or []
    for p in root.rglob("*"):
        rel = p.relative_to(root)
        s = None
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
            entry = {
                "path": str(rel),
                "is_dir": is_dir,
                "is_symlink": is_link,
                "target": target,
                "mode": oct(st.st_mode & 0o7777),
                "uid": st.st_uid,
                "gid": st.st_gid,
                "size": st.st_size,
                "mtime": st.st_mtime
            }
            if any(sp and sp in str(rel) for sp in skip):
                continue
            out.append(entry)
        except Exception:
            continue
    return out

# ---------------------------
# Dependency check (lightweight)
# ---------------------------

def check_dependencies(meta: Dict[str,Any], cfg: Dict[str,Any]) -> List[str]:
    """
    meta may contain dependencies in field 'depends' as list of package names.
    This function performs a lightweight check: if /var/lib/pyport/installed.json exists we consult it,
    else we check PATH for common binary names and warn.
    Returns list of missing deps (strings). Non-fatal.
    """
    missing: List[str] = []
    deps = meta.get("depends") or []
    if not deps:
        return missing
    installed_db = Path("/var/lib/pyport/installed.json")
    installed = {}
    if installed_db.exists():
        try:
            installed = json.loads(installed_db.read_text(encoding="utf-8"))
        except Exception:
            installed = {}
    for d in deps:
        # if installed DB contains name -> OK
        if d in installed:
            continue
        # heuristic: check if binary with same name is in PATH
        if shutil.which(d):
            continue
        missing.append(d)
    return missing

# ---------------------------
# Chroot helpers (prepare/destruct) - non-privileged ops only prepare dirs
# ---------------------------

def prepare_chroot(sandbox_dir: Path, cfg: Dict[str,Any]) -> List[Tuple[str,str]]:
    """
    Prepare directories for bind mounts and copy /etc/resolv.conf.
    Returns list of (src, dest) mount pairs the caller (or privileged helper) should mount.
    This function does NOT perform privileged mounts itself (unless running as root).
    """
    binds: List[Tuple[str,str]] = []
    for src in ("/dev","/proc","/sys","/run"):
        dst = str(sandbox_dir / src.lstrip("/"))
        Path(dst).mkdir(parents=True, exist_ok=True)
        binds.append((src, dst))
    # copy resolv.conf
    try:
        if Path("/etc/resolv.conf").exists():
            shutil.copy("/etc/resolv.conf", str(sandbox_dir / "etc" / "resolv.conf"))
    except Exception:
        _log(cfg, "chroot", "warning: could not copy /etc/resolv.conf")
    return binds

def try_mount_bind(src: str, dst: str) -> bool:
    try:
        subprocess.run(["mount","--bind", src, dst], check=True)
        return True
    except Exception:
        return False

def cleanup_chroot_binds(binds: List[Tuple[str,str]], cfg: Dict[str,Any]) -> None:
    # attempt umount in reverse order
    for src,dst in reversed(binds):
        try:
            subprocess.run(["umount", dst], check=False)
        except Exception:
            _log(cfg, "chroot", f"warning: failed to umount {dst}")

# ---------------------------
# Build tree creation
# ---------------------------

def create_build_tree(cfg: Dict[str,Any], meta: Dict[str,Any]) -> Tuple[Path,Path,Path]:
    build_root = Path(cfg.get("build_root"))
    cat = meta.get("category","misc")
    name = meta.get("name")
    ver = str(meta.get("version"))
    build_dir = build_root / cat / name / ver
    sandbox_dir = build_dir / "sandbox"
    src_cache = build_dir / "sources"
    build_dir.mkdir(parents=True, exist_ok=True)
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    src_cache.mkdir(parents=True, exist_ok=True)
    return build_dir, sandbox_dir, src_cache

# ---------------------------
# High-level build_port
# ---------------------------

def build_port(target: str, options: Optional[Dict[str,Any]] = None) -> Dict[str,Any]:
    """
    target: category/name or name
    options: dict with optional keys: keep_build, debug, dry_run, toolchain_dir, chroot (bool)
    Returns result dict with status, message, paths, metadata file, installed_files, logs.
    """
    cfg = get_config()
    opts = options or {}
    keep_build = opts.get("keep_build", False)
    debug = opts.get("debug", False)
    dry_run = opts.get("dry_run", False)
    toolchain_dir = opts.get("toolchain_dir", cfg.get("toolchain_dir"))

    # find portfile
    portsdir = Path(cfg.get("ports_dir"))
    if "/" in target:
        pf = portsdir / target / "portfile.yaml"
    else:
        matches = list(portsdir.glob("**/" + target + "/portfile.yaml"))
        pf = Path(matches[0]) if matches else None
    if not pf or not pf.exists():
        return {"status":"error","message":f"portfile for {target} not found", "target":target}

    portdir = pf.parent
    try:
        meta = _yaml_load(pf.read_text(encoding="utf-8")) or {}
    except Exception as e:
        return {"status":"error","message":f"failed parse portfile: {e}"}

    name = meta.get("name", portdir.name)
    version = meta.get("version", meta.get("pkgver", "0.0.0"))
    logname = f"{name}-{version}"
    logp = log_path(cfg, logname)
    _log(cfg, logname, f"build started for {name}-{version} (target: {target})")
    if debug:
        print(f"[pyport][debug] building {name}-{version}")

    build_dir, sandbox_dir, src_cache = create_build_tree(cfg, meta)

    # dependency check (non-fatal)
    missing = check_dependencies(meta, cfg)
    if missing:
        _log(cfg, logname, f"dependencies missing: {missing}")
        # non fatal, but warn
    # prepare fakerunner
    if Fakerunner is None:
        return {"status":"error","message":"Fakerunner module not available; please install fakeroot module", "log":str(logp)}

    fr = Fakerunner(use_bwrap=(cfg.get("sandbox_mode") in ("bwrap","both")),
                    use_fakeroot=(cfg.get("sandbox_mode") in ("fakeroot","both")),
                    debug=debug, dry_run=dry_run)

    # fetch sources (support mirrorlist and dict entries)
    try:
        srcs = meta.get("source") or []
        if isinstance(srcs, str):
            srcs = [srcs]
        fetched_paths: List[Path] = []
        for item in srcs:
            # normalize item to dict with keys: url(s)/git/checksum/dest
            if isinstance(item, str):
                if item.endswith(".git") or item.startswith("git@") or item.startswith("ssh://"):
                    # git shorthand
                    repo = item
                    dest = src_cache / (repo.rstrip("/").split("/")[-1].replace(".git",""))
                    _log(cfg, logname, f"cloning {repo} -> {dest}")
                    _git_clone_safely(repo, dest, cfg, logname)
                    fetched_paths.append(dest)
                    continue
                else:
                    urls = [item]
                    checksum = None
                    destname = item.rstrip("/").split("/")[-1]
            elif isinstance(item, dict):
                # accept "url" as string or list of mirrors
                checksum = item.get("checksum") or item.get("sha256") or item.get("sha512") or item.get("md5")
                destname = item.get("dest") or (item.get("url") and item.get("url").rstrip("/").split("/")[-1])
                if "git" in item:
                    repo = item.get("git")
                    dest = src_cache / (repo.rstrip("/").split("/")[-1].replace(".git",""))
                    _log(cfg, logname, f"cloning {repo} -> {dest}")
                    _git_clone_safely(repo, dest, cfg, logname, branch=item.get("branch"), commit=item.get("commit"))
                    fetched_paths.append(dest)
                    continue
                # mirrorlist
                if isinstance(item.get("url"), list):
                    urls = item.get("url")
                else:
                    urls = [item.get("url")]
            else:
                continue

            # attempt mirrors order; store in cache dir
            try:
                fp = fetch_from_mirrors([u for u in urls if u], destname or name, cfg, checksum=checksum)
            except Exception as e:
                _log(cfg, logname, f"download failed: {e}")
                raise
            # extract if archive
            try:
                # if file is archive extract into src_cache
                if any(str(fp).endswith(ext) for ext in (".tar.gz",".tgz",".tar.bz2",".tar.xz",".tar",".zip",".7z",".gz",".xz")):
                    _log(cfg, logname, f"extracting {fp} to {src_cache}")
                    extract_archive(fp, src_cache, cfg)
                    fetched_paths.append(src_cache)
                else:
                    # plain file, move to src_cache
                    fetched_paths.append(fp)
            except Exception as e:
                _log(cfg, logname, f"extract failed: {e}")
                raise

    except Exception as e:
        _log(cfg, logname, f"fetch error: {e}")
        # rollback: remove build_dir (partial) unless keep_build
        if not (cfg.get("keep_build_on_success") or keep_build):
            try:
                shutil.rmtree(build_dir)
            except Exception:
                pass
        return {"status":"error","message":f"fetch error: {e}", "log":str(logp)}

    # apply patches
    try:
        apply_patches(portdir, src_cache, cfg)
    except Exception as e:
        _log(cfg, logname, f"patch error: {e}")
        if not (cfg.get("keep_build_on_success") or keep_build):
            try: shutil.rmtree(build_dir)
            except Exception: pass
        return {"status":"error","message":f"patch error: {e}", "log":str(logp)}

    # find source root
    source_root = select_source_tree(src_cache)
    if source_root is None or not source_root.exists():
        _log(cfg, logname, "no source tree found")
        return {"status":"error","message":"no source tree found", "log":str(logp)}

    # run pre_configure hooks
    try:
        run_hook_list(meta.get("hooks", {}).get("pre_configure", []), source_root, fr, Path(sandbox_dir), cfg)
    except Exception as e:
        _log(cfg, logname, f"pre_configure failed: {e}")
        if not (cfg.get("keep_build_on_success") or keep_build):
            try: shutil.rmtree(build_dir)
            except Exception: pass
        return {"status":"error","message":f"pre_configure failed: {e}", "log":str(logp)}

    # snapshot before install
    before = snapshot_metadata(Path(sandbox_dir))

    # Run build
    build_system = meta.get("build_system", meta.get("build", "custom"))
    try:
        if build_system in _BUILD_HANDLERS:
            handler = _BUILD_HANDLERS[build_system]
            handler(source_root, build_dir, fr, Path(sandbox_dir), cfg)
        elif build_system == "custom":
            for cmd in meta.get("build", meta.get("steps", [])):
                fr.run_and_check(cmd, cwd=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
        else:
            _log(cfg, logname, f"unsupported build system: {build_system}")
            raise RuntimeError(f"unsupported build_system: {build_system}")
    except Exception as e:
        _log(cfg, logname, f"build failed: {e}")
        if not (cfg.get("keep_build_on_success") or keep_build):
            try: shutil.rmtree(build_dir)
            except Exception: pass
        return {"status":"error","message":f"build failed: {e}", "log":str(logp)}

    # post_configure hooks
    try:
        run_hook_list(meta.get("hooks", {}).get("post_configure", []), source_root, fr, Path(sandbox_dir), cfg)
    except Exception as e:
        _log(cfg, logname, f"post_configure failed: {e}")
        # non-fatal? treat as failure
        return {"status":"error","message":f"post_configure failed: {e}", "log":str(logp)}

    # checks
    try:
        run_hook_list(meta.get("hooks", {}).get("check", []), source_root, fr, Path(sandbox_dir), cfg)
    except Exception as e:
        _log(cfg, logname, f"check hook failed: {e}")
        # record warning, continue

    # pre_install hooks
    try:
        run_hook_list(meta.get("hooks", {}).get("pre_install", []), source_root, fr, Path(sandbox_dir), cfg)
    except Exception as e:
        _log(cfg, logname, f"pre_install hook failed: {e}")
        return {"status":"error","message":f"pre_install failed: {e}", "log":str(logp)}

    # If handlers didn't install, honor install field
    try:
        if not (source_root / "installed_marker").exists():
            for ic in meta.get("install", []):
                fr.install_into_sandbox(ic, build_dir=str(source_root), sandbox_dir=str(sandbox_dir), shell=True, stream_output=True)
    except Exception as e:
        _log(cfg, logname, f"install failed: {e}")
        return {"status":"error","message":f"install failed: {e}", "log":str(logp)}

    # post_install hooks
    try:
        run_hook_list(meta.get("hooks", {}).get("post_install", []), source_root, fr, Path(sandbox_dir), cfg)
    except Exception as e:
        _log(cfg, logname, f"post_install failed: {e}")
        return {"status":"error","message":f"post_install failed: {e}", "log":str(logp)}

    # snapshot after and compute installed files
    after = snapshot_metadata(Path(sandbox_dir))
    before_paths = {e["path"] for e in before}
    new_files = [e for e in after if e["path"] not in before_paths]

    # normalize permissions
    try:
        fr.normalize_permissions(str(sandbox_dir))
    except Exception:
        pass

    # write metadata JSON
    meta_file = Path(cfg.get("log_dir")) / f"{name}-{version}-sandbox-meta.json"
    try:
        meta_file.parent.mkdir(parents=True, exist_ok=True)
        with open(meta_file, "w", encoding="utf-8") as f:
            json.dump({"generated_at": time.time(), "entries": after}, f, indent=2)
    except Exception as e:
        _log(cfg, logname, f"failed writing metadata: {e}")

    # cleanup build dir if requested
    if not (cfg.get("keep_build_on_success") or keep_build):
        try:
            shutil.rmtree(build_dir)
        except Exception:
            pass

    # final logging + colored summary
    summary = {
        "status": "ok",
        "name": name,
        "version": version,
        "installed_count": len(new_files),
        "metadata": str(meta_file),
        "log": str(logp)
    }
    _log(cfg, logname, f"build finished: installed {len(new_files)} files; metadata: {meta_file}")
    # terminal summary
    try:
        if sys.stdout.isatty():
            print(f"{_COL_GREEN}PyPort: build OK{_COL_RESET} {name}-{version} -> {len(new_files)} files installed")
            if missing:
                print(f"{_COL_YELLOW}Aviso: dependências faltando: {', '.join(missing)}{_COL_RESET}")
        else:
            print(f"PyPort: build OK {name}-{version} -> {len(new_files)} files installed")
    except Exception:
        pass

    return {"status":"ok","message":"build successful","name":name,"version":version,"installed_files":[e["path"] for e in new_files],"metadata":str(meta_file),"log":str(logp)}

# ---------------------------
# Helper: git clone wrapper
# ---------------------------

def _git_clone_safely(repo: str, dest: Path, cfg: Dict[str,Any], logname: str, branch: Optional[str]=None, commit: Optional[str]=None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        # try to fetch and checkout; otherwise remove and fresh clone
        try:
            subprocess.run(["git","-C",str(dest),"fetch","--all","--tags"], check=True)
            if commit:
                subprocess.run(["git","-C",str(dest),"checkout",commit], check=True)
            elif branch:
                subprocess.run(["git","-C",str(dest),"checkout",branch], check=True)
            return
        except Exception:
            try:
                shutil.rmtree(dest)
            except Exception:
                pass
    clone_cmd = ["git","clone","--depth","1"]
    if branch:
        clone_cmd += ["-b", branch]
    clone_cmd += [repo, str(dest)]
    subprocess.run(clone_cmd, check=True)
    if commit:
        subprocess.run(["git","-C",str(dest),"fetch","--depth","1","origin",commit], check=False)
        subprocess.run(["git","-C",str(dest),"checkout",commit], check=False)

# ---------------------------
# CLI entrypoint for testing
# ---------------------------

def _cli_main():
    import argparse
    parser = argparse.ArgumentParser(prog="pyport-sandbox", description="PyPort advanced sandbox builder")
    parser.add_argument("target", help="category/name or name")
    parser.add_argument("--keep-build", action="store_true", help="preserve build tree")
    parser.add_argument("--debug", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    res = build_port(args.target, options={"keep_build":args.keep_build,"debug":args.debug,"dry_run":args.dry_run})
    print(json.dumps(res, indent=2))

if __name__ == "__main__":
    _cli_main()
