#!/usr/bin/env python3
"""
install.py - módulo de instalação de pacotes PyPort (evoluído, completo e funcional)

Funcionalidades principais:
 - Instala pacotes a partir de /pyport/packages (*.tar.zst, *.deb, *.rpm)
 - Pode instalar a partir de um arquivo .tar.zst gerado pelo packager, ou de formatos do sistema (.deb/.rpm)
 - Pode instalar a partir do nome do package (procura em /pyport/packages) e, se não encontrar, pede build via core (se disponível)
 - Executa hooks pre_install/post_install definidos no Portfile.yaml (rodando no sandbox se disponível)
 - Faz backup/rollback em falhas (backup temporário dos arquivos que serão sobrescritos)
 - Atualiza DB de instalados em /pyport/db/installed.json com lista de arquivos instalados, versão e timestamp
 - Logs detalhados em /pyport/logs/install-<pkg>.log
 - Notificação via notify-send quando disponível
 - Opções: --force (ignora conflitos), --dry-run, --sudo (usar sudo para operações de gravação em /)
"""

from __future__ import annotations
import os
import sys
import json
import shutil
import subprocess
import tempfile
import argparse
import time
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

try:
    import yaml
except Exception:
    yaml = None

# caminhos padrão
PKG_DIR = Path("/pyport/packages")
DB_DIR = Path("/pyport/db")
DB_DIR.mkdir(parents=True, exist_ok=True)
INSTALLED_DB = DB_DIR / "installed.json"

