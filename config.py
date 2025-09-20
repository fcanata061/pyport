#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
config.py - Configuração central do PyPort (versão evoluída)

Recursos:
 - Defaults internos
 - Arquivos externos: /etc/pyport/config.yaml e ~/.config/pyport/config.yaml
 - Override via variáveis de ambiente (PYPORT_SECTION_KEY)
 - Cache para performance
 - CLI para get/set/unset/export/reset
 - Backup automático ao salvar
 - Perfis de configuração
"""

import os
import sys
import json
import shutil
import yaml
import time
from pathlib import Path
from functools import lru_cache
from typing import Dict, Any, Optional

from pyport.logger import get_logger

log = get_logger("pyport.config")

SYSTEM_CONFIG = Path("/etc/pyport/config.yaml")
USER_CONFIG = Path.home() / ".config/pyport/config.yaml"
BACKUP_DIR = Path.home() / ".config/pyport/backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)

DEFAULTS: Dict[str, Any] = {
    "profile": "default",
    "paths": {
        "ports": "/usr/ports",
        "modules": "/pyport/modules",
        "db": "/pyport/db",
        "logs": "/pyport/logs",
        "packages": "/pyport/packages",
        "sandbox": "/pyport/sandbox",
        "toolchain": "/mnt/tools",
    },
    "sandbox": {
        "mode": "fakeroot",  # fakeroot, bwrap, both, none
    },
}

DOCS: Dict[str, str] = {
    "profile": "Perfil de configuração (default/dev/prod/etc)",
    "paths.ports": "Diretório dos Portfiles",
    "paths.modules": "Diretório dos módulos internos do PyPort",
    "paths.db": "Banco de dados de pacotes instalados",
    "paths.logs": "Diretório para logs",
    "paths.packages": "Local de pacotes empacotados",
    "paths.sandbox": "Sandbox de build/instalação",
    "paths.toolchain": "Toolchain construído pelo PyPort",
    "sandbox.mode": "Modo de sandbox (fakeroot, bwrap, both, none)",
}

# -------------------- Helpers --------------------

def load_yaml(path: Path) -> Dict[str, Any]:
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
        except Exception as e:
            log.error(f"Erro lendo {path}: {e}")
    return {}

def merge_dicts(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    result = base.copy()
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(result.get(k), dict):
            result[k] = merge_dicts(result[k], v)
        else:
            result[k] = v
    return result

def backup_file(path: Path):
    if not path.exists():
        return
    ts = time.strftime("%Y%m%d-%H%M%S")
    backup_path = BACKUP_DIR / f"{path.name}.{ts}.bak"
    shutil.copy2(path, backup_path)
    log.info(f"Backup criado: {backup_path}")

def save_yaml(path: Path, data: Dict[str, Any]):
    path.parent.mkdir(parents=True, exist_ok=True)
    backup_file(path)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, sort_keys=False, allow_unicode=True)
    log.info(f"Config salva em {path}")

def apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    for key, value in os.environ.items():
        if key.startswith("PYPORT_"):
            parts = key.lower().split("_")[1:]
            d = cfg
            for p in parts[:-1]:
                d = d.setdefault(p, {})
            d[parts[-1]] = value
    return cfg

def validate(cfg: Dict[str, Any]) -> Dict[str, Any]:
    # sandbox mode válido
    mode = cfg.get("sandbox", {}).get("mode", "fakeroot")
    if mode not in ["fakeroot", "bwrap", "both", "none"]:
        log.warning(f"Sandbox mode inválido '{mode}', resetando para 'fakeroot'")
        cfg["sandbox"]["mode"] = "fakeroot"

    # paths são strings
    for k, v in cfg.get("paths", {}).items():
        if not isinstance(v, str):
            log.warning(f"Path '{k}' inválido: {v} (resetando para default)")
            cfg["paths"][k] = DEFAULTS["paths"][k]

    return cfg

# -------------------- Config --------------------

@lru_cache()
def get_config() -> Dict[str, Any]:
    cfg = DEFAULTS.copy()

    # system
    sys_cfg = load_yaml(SYSTEM_CONFIG)
    cfg = merge_dicts(cfg, sys_cfg)

    # user
    usr_cfg = load_yaml(USER_CONFIG)
    cfg = merge_dicts(cfg, usr_cfg)

    # env
    cfg = apply_env_overrides(cfg)

    # validate
    cfg = validate(cfg)

    # expand ~
    for k, v in cfg.get("paths", {}).items():
        cfg["paths"][k] = str(Path(v).expanduser())

    return cfg

def set_config(key: str, value: Any):
    cfg = get_config()
    keys = key.split(".")
    d = cfg
    for k in keys[:-1]:
        d = d.setdefault(k, {})
    d[keys[-1]] = value
    save_yaml(USER_CONFIG, cfg)
    get_config.cache_clear()

def unset_config(key: str):
    cfg = get_config()
    keys = key.split(".")
    d = cfg
    for k in keys[:-1]:
        d = d.get(k, {})
    if keys[-1] in d:
        del d[keys[-1]]
    save_yaml(USER_CONFIG, cfg)
    get_config.cache_clear()

def reset_config():
    backup_file(USER_CONFIG)
    if USER_CONFIG.exists():
        USER_CONFIG.unlink()
    get_config.cache_clear()
    log.info("Config resetada para defaults")

# -------------------- CLI --------------------

def _cli():
    import argparse
    parser = argparse.ArgumentParser(description="PyPort Config")
    parser.add_argument("--json", action="store_true", help="Mostrar config em JSON")
    parser.add_argument("--get", metavar="KEY", help="Obter valor (ex: paths.logs)")
    parser.add_argument("--set", nargs=2, metavar=("KEY", "VALUE"), help="Definir valor")
    parser.add_argument("--unset", metavar="KEY", help="Remover chave")
    parser.add_argument("--reset", action="store_true", help="Resetar para defaults")
    parser.add_argument("--export", metavar="FILE", help="Exportar config atual")
    parser.add_argument("--describe", metavar="KEY", help="Mostrar documentação de chave")
    args = parser.parse_args()

    cfg = get_config()

    if args.json:
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
    elif args.get:
        keys = args.get.split(".")
        val = cfg
        for k in keys:
            val = val.get(k, {})
        print(val)
    elif args.set:
        set_config(args.set[0], args.set[1])
        print(f"{args.set[0]} atualizado para {args.set[1]}")
    elif args.unset:
        unset_config(args.unset)
        print(f"{args.unset} removido")
    elif args.reset:
        reset_config()
        print("Config resetada para defaults")
    elif args.export:
        out = Path(args.export)
        out.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Config exportada para {out}")
    elif args.describe:
        print(DOCS.get(args.describe, "Sem documentação disponível"))
    else:
        parser.print_help()

if __name__ == "__main__":
    _cli()
