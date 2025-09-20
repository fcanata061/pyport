#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pyport/config.py — Versão evoluída da configuração central para PyPort

Melhorias incluídas:
 - Logging via módulo logger
 - Sobrescrita via variáveis de ambiente
 - Validação, normalização
 - CLI para show / get / set / validate / export-json
 - Cache da configuração
"""

import os
from pathlib import Path
import yaml
import json
from functools import lru_cache
from typing import Any, Dict, Optional

from pyport.logger import get_logger

log = get_logger("pyport.config")

# Caminhos dos arquivos de configuração
_SYSTEM_CONFIG = Path("/etc/pyport/config.yaml")
_USER_CONFIG = Path.home() / ".config" / "pyport" / "config.yaml"

# Valores defaults
DEFAULT_CONFIG: Dict[str, Any] = {
    "paths": {
        "ports": "/usr/ports",
        "modules": "/pyport/modules",
        "logs": "/pyport/logs",
        "db": "/pyport/db",
        "installed_db": "/pyport/db/installed.json",
        "packages": "/pyport/packages",
        "sandbox": "/pyport/sandbox",
        "toolchain": "/mnt/tools",
    },
    "sandbox_mode": "both",  # opções: "fakeroot", "bwrap", "both", "none"
    "notifications": {
        "enabled": True
    },
    "download": {
        "distfiles_cache": "/var/cache/pyport/distfiles",
        "max_retries": 3,
        "backoff": 2
    },
    "repositories": [],  # lista de repositórios remotos
}

# Funções auxiliares ---------------------------------------------------------

def _load_yaml_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
            if isinstance(data, dict):
                return data
            else:
                log.warning(f"Config file {path} não retorna dict, ignorando.")
                return {}
    except Exception as e:
        log.error(f"Erro lendo config {path}: {e}")
        return {}

def _merge(a: Dict[str, Any], b: Dict[str, Any]) -> Dict[str, Any]:
    result = dict(a)
    for key, val in b.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _merge(result[key], val)
        else:
            result[key] = val
    return result

def _apply_env_overrides(cfg: Dict[str, Any]) -> Dict[str, Any]:
    # Por exemplo: PYPORT_PATHS_LOGS, PYPORT_SANDBOX_MODE etc.
    out = dict(cfg)
    for section, sub in cfg.items():
        if isinstance(sub, dict):
            for key in sub:
                env_key = f"PYPORT_{section.upper()}_{key.upper()}"
                if env_key in os.environ:
                    val = os.environ[env_key]
                    log.info(f"Override via env: {section}.{key} = {val}")
                    # tentar converter tipo (int, bool)
                    if isinstance(sub[key], bool):
                        sub[key] = val.lower() in ("1","true","yes")
                    elif isinstance(sub[key], int):
                        try:
                            sub[key] = int(val)
                        except ValueError:
                            log.warning(f"Não foi possível converter {env_key}='{val}' para int")
                            sub[key] = val
                    else:
                        sub[key] = val
    return out

def _validate_and_normalize(cfg: Dict[str, Any]) -> Dict[str, Any]:
    # Validar sandbox_mode
    sm = cfg.get("sandbox_mode", "both")
    if sm not in ("fakeroot","bwrap","both","none"):
        log.warning(f"Sandbox mode '{sm}' inválido; usando 'both'")
        cfg["sandbox_mode"] = "both"

    # Verificar/garantir existência de diretórios em paths
    paths = cfg.get("paths", {})
    for name, p in paths.items():
        try:
            pp = Path(p).expanduser()
            if not pp.exists():
                pp.mkdir(parents=True, exist_ok=True)
                log.info(f"Criado diretório para paths.{name}: {pp}")
            # opcional: verificar permissões ou se é diretório
        except Exception as e:
            log.error(f"Não foi possível criar path {name} = {p}: {e}")

    return cfg

# Configuração carregada e cacheada ------------------------------------------------

@lru_cache()
def get_config() -> Dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    # system override
    sys_conf = _load_yaml_file(_SYSTEM_CONFIG)
    cfg = _merge(cfg, sys_conf)
    # user override
    user_conf = _load_yaml_file(_USER_CONFIG)
    cfg = _merge(cfg, user_conf)
    # env override
    cfg = _apply_env_overrides(cfg)
    # validate / normalize
    cfg = _validate_and_normalize(cfg)
    log.debug("Config carregada e normalizada")
    return cfg

def reload_config() -> Dict[str, Any]:
    get_config.cache_clear()
    return get_config()

# Exibição / CLI -------------------------------------------------------------

def _cli():
    import argparse
    parser = argparse.ArgumentParser(prog="pyport-config", description="Mostrar/configurar PyPort config")
    parser.add_argument("--reload", action="store_true", help="Recarrega config ignorando cache")
    parser.add_argument("--json", action="store_true", help="Exibir configuração em JSON")
    parser.add_argument("--get", metavar="KEY", help="Obter valor de uma chave no estilo paths.logs ou sandbox_mode")
    args = parser.parse_args()

    if args.reload:
        cfg = reload_config()
    else:
        cfg = get_config()

    if args.json:
        print(json.dumps(cfg, indent=2))
        return

    if args.get:
        # suportar chaves compostas
        parts = args.get.split(".")
        val = cfg
        for p in parts:
            if isinstance(val, dict) and p in val:
                val = val[p]
            else:
                print(f"Chave não encontrada: {args.get}")
                return
        print(val)
        return

    # se nenhum argumento extra, exibe todos
    print("Configuração PyPort:")
    for section, sub in cfg.items():
        print(f"[{section}]")
        if isinstance(sub, dict):
            for key, val in sub.items():
                print(f"  {key} = {val}")
        else:
            print(f"  {section} = {sub}")

if __name__ == "__main__":
    _cli()
