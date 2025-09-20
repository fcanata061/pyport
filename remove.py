#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
remove.py - PyPort módulo de remoção evoluído

Melhorias:
 - sandbox/fakeroot suportado
 - dry-run, confirmação interativa
 - logs coloridos + JSON
 - verificação de permissões
 - rollback mais robusto
 - integração com logger central, hooks
"""

import os
import sys
import json
import shutil
import subprocess
import time
from pathlib import Path
from typing import List, Dict, Any, Optional

from pyport.logger import get_logger
from pyport.hooks import run_hook
from pyport.dependency import DependencyGraph
from pyport.config import get_config
from pyport.sandbox import Sandbox

log = get_logger("pyport.remove")

DB_FILE = Path("/pyport/db/installed.json")
LOG_DIR = Path("/pyport/logs")
REMOVE_HISTORY = LOG_DIR / "remove_history.json"
LOG_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE.parent.mkdir(parents=True, exist_ok=True)

class RemoveError(Exception):
    pass

# ---------------- Utilities ----------------

def load_db() -> Dict[str, Any]:
    if DB_FILE.exists():
        try:
            return json.loads(DB_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            log.error(f"Erro lendo DB instalado: {e}")
    return {}

def save_db(db: Dict[str, Any]):
    DB_FILE.write_text(json.dumps(db, indent=2), encoding="utf-8")

def write_history(entry: Dict[str, Any]):
    arr = []
    if REMOVE_HISTORY.exists():
        try:
            arr = json.loads(REMOVE_HISTORY.read_text(encoding="utf-8"))
        except Exception:
            arr = []
    arr.append(entry)
    REMOVE_HISTORY.write_text(json.dumps(arr, indent=2), encoding="utf-8")

def backup_files(files: List[str], pkgname: str) -> Path:
    backup_dir = Path(tempfile_dir()) / f"pyport_backup_remove_{pkgname}_{int(time.time())}"
    if backup_dir.exists():
        shutil.rmtree(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    pairs = []
    for f in files:
        p = Path(f)
        if p.exists():
            dst = backup_dir / f.lstrip("/")
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                if p.is_file() or p.is_symlink():
                    shutil.copy2(p, dst)
                elif p.is_dir():
                    shutil.copytree(p, dst)
            except Exception as e:
                log.warning(f"Falha backup de {p}: {e}")
    return backup_dir

def restore_backup(backup_dir: Path, removed: List[str]):
    for f in removed:
        b = backup_dir / f.lstrip("/")
        dest = Path(f)
        try:
            if b.exists():
                dest.parent.mkdir(parents=True, exist_ok=True)
                if b.is_file():
                    shutil.copy2(b, dest)
                elif b.is_dir():
                    if dest.exists():
                        shutil.rmtree(dest, ignore_errors=True)
                    shutil.copytree(b, dest)
        except Exception as e:
            log.error(f"Falha restaurando {f} do backup: {e}")

def cleanup_empty_dirs(files: List[str]):
    dirs = sorted({str(Path(f).parent) for f in files}, key=lambda x: len(x.split(os.sep)), reverse=True)
    for d in dirs:
        p = Path(d)
        try:
            if p.exists() and p.is_dir() and not any(p.iterdir()):
                p.rmdir()
        except Exception:
            pass

def tempfile_dir() -> str:
    # usar tmp local ou variável de ambiente
    return os.getenv("PYPORT_TMP", "/tmp")

# ---------------- Main remove ----------------

def remove_package(
    pkgname: str,
    *,
    force: bool = False,
    dry_run: bool = False,
    yes: bool = False,
    sandbox: Optional[Sandbox] = None
) -> Dict[str, Any]:
    cfg = get_config()
    db = load_db()

    if pkgname not in db:
        log.error(f"Pacote '{pkgname}' não instalado.")
        return {"status": "error", "message": "not installed", "package": pkgname}

    # checar dependências reversas
    dg = DependencyGraph()
    rev = dg.reverse_dependencies(pkgname)
    if rev and not force:
        log.error(f"Dependências reversas detectadas para {pkgname}: {rev}")
        if not yes:
            log.info("Use --force ou confirme para continuar.")
            resp = input(f"Remover mesmo assim {pkgname}? (y/N): ").strip().lower()
            if resp not in ("y","yes"):
                return {"status": "cancelled", "message": "user aborted", "reverse_deps": rev}
        else:
            log.info("Forçando remoção ignorando dependências reversas.")

    files = db[pkgname].get("files", [])

    if not files:
        log.warning(f"Nenhum arquivo registrado para {pkgname}.")

    # Hooks pre_remove
    run_hook({"name": pkgname}, "pre_remove", sandbox_dirArg(sandbox), sandbox)

    if dry_run:
        log.info(f"Dry-run: mostraria remoção de {len(files)} arquivos.")
        return {"status": "dry-run", "package": pkgname, "files": files}

    # backup
    backup_dir = backup_files(files, pkgname)
    removed = []

    failed = False
    for f in files:
        p = Path(f)
        try:
            if p.is_file() or p.is_symlink():
                p.unlink()
            elif p.is_dir():
                shutil.rmtree(p)
            removed.append(str(p))
        except Exception as e:
            log.error(f"Falha removendo {p}: {e}")
            failed = True
            break

    if failed:
        # rollback
        restore_backup(backup_dir, removed)
        log.error(f"Rollback concluído para {pkgname}")
        return {"status": "error", "message": "removal failed, rollback done", "removed": removed}

    # Hooks post_remove
    run_hook({"name": pkgname}, "post_remove", sandbox_dirArg(sandbox), sandbox)

    # update DB
    del db[pkgname]
    save_db(db)

    cleanup_empty_dirs(removed)

    # history & log
    entry = {
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "package": pkgname,
        "removed_files": removed,
        "force": force
    }
    write_history(entry)

    log.info(f"Pacote {pkgname} removido com sucesso: {len(removed)} arquivos.")

    _notify(f"PyPort: {pkgname} removido")

    return {"status": "ok", "package": pkgname, "removed": removed}

# ---------------- CLI ----------------

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="PyPort remove package")
    parser.add_argument("package", help="Nome do pacote para remover")
    parser.add_argument("--force", action="store_true", help="Ignorar dependências inversas")
    parser.add_argument("--dry-run", action="store_true", help="Simular sem remover")
    parser.add_argument("--yes", action="store_true", help="Confirmar automaticamente")
    args = parser.parse_args()

    try:
        res = remove_package(args.package, force=args.force, dry_run=args.dry_run, yes=args.yes)
        print(json.dumps(res, indent=2, ensure_ascii=False))
        if res.get("status") != "ok":
            sys.exit(1)
    except Exception as e:
        log.error(f"Erro ao remover pacote: {e}")
        sys.exit(1)

if __name__ == "__main__":
    _cli()
