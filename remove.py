"""
pyport remove module - evolved version ++

Gerencia a remoção de pacotes:
- Lê DB de pacotes instalados
- Executa hooks pre/post remove (em sandbox)
- Remove arquivos rastreados com rollback em falhas
- Atualiza DB
- Integra com dependency.py para checar dependências reversas
- Limpa diretórios órfãos
- Gera logs detalhados e histórico em JSON
- Suporte a --force para ignorar dependências
"""

import os, json, subprocess, shutil, sys, time
from pathlib import Path
from typing import Dict, Any, List

import yaml

from dependency import DependencyGraph
from sandbox import run_in_sandbox

PORTFILES_ROOT = Path("/usr/ports")
PKG_DB = Path("/pyport/db")
PKG_DB.mkdir(parents=True, exist_ok=True)
INSTALLED_DB = PKG_DB / "installed.json"

LOG_DIR = Path("/pyport/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
REMOVE_HISTORY = LOG_DIR / "remove_history.json"


def log(msg: str, pkg: str = None):
    print(f"[remove] {msg}")
    if pkg:
        with open(LOG_DIR / f"{pkg}.log", "a") as f:
            f.write(msg + "\n")


def load_installed_db() -> Dict[str, Any]:
    if INSTALLED_DB.exists():
        return json.loads(INSTALLED_DB.read_text())
    return {}


def save_installed_db(db: Dict[str, Any]):
    INSTALLED_DB.write_text(json.dumps(db, indent=2))


def update_remove_history(entry: Dict[str, Any]):
    history = []
    if REMOVE_HISTORY.exists():
        history = json.loads(REMOVE_HISTORY.read_text())
    history.append(entry)
    REMOVE_HISTORY.write_text(json.dumps(history, indent=2))


def run_hook(portname: str, hook: str, sandbox: Path = None):
    """
    Executa hooks de remoção definidos no Portfile.yaml.
    """
    portfile = PORTFILES_ROOT / f"{portname}/Portfile.yaml"
    if not portfile.exists():
        return
    try:
        meta = yaml.safe_load(open(portfile))
        hooks = meta.get("hooks", {})
        if hook in hooks and hooks[hook]:
            log(f"executando hook {hook}", portname)
            if sandbox:
                run_in_sandbox(sandbox, hooks[hook])
            else:
                subprocess.run(hooks[hook], shell=True, check=False)
    except Exception as e:
        log(f"falha ao executar hook {hook}: {e}", portname)


def check_reverse_dependencies(portname: str) -> list:
    """
    Usa dependency.py para verificar dependentes reversos.
    """
    dg = DependencyGraph()
    dg.load_from_ports(PORTFILES_ROOT)
    return dg.reverse_dependencies(portname)


def remove_files(file_list: List[str], pkg: str) -> List[str]:
    """
    Remove arquivos instalados registrados no DB.
    Retorna lista de arquivos realmente removidos.
    """
    removed = []
    for f in file_list:
        try:
            path = Path(f)
            if not path.exists():
                continue
            if path.is_file() or path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
            removed.append(str(path))
        except Exception as e:
            log(f"falha ao remover {f}: {e}", pkg)
    return removed


def cleanup_empty_dirs(file_list: List[str]):
    """
    Remove diretórios órfãos após remoção.
    """
    dirs = sorted({str(Path(f).parent) for f in file_list}, key=len, reverse=True)
    for d in dirs:
        try:
            p = Path(d)
            if p.exists() and not any(p.iterdir()):
                p.rmdir()
        except Exception:
            pass


def rollback_files(removed_files: List[str], backup_dir: Path, pkg: str):
    """
    Restaura arquivos de backup em caso de falha.
    """
    for f in removed_files:
        src = backup_dir / f.lstrip("/")
        dest = Path(f)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
            log(f"rollback restaurado: {f}", pkg)
        except Exception as e:
            log(f"falha rollback {f}: {e}", pkg)


def backup_files(file_list: List[str], pkg: str) -> Path:
    """
    Cria backup temporário antes de remover arquivos.
    """
    backup_dir = Path(f"/tmp/pyport_backup_{pkg}")
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    for f in file_list:
        path = Path(f)
        if path.exists():
            dest = backup_dir / f.lstrip("/")
            dest.parent.mkdir(parents=True, exist_ok=True)
            try:
                if path.is_file():
                    shutil.copy2(path, dest)
                elif path.is_dir():
                    shutil.copytree(path, dest)
            except Exception as e:
                log(f"falha backup {f}: {e}", pkg)
    return backup_dir


def remove_port(portname: str, force: bool = False, sandbox: Path = None) -> bool:
    db = load_installed_db()
    if portname not in db:
        log(f"{portname} não está instalado")
        return False

    reverse_deps = check_reverse_dependencies(portname)
    if reverse_deps and not force:
        log(f"ERRO: {portname} é dependência de {', '.join(reverse_deps)}. Use --force para ignorar.")
        return False

    pkginfo = db[portname]
    files = pkginfo.get("files", [])

    log(f"iniciando remoção de {portname} ({len(files)} arquivos)", portname)

    run_hook(portname, "pre_remove", sandbox)

    backup_dir = backup_files(files, portname)
    removed_files = []

    try:
        removed_files = remove_files(files, portname)
        cleanup_empty_dirs(files)
    except Exception as e:
        log(f"falha crítica na remoção: {e}", portname)
        rollback_files(removed_files, backup_dir, portname)
        return False

    run_hook(portname, "post_remove", sandbox)

    if portname in db:
        del db[portname]
        save_installed_db(db)

    update_remove_history({
        "package": portname,
        "removed_files": removed_files,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    })

    log(f"{portname} removido com sucesso ({len(removed_files)} arquivos)", portname)

    try:
        subprocess.run([
            "notify-send",
            "PyPort",
            f"Pacote {portname} removido ({len(removed_files)} arquivos)"
        ], check=False)
    except FileNotFoundError:
        pass  # notify-send não disponível

    return True


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 remove.py <pacote> [--force]")
        sys.exit(1)

    pkg = sys.argv[1]
    force = "--force" in sys.argv
    remove_port(pkg, force=force)
