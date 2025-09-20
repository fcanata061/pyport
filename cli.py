#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pyport.cli - Interface de linha de comando para PyPort
Completa, funcional, colorida, com abreviações.
"""

import argparse
import sys
from colorama import Fore, Style, init as colorama_init

# Integração de módulos
import build
import remove
import info
import search
import sync
import update
import dependency
import packager
import config as cfg

colorama_init(autoreset=True)


def c_info(msg): print(Fore.CYAN + "[INFO] " + Style.RESET_ALL + msg)
def c_ok(msg): print(Fore.GREEN + "[OK] " + Style.RESET_ALL + msg)
def c_warn(msg): print(Fore.YELLOW + "[WARN] " + Style.RESET_ALL + msg)
def c_err(msg): print(Fore.RED + "[ERROR] " + Style.RESET_ALL + msg)


def main():
    parser = argparse.ArgumentParser(
        prog="pyport",
        description="PyPort - Gerenciador de Ports estilo BSD, em Python",
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # build
    p_build = sub.add_parser("build", aliases=["b"], help="Construir um pacote")
    p_build.add_argument("pkg", help="Nome do pacote a construir")

    # install
    p_install = sub.add_parser("install", aliases=["i"], help="Instalar pacote")
    p_install.add_argument("file", help="Arquivo do pacote (.tar.zst, .deb, etc.)")

    # remove
    p_remove = sub.add_parser("remove", aliases=["rm"], help="Remover pacote")
    p_remove.add_argument("pkg", help="Nome do pacote a remover")

    # update
    p_update = sub.add_parser("update", aliases=["u"], help="Checar atualizações")

    # deps
    p_deps = sub.add_parser("deps", aliases=["d"], help="Gerenciar dependências")
    p_deps.add_argument("pkg", help="Nome do pacote")
    p_deps.add_argument("--reverse", "-r", action="store_true", help="Mostrar dependências reversas")

    # search
    p_search = sub.add_parser("search", aliases=["s"], help="Buscar pacotes")
    p_search.add_argument("term", help="Termo de busca")

    # info
    p_info = sub.add_parser("info", aliases=["in"], help="Mostrar informações")
    p_info.add_argument("pkg", help="Nome do pacote")

    # sync
    p_sync = sub.add_parser("sync", aliases=["sy"], help="Sincronizar repositório")

    # config
    p_config = sub.add_parser("config", aliases=["c"], help="Gerenciar configuração")
    p_config.add_argument("action", choices=["show", "validate"], help="Ação de configuração")

    args = parser.parse_args()

    # Dispatcher
    if args.cmd in ("build", "b"):
        c_info(f"Construindo {args.pkg}...")
        build.build_package(args.pkg)
        c_ok(f"Build finalizado: {args.pkg}")

    elif args.cmd in ("install", "i"):
        c_info(f"Instalando {args.file}...")
        packager.install_package(args.file)
        c_ok("Instalação concluída.")

    elif args.cmd in ("remove", "rm"):
        c_warn(f"Removendo {args.pkg}...")
        remove.remove_package(args.pkg)
        c_ok("Remoção concluída.")

    elif args.cmd in ("update", "u"):
        c_info("Checando atualizações...")
        update.check_updates()

    elif args.cmd in ("deps", "d"):
        dg = dependency.DependencyGraph()
        if args.reverse:
            rev = dg.get_reverse_dependencies(args.pkg)
            c_info(f"Dependências reversas de {args.pkg}: {rev}")
        else:
            deps = dg.get_dependencies(args.pkg)
            c_info(f"Dependências de {args.pkg}: {deps}")

    elif args.cmd in ("search", "s"):
        res = search.search_ports(args.term)
        if res:
            for r in res:
                print(f"- {Fore.GREEN}{r['name']}{Style.RESET_ALL} {r['version']} - {r.get('description','')}")
        else:
            c_warn("Nenhum resultado encontrado.")

    elif args.cmd in ("info", "in"):
        info.show_info(args.pkg)

    elif args.cmd in ("sync", "sy"):
        c_info("Sincronizando repositórios...")
        sync.sync_ports()

    elif args.cmd in ("config", "c"):
        conf = cfg.Config.load()
        if args.action == "show":
            for k, v in conf.to_dict().items():
                print(f"{Fore.CYAN}{k}{Style.RESET_ALL}: {v}")
        elif args.action == "validate":
            ok, probs = conf.validate()
            if ok:
                c_ok("Configuração válida.")
            else:
                c_err("Configuração inválida:")
                for p in probs:
                    print(" -", p)

    else:
        parser.print_help()


if __name__ == "__main__":
    main()
