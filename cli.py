#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
cli.py — Interface de linha de comando do PyPort

Chamadas suportadas:
  build, remove, sync, info, list, install, search, update,
  toolchain, chroot

Com abreviaturas, cores, integração de todos os módulos
"""

import os
import sys
import argparse
from pathlib import Path
import json

from pyport.logger import get_logger
from pyport.config import get_config
from pyport.core import Core
from pyport.dependency import DependencyGraph
from pyport.update import check_updates
from pyport.search import search_ports
from pyport.info import info_ports
from pyport.install import install_package
from pyport.fakeroot import Fakerunner
from pyport.toolchain import ToolchainManager
from pyport.sandbox import ChrootManager

LOG = get_logger("pyport.cli")

def _print_colored(text: str, color_code: str, enabled=True):
    if enabled:
        print(f"\033[{color_code}m{text}\033[0m")
    else:
        print(text)

def main():
    cfg = get_config()
    core = Core()
    toolchain = ToolchainManager(cfg)
    chroot = ChrootManager(cfg)

    parser = argparse.ArgumentParser(prog="pyport",
                                     description="PyPort - Gerenciador de ports source para Linux",
                                     formatter_class=argparse.RawTextHelpFormatter)

    # Abreviações
    cmd_aliases = {
        "b": "build",
        "rm": "remove",
        "r": "remove",
        "i": "install",
        "s": "sync",
        "ls": "list",
        "u": "update",
        "upd": "update",
        "se": "search",
        "tc": "toolchain",
        "ch": "chroot"
    }

    sub = parser.add_subparsers(dest="cmd", required=True, help="comando a executar")

    # build
    p_build = sub.add_parser("build", help="Construir um port")
    p_build.add_argument("portname", help="Nome do port a construir")
    p_build.add_argument("--force", "-f", action="store_true", help="Forçar rebuild")
    p_build.add_argument("--dry-run", "-d", action="store_true", help="Simular sem executar")

    # remove
    p_remove = sub.add_parser("remove", help="Remover um port instalado")
    p_remove.add_argument("portname", help="Nome do port a remover")
    p_remove.add_argument("--force", "-f", action="store_true", help="Ignorar dependências reversas")
    p_remove.add_argument("--dry-run", "-d", action="store_true", help="Simular sem executar")
    p_remove.add_argument("--yes", "-y", action="store_true", help="Confirmar automaticamente")

    # sync
    sub.add_parser("sync", help="Sincronizar árvore de ports")

    # info
    p_info = sub.add_parser("info", help="Mostrar informação de um port")
    p_info.add_argument("portname", help="Nome do port")

    # list
    sub.add_parser("list", help="Listar todos os ports disponíveis")

    # install
    p_install = sub.add_parser("install", help="Instalar pacote gerado")
    p_install.add_argument("package_file", help="Arquivo de pacote ou nome")
    p_install.add_argument("--force", "-f", action="store_true")
    p_install.add_argument("--dry-run", "-d", action="store_true")

    # search
    p_search = sub.add_parser("search", help="Procurar ports")
    p_search.add_argument("term", help="Termo de busca")
    p_search.add_argument("--limit", "-l", type=int, default=20)

    # update
    sub.add_parser("update", help="Verificar atualizações disponíveis")

    # toolchain
    p_tc = sub.add_parser("toolchain", help="Gerenciar toolchain em /mnt/tools")
    tc_sub = p_tc.add_subparsers(dest="tc_cmd", required=True)

    p_tc_create = tc_sub.add_parser("create", help="Criar toolchain base")
    p_tc_create.add_argument("--arch", default="x86_64", help="Arquitetura alvo")

    tc_sub.add_parser("remove", help="Remover toolchain")
    tc_sub.add_parser("list", help="Listar toolchains disponíveis")

    # chroot
    p_ch = sub.add_parser("chroot", help="Gerenciar ambiente chroot")
    ch_sub = p_ch.add_subparsers(dest="ch_cmd", required=True)

    ch_sub.add_parser("enter", help="Entrar no chroot")
    ch_sub.add_parser("clean", help="Desmontar e limpar chroot")

    # opções globais
    parser.add_argument("--color", action="store_true", help="Forçar saída colorida")
    parser.add_argument("--no-color", action="store_true", help="Desativar cores")
    parser.add_argument("--verbose", "-v", action="count", default=0, help="Mais verbosidade")

    args = parser.parse_args()

    # Resolver abreviações
    cmd = cmd_aliases.get(args.cmd, args.cmd)

    # Verbosidade
    if args.verbose >= 2:
        LOG.setLevel("DEBUG")
    elif args.verbose == 1:
        LOG.setLevel("INFO")

    use_color = not args.no_color

    try:
        if cmd == "build":
            res = core.build(args.portname, force=args.force, dry_run=args.dry_run)
            sys.exit(0 if res else 1)

        elif cmd == "remove":
            res = core.remove(args.portname, force=args.force, dry_run=args.dry_run, yes=args.yes)
            sys.exit(0 if res else 1)

        elif cmd == "sync":
            sys.exit(0 if core.sync() else 1)

        elif cmd == "info":
            core.info(args.portname)
            sys.exit(0)

        elif cmd == "list":
            core.list_ports()
            sys.exit(0)

        elif cmd == "install":
            pkg = Path(args.package_file)
            if not pkg.exists():
                pkgdir = Path(cfg["paths"]["packages"])
                candidate = pkgdir / args.package_file
                if candidate.exists():
                    pkg = candidate
                else:
                    LOG.error(f"Pacote {args.package_file} não encontrado")
                    sys.exit(1)
            fr = Fakerunner(dry_run=args.dry_run, debug=(args.verbose>=2))
            res = install_package(pkg, sandbox=fr, force=args.force, dry_run=args.dry_run)
            sys.exit(0 if res else 1)

        elif cmd == "search":
            matches = search_ports(args.term, limit=args.limit)
            for m in matches:
                _print_colored(m, "33", enabled=use_color)
            sys.exit(0)

        elif cmd == "update":
            upd = check_updates()
            if not upd:
                LOG.info("Nenhuma atualização encontrada.")
                sys.exit(0)
            for portname, current, latest in upd:
                _print_colored(f"{portname}: {current} → {latest}", "32", enabled=use_color)
            repfile = cfg.get("update", {}).get("report_file")
            if repfile:
                with open(repfile, "w", encoding="utf-8") as f:
                    f.write(json.dumps(upd, indent=2, ensure_ascii=False))
                LOG.info(f"Relatório salvo em {repfile}")
            sys.exit(0)

        elif cmd == "toolchain":
            if args.tc_cmd == "create":
                res = toolchain.create(args.arch)
                sys.exit(0 if res else 1)
            elif args.tc_cmd == "remove":
                sys.exit(0 if toolchain.remove() else 1)
            elif args.tc_cmd == "list":
                toolchain.list()
                sys.exit(0)

        elif cmd == "chroot":
            if args.ch_cmd == "enter":
                sys.exit(0 if chroot.enter() else 1)
            elif args.ch_cmd == "clean":
                sys.exit(0 if chroot.clean() else 1)

        else:
            parser.print_help()
            sys.exit(1)

    except Exception as e:
        LOG.error(f"Erro no comando {cmd}: {e}")
        if args.verbose >= 2:
            import traceback
            traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
