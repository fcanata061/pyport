#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
core.py - Núcleo avançado do PyPort

Versão evoluída: coordena todo o ciclo de vida de um port,
com compatibilidade total com módulos: logger, config, dependency, fetch, extract, patch,
configure, compile, install, remove, hooks, packager, sandbox.

Funções principais: build, remove, sync, info, list
"""

from __future__ import annotations
import os
import sys
import json
import time
from pathlib import Path
from typing import Dict, Any

import yaml

from pyport.logger import get_logger
from pyport.config import get_config
from pyport.dependency import DependencyGraph
from pyport.fetch import fetch_sources
from pyport.extract import extract_sources
from pyport.patch import apply_patches
from pyport.configure import configure
from pyport.compile import compile_port
from pyport.packager import package_from_metadata
from pyport.install import install_package
from pyport.remove import remove_package
from pyport.hooks import run_hook
from pyport.sandbox import Sandbox

LOG = get_logger("pyport.core")

class Core:
    """
    Classe central para operações principais do PyPort
    """

    def __init__(self):
        self.cfg = get_config()
        self.ports_root = Path(self.cfg["paths"]["ports"])
        self.sandbox_root = Path(self.cfg["paths"]["sandbox"])
        self.installed_db_path = Path(self.cfg["paths"]["db"]) / "installed.json"

    def load_installed(self) -> Dict[str, Any]:
        if self.installed_db_path.exists():
            try:
                return json.loads(self.installed_db_path.read_text(encoding="utf-8"))
            except Exception as e:
                LOG.error(f"Erro lendo DB instalado: {e}")
        return {}

    def save_installed(self, db: Dict[str, Any]):
        self.installed_db_path.parent.mkdir(parents=True, exist_ok=True)
        self.installed_db_path.write_text(json.dumps(db, indent=2), encoding="utf-8")

    def is_installed(self, name: str) -> bool:
        db = self.load_installed()
        return name in db

    def build(self, portname: str, force: bool = False, dry_run: bool = False) -> bool:
        name = portname
        LOG.info(f"Build iniciado: {name}, force={force}, dry_run={dry_run}")

        # Carregar portfile
        pf = self.ports_root / portname / "Portfile.yaml"
        if not pf.exists():
            LOG.error(f"Portfile não encontrado: {pf}")
            return False

        try:
            meta = yaml.safe_load(pf.read_text(encoding="utf-8"))
        except Exception as e:
            LOG.error(f"Erro lendo Portfile {pf}: {e}")
            return False

        version = meta.get("version", "unknown")

        # Verificar se já instalado
        if self.is_installed(name) and not force:
            LOG.info(f"{name} já instalado (versão possivelmente {version}), pulando build.")
            return True

        # Resolver dependências
        dg = DependencyGraph(persist=True)
        dg.load_ports_tree(self.ports_root)
        res = dg.resolve(name)
        if not res.get("ok", False):
            LOG.error(f"Falha na resolução de dependências para {name}")
            LOG.debug(f"Missing: {res.get('missing')}, cycles: {res.get('cycles')}, conflicts: {res.get('conflicts')}")
            return False

        # Construir dependências primeiro
        for dep in res["order"]:
            if dep == name:
                continue
            LOG.info(f"Construindo dependência: {dep}")
            if not self.build(dep, force=force, dry_run=dry_run):
                LOG.error(f"Falha ao construir dependência {dep}")
                return False

        # Preparar sandbox
        sandbox_dir = self.sandbox_root / f"{name}-{version}"
        sb = Sandbox(str(sandbox_dir), force_clean=force)
        try:
            sb.prepare()
        except Exception as e:
            LOG.error(f"Falha ao preparar sandbox para {name}: {e}")
            return False

        try:
            # executar hook de início
            run_hook(meta, "pre_build_start", sb)

            # Fetch
            fetched = fetch_sources(meta)
            if not fetched:
                LOG.error(f"Fetch falhou para {name}")
                return False

            # Extract
            extracted = extract_sources(meta, fetched, sandbox=sb, force=force)
            if not extracted:
                LOG.error(f"Extração falhou para {name}")
                return False

            # Patch
            if not apply_patches(meta, extracted, sandbox=sb):
                LOG.error(f"Patches falharam para {name}")
                return False

            # Hooks
            run_hook(meta, "pre_configure", sb)

            # Configure
            configure(meta, extracted, sandbox=sb, force=force)

            # pre_build hook
            run_hook(meta, "pre_build", sb)

            # Compile
            compile_port(meta, extracted, sandbox=sb, force=force)

            # post_build hook
            run_hook(meta, "post_build", sb)

            # pre_install hook
            run_hook(meta, "pre_install", sb)

            # Empacotar
            metadata_file = sb.write_metadata(name, version, meta)
            pkg = package_from_metadata(metadata_file)
            LOG.info(f"Pacote criado: {pkg}")

            # Instalar se não dry_run
            if not dry_run:
                install_package(pkg, sandbox=sb, force=force, dry_run=dry_run)

            # post_install hook
            run_hook(meta, "post_install", sb)

            # hook de fim
            run_hook(meta, "post_build_end", sb)

            # Marcar instalado no DB
            db = self.load_installed()
            db[name] = {"version": version, "pkg": str(pkg), "built_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
            self.save_installed(db)

            LOG.info(f"Build de {name} finalizado com sucesso.")

            # notificação se configurado
            if self.cfg.get("notify", {}).get("enabled", False):
                try:
                    from pyport.core import _notify
                    _notify(f"PyPort: Build de {name} concluído")
                except Exception:
                    pass

            return True

        except Exception as e:
            LOG.error(f"Erro no build de {name}: {e}")
            return False

        finally:
            try:
                sb.destroy(clean=not self.cfg.get("sandbox", {}).get("keep", False))
            except Exception as e:
                LOG.warning(f"Erro destruindo sandbox para {name}: {e}")

    def remove(self, portname: str, force: bool = False, dry_run: bool = False, yes: bool = False) -> bool:
        LOG.info(f"Remover iniciado: {portname}, force={force}, dry_run={dry_run}, yes={yes}")
        try:
            res = remove_package(portname, force=force, dry_run=dry_run, yes=yes)
            if res.get("status") == "ok":
                # remover hook
                LOG.info(f"{portname} removido com sucesso")
                return True
            else:
                LOG.error(f"Remoção falhou: {res.get('message')}")
                return False
        except Exception as e:
            LOG.error(f"Erro em remove de {portname}: {e}")
            return False

    def sync(self) -> bool:
        LOG.info("Sincronização iniciada")
        repo = self.cfg.get("repo", {})
        url = repo.get("url")
        branch = repo.get("branch", "main")
        if not url:
            LOG.error("repo.url não configurado")
            return False
        ports = self.ports_root
        try:
            if (ports / ".git").exists():
                subprocess.run(["git", "-C", str(ports), "fetch", "--all"], check=True)
                subprocess.run(["git", "-C", str(ports), "checkout", branch], check=True)
                subprocess.run(["git", "-C", str(ports), "pull", "origin", branch], check=True)
                LOG.info("Sync via git concluído")
                return True
            else:
                LOG.error("Diretório de ports não é repositório git")
                return False
        except Exception as e:
            LOG.error(f"Erro sincronizando ports: {e}")
            return False

    def info(self, portname: str) -> None:
        pf = self.ports_root / portname / "Portfile.yaml"
        if not pf.exists():
            LOG.error(f"Portfile não encontrado para {portname}")
            return
        try:
            meta = yaml.safe_load(pf.read_text(encoding="utf-8"))
        except Exception as e:
            LOG.error(f"Erro lendo Portfile {portname}: {e}")
            return
        name = meta.get("name", portname)
        version = meta.get("version", "unknown")
        deps = meta.get("depends", [])
        installed = self.load_installed()
        status = "installed" if name in installed else "not installed"
        LOG.info(f"Port: {name}")
        LOG.info(f" Version: {version}")
        LOG.info(f" Dependências: {deps}")
        LOG.info(f" Status: {status}")
        if status == "installed":
            LOG.info(f" Instalado versão: {installed[name].get('version')}")
        # show update info if present
        if "update" in meta:
            LOG.info(f" Pode atualizar via: {meta['update']}")

    def list_ports(self) -> None:
        if not self.ports_root.exists():
            LOG.error(f"Pasta de ports não encontrada: {self.ports_root}")
            return
        for d in sorted(self.ports_root.iterdir()):
            if d.is_dir():
                pf = d / "Portfile.yaml"
                if pf.exists():
                    try:
                        meta = yaml.safe_load(pf.read_text(encoding="utf-8"))
                        name = meta.get("name", d.name)
                        version = meta.get("version", "unknown")
                        print(f"{name} - {version}")
                    except Exception as e:
                        print(f"{d.name} - erro ao ler versão: {e}")

    # CLI exposta
    @staticmethod
    def cli():
        import argparse
        parser = argparse.ArgumentParser(prog="pyport", description="PyPort core CLI")
        sub = parser.add_subparsers(dest="cmd", required=True)

        p_build = sub.add_parser("build", help="Construir port")
        p_build.add_argument("portname")
        p_build.add_argument("--force", action="store_true")
        p_build.add_argument("--dry-run", action="store_true")

        p_remove = sub.add_parser("remove", help="Remover port instalado")
        p_remove.add_argument("portname")
        p_remove.add_argument("--force", action="store_true")
        p_remove.add_argument("--dry-run", action="store_true")
        p_remove.add_argument("--yes", action="store_true")

        p_sync = sub.add_parser("sync", help="Sincronizar ports tree")

        p_info = sub.add_parser("info", help="Mostrar info de port")
        p_info.add_argument("portname")

        p_list = sub.add_parser("list", help="Listar ports disponíveis")

        args = parser.parse_args()

        core = Core()

        if args.cmd == "build":
            ret = core.build(args.portname, force=args.force, dry_run=args.dry_run)
            sys.exit(0 if ret else 1)
        elif args.cmd == "remove":
            ret = core.remove(args.portname, force=args.force, dry_run=args.dry_run, yes=args.yes)
            sys.exit(0 if ret else 1)
        elif args.cmd == "sync":
            ret = core.sync()
            sys.exit(0 if ret else 1)
        elif args.cmd == "info":
            core.info(args.portname)
        elif args.cmd == "list":
            core.list_ports()
        else:
            parser.print_help()

def main():
    Core.cli()

if __name__ == "__main__":
    main()
