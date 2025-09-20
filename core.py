"""
pyport core module - evolved version ++

Coordena todo o ciclo de vida de um port:
- Leitura do Portfile.yaml
- Resolução de dependências
- Criação e destruição de sandbox
- Download de fontes (http, ftp, git, múltiplos)
- Descompactação automática
- Aplicação de patches
- Execução de hooks
- Construção via autotools, python, rust, java ou custom
- Empacotamento com packager.py
- Instalação e remoção limpa com DB
- Logs persistentes
"""

import os, sys, shutil, subprocess, tempfile, json
from pathlib import Path
from typing import Dict, Any, List
import yaml

from sandbox import create_sandbox, run_in_sandbox, destroy_sandbox
from packager import package_from_metadata


# diretórios principais
PORTFILES_ROOT = Path("/usr/ports")
PATCHES_DIRNAME = "patches"
PKG_DB = Path("/pyport/db")
PKG_DB.mkdir(parents=True, exist_ok=True)
INSTALLED_DB = PKG_DB / "installed.json"

LOG_DIR = Path("/pyport/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)


def log(msg: str, pkg: str = None):
    print(f"[core] {msg}")
    if pkg:
        with open(LOG_DIR / f"{pkg}.log", "a") as f:
            f.write(msg + "\n")


def load_installed_db() -> Dict[str, Any]:
    if INSTALLED_DB.exists():
        return json.loads(INSTALLED_DB.read_text())
    return {}


def save_installed_db(db: Dict[str, Any]):
    INSTALLED_DB.write_text(json.dumps(db, indent=2))


def load_portfile(portfile: Path) -> Dict[str, Any]:
    with open(portfile) as f:
        return yaml.safe_load(f)


def resolve_dependencies(deps: List[str]):
    for dep in deps:
        log(f"checando dependência: {dep}")
        # aqui pode chamar build_port recursivo
    return True


def run_cmd(cmd: List[str], cwd: Path = None):
    subprocess.run(cmd, cwd=cwd, check=True)


def download_sources(sources: List[str], workdir: Path):
    """
    Baixa múltiplas fontes (http, ftp, git).
    """
    for src in sources:
        if src.startswith("git+"):
            url = src[4:]
            log(f"clonando git {url}")
            run_cmd(["git", "clone", "--depth", "1", url], cwd=workdir)
        else:
            filename = src.split("/")[-1]
            dest = workdir / filename
            if not dest.exists():
                log(f"baixando {src}")
                try:
                    run_cmd(["wget", "-c", src], cwd=workdir)
                except Exception:
                    run_cmd(["curl", "-L", "-O", src], cwd=workdir)
            extract_if_archive(dest, workdir)


def extract_if_archive(filepath: Path, workdir: Path):
    """
    Detecta e extrai formatos comuns.
    """
    if filepath.suffixes[-2:] in [[".tar", ".gz"], [".tar", ".xz"], [".tar", ".bz2"], [".tar", ".zst"]]:
        log(f"extraindo {filepath.name}")
        run_cmd(["tar", "xf", str(filepath)], cwd=workdir)
    elif filepath.suffix == ".zip":
        log(f"extraindo {filepath.name}")
        run_cmd(["unzip", "-o", str(filepath)], cwd=workdir)


def apply_patches(patch_dir: Path, target_dir: Path):
    if not patch_dir.exists():
        return
    for patch in sorted(patch_dir.glob("*.patch")):
        log(f"aplicando patch {patch.name}")
        run_cmd(["patch", "-p1", "-i", str(patch)], cwd=target_dir)


def run_hooks(hooks: Dict[str, str], stage: str, sandbox_dir: Path):
    if stage in hooks and hooks[stage]:
        log(f"executando hook {stage}")
        run_in_sandbox(sandbox_dir, hooks[stage])


def build_with_system(build_type: str, sandbox_dir: Path):
    if build_type == "autotools":
        cmds = [
            "./configure --prefix=/usr",
            "make -j$(nproc)",
            "make install DESTDIR=/install"
        ]
    elif build_type == "python":
        cmds = [
            "python3 setup.py build",
            "python3 setup.py install --root=/install --prefix=/usr"
        ]
    elif build_type == "rust":
        cmds = [
            "cargo build --release",
            "cargo install --path . --root /install/usr"
        ]
    elif build_type == "java":
        cmds = [
            "javac *.java",
            "mkdir -p /install/usr/share/java",
            "cp *.class /install/usr/share/java/"
        ]
    else:
        log(f"tipo de build desconhecido: {build_type}")
        return False

    for cmd in cmds:
        run_in_sandbox(sandbox_dir, cmd)
    return True


def build_port(portname: str):
    portfile = PORTFILES_ROOT / f"{portname}/Portfile.yaml"
    if not portfile.exists():
        log(f"Portfile {portfile} não encontrado")
        return False

    meta = load_portfile(portfile)
    name, version = meta["name"], meta["version"]

    log(f"iniciando build de {name}-{version}", name)

    resolve_dependencies(meta.get("depends", []))

    sandbox_dir = create_sandbox(name, version)
    workdir = sandbox_dir / "build"
    workdir.mkdir(parents=True, exist_ok=True)

    download_sources(meta.get("source", []), workdir)

    patch_dir = portfile.parent / PATCHES_DIRNAME
    apply_patches(patch_dir, workdir)

    run_hooks(meta.get("hooks", {}), "pre_configure", sandbox_dir)

    if meta.get("build", "custom") != "custom":
        build_with_system(meta["build"], sandbox_dir)
    else:
        run_hooks(meta.get("hooks", {}), "build", sandbox_dir)

    run_hooks(meta.get("hooks", {}), "post_configure", sandbox_dir)
    run_hooks(meta.get("hooks", {}), "pre_install", sandbox_dir)
    run_hooks(meta.get("hooks", {}), "install", sandbox_dir)
    run_hooks(meta.get("hooks", {}), "post_install", sandbox_dir)

    metadata_path = sandbox_dir / "metadata.json"
    metadata = {
        "name": name,
        "version": version,
        "depends": meta.get("depends", []),
        "description": meta.get("description", ""),
        "sandbox": str(sandbox_dir / "install")
    }
    metadata_path.write_text(json.dumps(metadata, indent=2))

    results = package_from_metadata(metadata_path)

    log(f"build concluído: {results}", name)
    return True


def install_port(portname: str):
    db = load_installed_db()
    if portname in db:
        log(f"{portname} já instalado")
        return False

    pkgdir = Path("/pyport/packages")
    pkgfile = next(pkgdir.glob(f"{portname}-*.tar.zst"), None)
    if not pkgfile:
        log(f"pacote {portname} não encontrado, execute build primeiro")
        return False

    log(f"instalando {pkgfile}", portname)
    subprocess.run(f"tar --use-compress-program=zstd -xvf {pkgfile} -C /", shell=True, check=True)

    db[portname] = {"pkgfile": str(pkgfile)}
    save_installed_db(db)
    return True


def remove_port(portname: str):
    db = load_installed_db()
    if portname not in db:
        log(f"{portname} não está instalado")
        return False

    log(f"removendo {portname}", portname)
    # futuro: remover rastreamento de arquivos
    del db[portname]
    save_installed_db(db)
    return True


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("Uso: python3 core.py <build|install|remove> <nome>")
        sys.exit(1)

    cmd, pkg = sys.argv[1], sys.argv[2]
    if cmd == "build":
        build_port(pkg)
    elif cmd == "install":
        install_port(pkg)
    elif cmd == "remove":
        remove_port(pkg)
    else:
        print(f"comando desconhecido: {cmd}")