LOG_DIR = Path("/pyport/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)

PORTS_ROOT = Path("/usr/ports")

# utilidades
def now_ts() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")

def log(pkg: str, msg: str) -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    f = LOG_DIR / f"install-{pkg}.log"
    line = f"[{now_ts()}] {msg}\n"
    with open(f, "a", encoding="utf-8") as fh:
        fh.write(line)
    # também ecoa no stdout
    print(f"[{pkg}] {msg}")

def load_installed_db() -> Dict[str, Any]:
    if INSTALLED_DB.exists():
        try:
            return json.loads(INSTALLED_DB.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}

def save_installed_db(db: Dict[str, Any]) -> None:
    INSTALLED_DB.write_text(json.dumps(db, indent=2), encoding="utf-8")

def notify(summary: str, body: str = "") -> None:
    try:
        subprocess.run(["notify-send", summary, body], check=False)
    except Exception:
        pass

# --- helpers para extração e instalação -------------------------------------

def _is_tar_zst(path: Path) -> bool:
    return path.suffix == ".zst" or str(path).endswith(".tar.zst") or str(path).endswith(".tar.zst")

def _extract_tar_zst(pkgfile: Path, target_dir: Path, use_zstd_bin: bool = True) -> None:
    """
    Extrai pacote .tar.zst para target_dir.
    Usa tar -I zstd se disponível; fallback para Python zstandard + tarfile não implementado aqui.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    if shutil.which("tar") and (shutil.which("zstd") or shutil.which("zstdcat")):
        # usar tar com -I zstd
        cmd = f"tar -I zstd -xf {str(pkgfile)} -C {str(target_dir)}"
        subprocess.run(cmd, shell=True, check=True)
    else:
        # fallback: tentar usar system 'zstdcat' | tar -xf -
        if shutil.which("zstdcat") and shutil.which("tar"):
            cmd = f"zstdcat {str(pkgfile)} | tar -xf - -C {str(target_dir)}"
            subprocess.run(cmd, shell=True, check=True)
        else:
            raise RuntimeError("Necessário 'tar' com suporte a zstd ou 'zstdcat' para extrair .tar.zst")

def _install_deb(pkgfile: Path, sudo: bool = True) -> None:
    if not shutil.which("dpkg"):
        raise RuntimeError("dpkg não encontrado no sistema")
    cmd = ["dpkg", "-i", str(pkgfile)]
    if sudo:
        cmd.insert(0, "sudo")
    subprocess.run(cmd, check=True)

def _install_rpm(pkgfile: Path, sudo: bool = True) -> None:
    # tenta rpm ou dnf/rpm-ostree dependendo do sistema
    if shutil.which("rpm"):
        cmd = ["rpm", "-i", str(pkgfile)]
    elif shutil.which("dnf"):
        cmd = ["dnf", "install", "-y", str(pkgfile)]
    else:
        raise RuntimeError("Nem 'rpm' nem 'dnf' encontrados")
    if sudo:
        cmd.insert(0, "sudo")
    subprocess.run(cmd, check=True)

# lista todos os arquivos contidos no diretório data/ ou árvore extraída
def _list_installed_files(extract_dir: Path) -> List[str]:
    # se pacote tiver estrutura data/control, procurar data/
    data_dir = extract_dir / "data"
    root_dir = extract_dir
    if data_dir.exists():
        root_dir = data_dir
    files = []
    for p in root_dir.rglob("*"):
        # registrar caminhos absolutos relativos ao root do sistema
        rel = p.relative_to(root_dir)
        files.append(str(Path("/") / rel))
    return files

# backup de arquivos que serão sobrescritos/ removidos
def _backup_existing_files(file_list: List[str], backup_root: Path) -> List[Tuple[str,str]]:
    """
    Copia para backup_root a lista de arquivos/dirs que existem atualmente.
    Retorna lista de pares (original, backup_path)
    """
    pairs: List[Tuple[str,str]] = []
    for f in file_list:
        src = Path(f)
        if not src.exists():
            continue
        dest = backup_root / f.lstrip("/")
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            if src.is_file() or src.is_symlink():
                shutil.copy2(src, dest)
            elif src.is_dir():
                shutil.copytree(src, dest)
            pairs.append((str(src), str(dest)))
        except Exception as e:
            # se falhar no backup, continuamos; rollback tentará restaurar o que existir
            pass
    return pairs

def _restore_backup(pairs: List[Tuple[str,str]]) -> None:
    for orig, backup in pairs:
        try:
            src = Path(backup)
            dst = Path(orig)
            if not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_file():
                shutil.copy2(src, dst)
            elif src.is_dir():
                # remove destino e recopia
                if dst.exists():
                    if dst.is_dir():
                        shutil.rmtree(dst, ignore_errors=True)
                    else:
                        try: dst.unlink()
                        except Exception: pass
                shutil.copytree(src, dst)
        except Exception:
            pass

# --- localizar Portfile e executar hooks -----------------------------------

def find_portfile_for_package_name(pkgname: str) -> Optional[Path]:
    """
    Procura por Portfile.yaml que declare name == pkgname
    """
    if not PORTS_ROOT.exists():
        return None
    for pf in PORTS_ROOT.glob("**/Portfile.yaml"):
        try:
            if yaml:
                with open(pf, encoding="utf-8") as fh:
                    data = yaml.safe_load(fh)
                if data and data.get("name") == pkgname:
                    return pf
            else:
                txt = pf.read_text(encoding="utf-8")
                for line in txt.splitlines():
                    if line.strip().startswith("name:"):
                        val = line.split(":",1)[1].strip()
                        if val == pkgname:
                            return pf
        except Exception:
            continue
    return None

def _run_hook_from_portfile(portfile: Path, hook_name: str, sandbox_runner: Optional[callable] = None) -> None:
    """
    Executa hook definido no portfile: hooks: { pre_install: "...", post_install: "..." }
    Se sandbox_runner fornecido, usa ele (ex.: run_in_sandbox(sandbox, cmd))
    """
    if not portfile or not portfile.exists():
        return
    try:
        data = yaml.safe_load(portfile.read_text(encoding="utf-8")) if yaml else {}
        hooks = data.get("hooks", {}) if isinstance(data, dict) else {}
        cmd = hooks.get(hook_name)
        if not cmd:
            return
        log(portfile.parent.name, f"executando hook {hook_name}: {cmd}")
        if sandbox_runner:
            sandbox_runner(cmd)
        else:
            subprocess.run(cmd, shell=True, check=False)
    except Exception as e:
        log(portfile.parent.name, f"erro ao executar hook {hook_name}: {e}")

# --- operação principal: instalar a partir de arquivo -----------------------

def install_from_file(pkgfile: Path, *, force: bool = False, dry_run: bool = False, sudo: bool = True,
                      sandbox_runner: Optional[callable] = None) -> Dict[str, Any]:
    """
    Instala pacote a partir de arquivo. Suporta:
     - .tar.zst (conteúdo do pacote criado pelo packager)
     - .deb  (dpkg -i)
     - .rpm  (rpm -i ou dnf)
    Retorna dicionário com status, installed_files, message.
    """
    if not pkgfile.exists():
        return {"status":"error", "message":f"package file not found: {pkgfile}"}
    pkgname = pkgfile.stem.split("-")[0]
    log(pkgname, f"Iniciando instalação do arquivo {pkgfile}")

    # se .deb / .rpm -> delegar para gerenciador de pacotes do sistema
    try:
        if str(pkgfile).endswith(".deb"):
            if dry_run:
                log(pkgname, "dry-run: .deb install would run dpkg -i")
                return {"status":"ok","message":"dry-run"}
            _install_deb(pkgfile, sudo=sudo)
            # após dpkg, não temos lista de arquivos facilmente; pedir dpkg-query? omitido — marca instalado sem arquivo list
            db = load_installed_db()
            db[pkgname] = {"package_file": str(pkgfile), "installed_at": now_ts(), "files": []}
            save_installed_db(db)
            notify("PyPort", f"{pkgname} instalado (.deb)")
            log(pkgname, "instalação .deb concluída")
            return {"status":"ok", "message":".deb installed", "installed_files":[]}

        if str(pkgfile).endswith(".rpm"):
            if dry_run:
                log(pkgname, "dry-run: .rpm install would run rpm/dnf")
                return {"status":"ok","message":"dry-run"}
            _install_rpm(pkgfile, sudo=sudo)
            db = load_installed_db()
            db[pkgname] = {"package_file": str(pkgfile), "installed_at": now_ts(), "files": []}
            save_installed_db(db)
            notify("PyPort", f"{pkgname} instalado (.rpm)")
            log(pkgname, "instalação .rpm concluída")
            return {"status":"ok", "message":".rpm installed", "installed_files":[]}
    except Exception as e:
        log(pkgname, f"falha instalando pacote nativo: {e}")
        return {"status":"error", "message":str(e)}

    # assume .tar.zst or archive with root tree / data/
    tmpd = Path(tempfile.mkdtemp(prefix="pyport_install_"))
    try:
        log(pkgname, f"extraindo pacote para {tmpd}")
        _extract_ok = False
        try:
            _extract_tar_zst(pkgfile, tmpd)
            _extract_ok = True
        except Exception as e:
            # talvez seja .tar (não compress) ou outro; tentar tar -xf
            try:
                subprocess.run(f"tar -xf {str(pkgfile)} -C {str(tmpd)}", shell=True, check=True)
                _extract_ok = True
            except Exception as e2:
                log(pkgname, f"não foi possível extrair pacote: {e} / {e2}")
                raise RuntimeError(f"extract failed: {e} / {e2}")

        if not _extract_ok:
            raise RuntimeError("failed to extract package")

        # lista arquivos que serão instalados
        installed_files = _list_installed_files(tmpd)

        # se não for force, verificar conflitos: se algum arquivo já pertence a outro pacote instalado, avisar/abortar
        db = load_installed_db()
        if not force:
            conflicts = []
            for other, info in db.items():
                other_files = info.get("files", [])
                overlap = set(other_files) & set(installed_files)
                if overlap:
                    conflicts.append({"package": other, "overlap_count": len(overlap), "overlap_sample": list(overlap)[:5]})
            if conflicts:
                log(pkgname, f"conflitos detectados com pacotes instalados: {conflicts}")
                return {"status":"conflict", "message":"conflicts detected", "conflicts": conflicts}

        # executar pre_install hook (do Portfile, se houver)
        pf = find_portfile_for_package_name(pkgname)
        _run_hook_from_portfile(pf, "pre_install", sandbox_runner)

        # backup arquivos existentes
        backup_root = Path(tempfile.mkdtemp(prefix=f"pyport_backup_{pkgname}_"))
        backup_pairs = _backup_existing_files(installed_files, backup_root)
        log(pkgname, f"arquivos que serão instalados: {len(installed_files)}; backups: {len(backup_pairs)}")

        # copiar arquivos para / (respeitar dry_run)
        copied = []
        try:
            if dry_run:
                log(pkgname, "dry-run: não serão escritos arquivos no sistema")
            else:
                # se o pacote extraiu com data/ subdir, usar data as root; caso contrário, usar tmpd as root
                data_dir = tmpd / "data"
                root_src = data_dir if data_dir.exists() else tmpd
                # copiar tudo preservando metadados
                for p in sorted(root_src.rglob("*")):
                    rel = p.relative_to(root_src)
                    dest = Path("/") / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        if p.is_dir():
                            # garantir diretório
                            dest.mkdir(parents=True, exist_ok=True)
                            shutil.copystat(p, dest, follow_symlinks=False)
                        elif p.is_symlink():
                            # recriar symlink
                            if dest.exists() or dest.is_symlink():
                                try: dest.unlink()
                                except Exception: pass
                            target = os.readlink(str(p))
                            os.symlink(target, str(dest))
                        elif p.is_file():
                            shutil.copy2(p, dest)
                        copied.append(str(dest))
                    except Exception as e:
                        log(pkgname, f"falha ao copiar {p} -> {dest}: {e}")
                        raise

            # executar post_install hook
            _run_hook_from_portfile(pf, "post_install", sandbox_runner)

            # atualizar DB de instalados com lista de arquivos
            db = load_installed_db()
            db[pkgname] = {
                "package_file": str(pkgfile),
                "installed_at": now_ts(),
                "files": installed_files
            }
            save_installed_db(db)

            notify("PyPort", f"{pkgname} instalado ({len(installed_files)} arquivos)")
            log(pkgname, f"instalação concluída: {len(installed_files)} arquivos")
            return {"status":"ok", "installed_files": installed_files}

        except Exception as e:
            # rollback: restaurar backups
            log(pkgname, f"erro durante instalação: {e}; iniciando rollback")
            try:
                _restore_backup(backup_pairs)
                log(pkgname, "rollback concluído")
            except Exception as e2:
                log(pkgname, f"erro durante rollback: {e2}")
            return {"status":"error", "message": str(e)}
    finally:
        try:
            shutil.rmtree(tmpd, ignore_errors=True)
        except Exception:
            pass

# --- busca de pacote por nome e instalação (alto nível) ---------------------

def install_package_by_name(name: str, *, force: bool = False, dry_run: bool = False,
                            sudo: bool = True, build_if_missing: bool = True,
                            sandbox_runner: Optional[callable] = None) -> Dict[str, Any]:
    """
    Instala pacote procurando em /pyport/packages por arquivos que comecem com name-.
    Se não encontrar e build_if_missing=True, tenta chamar core.build (se disponível).
    """
    log(name, f"request install (force={force}, dry_run={dry_run}, build_if_missing={build_if_missing})")

    # procurar pacote em /pyport/packages
    candidates = sorted(PKG_DIR.glob(f"{name}-*"))
    if candidates:
        # escolher o mais recente por nome-> versão simples (ordem lexicográfica)
        pkgfile = candidates[-1]
        log(name, f"pacote encontrado: {pkgfile}")
        return install_from_file(pkgfile, force=force, dry_run=dry_run, sudo=sudo, sandbox_runner=sandbox_runner)

    # se não achou, tentar build via core (se disponível)
    try:
        import core
        if build_if_missing and hasattr(core, "build_and_package"):
            log(name, "pacote não encontrado em packages; invocando core.build_and_package")
            res = core.build_and_package(name)
            if res.get("status") == "ok":
                # pegar pacote gerado
                pkgs = res.get("packages") or {}
                # escolher primeira tar.zst ou any
                for ext in ("tar.zst","deb","rpm"):
                    if isinstance(pkgs, dict) and pkgs.get(ext):
                        pkgfile = Path(pkgs[ext])
                        return install_from_file(pkgfile, force=force, dry_run=dry_run, sudo=sudo, sandbox_runner=sandbox_runner)
                # se packager devolveu list:
                if isinstance(pkgs, list) and pkgs:
                    return install_from_file(Path(pkgs[0]), force=force, dry_run=dry_run, sudo=sudo, sandbox_runner=sandbox_runner)
            else:
                log(name, f"core.build_and_package falhou: {res}")
                return {"status":"error", "message": "build failed", "detail": res}
    except Exception:
        log(name, "core module não disponível / build auto não possível")

    return {"status":"error", "message":"package not found and build not available"}

# --- CLI --------------------------------------------------------------------

def _cli():
    p = argparse.ArgumentParser(prog="pyport-install", description="PyPort installer")
    p.add_argument("what", help="arquivo de pacote (.tar.zst/.deb/.rpm) ou nome do pacote")
    p.add_argument("--force", action="store_true", help="ignorar conflitos")
    p.add_argument("--dry-run", action="store_true", help="não escrever no sistema")
    p.add_argument("--no-sudo", dest="sudo", action="store_false", help="não usar sudo para operações que precisem de privilégio")
    p.add_argument("--no-build", dest="build", action="store_false", help="não tentar buildar se pacote não existir")
    args = p.parse_args()

    target = Path(args.what)
    if target.exists():
        res = install_from_file(target, force=args.force, dry_run=args.dry_run, sudo=args.sudo)
    else:
        res = install_package_by_name(str(args.what), force=args.force, dry_run=args.dry_run, sudo=args.sudo, build_if_missing=args.build)

    print(json.dumps(res, indent=2))

if __name__ == "__main__":
    _cli()
