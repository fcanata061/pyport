#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
pyport.config - configuração central do PyPort (evoluído, completo e funcional)

Principais responsabilidades:
 - Carregar configuração do sistema (/etc/pyport/config.yaml) e do usuário (~/.config/pyport/config.yaml)
 - Fornecer valores default e override hierárquico (user > system > defaults)
 - Helpers para acessar paths usados por outros módulos (build_root, ports_dir, distfiles_cache, toolchain_dir, log_dir, packages_dir)
 - Gerenciamento de repositórios remotos (add/list/remove/update)
 - Validação e normalização (paths, booleans, sandbox_mode)
 - Export / import de configuração (yaml/json)
 - Geração de um exemplo de unit systemd para agendamento de sync
 - CLI para gerenciar configuração e repositórios
 - Não depende obrigatoriamente de PyYAML (usa fallback simples)
"""

from __future__ import annotations
import os
import sys
import json
import shutil
import argparse
import getpass
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

# Try to use PyYAML if available
try:
    import yaml  # type: ignore
    _HAS_YAML = True
except Exception:
    _HAS_YAML = False

# Defaults
_DEFAULTS: Dict[str, Any] = {
    "build_root": "/pyport/build",
    "ports_dir": "/usr/ports",
    "log_dir": "/pyport/logs",
    "packages_dir": "/pyport/packages",
    "distfiles_cache": "/var/cache/pyport/distfiles",
    "toolchain_dir": "/mnt/tools",
    "sandbox_mode": "both",     # "fakeroot", "bwrap", "both", "none"
    "max_download_retries": 3,
    "download_backoff": 2,
    "keep_build_on_success": False,
    "7z_cmd": "7z",
    "repositories": [          # sample list of repo definitions
        # {"name": "main", "type": "git", "url": "https://example.org/ports.git", "branch": "main"}
    ],
    "notification": {
        "enabled": True
    },
    "systemd": {
        "enable_sync_service": False
    }
}

# Standard config file paths
_SYSTEM_CONFIG = Path("/etc/pyport/config.yaml")
_USER_CONFIG = Path.home() / ".config" / "pyport" / "config.yaml"

# Helper functions -----------------------------------------------------------

def _read_yaml_text(text: str) -> Dict[str, Any]:
    """Fallback YAML-ish parser (very small, only for basic mappings and lists)"""
    if _HAS_YAML:
        return yaml.safe_load(text) or {}
    out: Dict[str, Any] = {}
    cur_key = None
    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if ":" in s and not s.startswith("-"):
            k, v = s.split(":", 1)
            k = k.strip()
            v = v.strip()
            if v == "":
                # start of block/list
                out[k] = []
                cur_key = k
            else:
                # try to parse basic types
                if v.lower() in ("true","false"):
                    out[k] = v.lower() == "true"
                else:
                    try:
                        out[k] = int(v)
                    except Exception:
                        out[k] = v
                cur_key = None
        elif s.startswith("- ") and cur_key is not None:
            out[cur_key].append(s[2:].strip())
        else:
            # ignore complex syntax in fallback
            continue
    return out

def _load_file(p: Path) -> Dict[str, Any]:
    if not p.exists():
        return {}
    try:
        txt = p.read_text(encoding="utf-8")
        return _read_yaml_text(txt)
    except Exception:
        return {}

def _write_yaml(p: Path, data: Dict[str, Any]) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    if _HAS_YAML:
        with open(p, "w", encoding="utf-8") as f:
            yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False, allow_unicode=True)
    else:
        # crude serializer for simple structures
        with open(p, "w", encoding="utf-8") as f:
            for k, v in data.items():
                if isinstance(v, dict):
                    f.write(f"{k}:\n")
                    for kk, vv in v.items():
                        f.write(f"  {kk}: {vv}\n")
                elif isinstance(v, list):
                    f.write(f"{k}:\n")
                    for item in v:
                        f.write(f"  - {item}\n")
                else:
                    f.write(f"{k}: {v}\n")

# Config class ---------------------------------------------------------------

class Config:
    """
    Central configuration object. Loads system and user configs, merges them with defaults.
    Use Config.load() or Config.from_files(...) to obtain an instance.
    """

    def __init__(self, data: Optional[Dict[str, Any]] = None) -> None:
        self._data = dict(_DEFAULTS)
        if data:
            self._merge(data)

    @classmethod
    def load(cls, system_file: Path = _SYSTEM_CONFIG, user_file: Path = _USER_CONFIG) -> "Config":
        syscfg = _load_file(system_file)
        usercfg = _load_file(user_file)
        cfg = cls(syscfg)
        cfg._merge(usercfg)
        cfg._normalize()
        return cfg

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "Config":
        return cls(data)

    def _merge(self, other: Dict[str, Any]) -> None:
        # shallow merge; for lists/dicts we usually replace
        for k, v in other.items():
            if v is None:
                continue
            self._data[k] = v

    def _normalize(self) -> None:
        # normalize various path keys to absolute Path strings
        for key in ("build_root","ports_dir","log_dir","packages_dir","distfiles_cache","toolchain_dir"):
            val = self._data.get(key)
            if val is None:
                continue
            p = Path(str(val)).expanduser()
            self._data[key] = str(p)
            try:
                p.mkdir(parents=True, exist_ok=True)
            except Exception:
                pass
        # clamp sandbox_mode
        sm = str(self._data.get("sandbox_mode","both")).lower()
        if sm not in ("fakeroot","bwrap","both","none"):
            sm = "both"
        self._data["sandbox_mode"] = sm
        # ensure repositories is a list
        repos = self._data.get("repositories", []) or []
        if not isinstance(repos, list):
            repos = []
        self._data["repositories"] = repos

    # accessors ---------------------------------------------------------------

    def get(self, key: str, default: Any = None) -> Any:
        return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._data[key] = value
        self._normalize()

    def to_dict(self) -> Dict[str, Any]:
        return dict(self._data)

    # repositories management -------------------------------------------------

    def list_repositories(self) -> List[Dict[str, Any]]:
        return list(self._data.get("repositories", []))

    def find_repository(self, name_or_url: str) -> Optional[Dict[str, Any]]:
        for r in self.list_repositories():
            if r.get("name") == name_or_url or r.get("url") == name_or_url:
                return r
        return None

    def add_repository(self, name: str, url: str, typ: str = "git", branch: Optional[str] = None, checksum: Optional[str] = None) -> None:
        if self.find_repository(name):
            raise ValueError(f"repo with name '{name}' already exists")
        entry: Dict[str, Any] = {"name": name, "url": url, "type": typ}
        if branch:
            entry["branch"] = branch
        if checksum:
            entry["checksum"] = checksum
        self._data.setdefault("repositories", []).append(entry)

    def remove_repository(self, name_or_url: str) -> bool:
        repos = self._data.get("repositories", [])
        for i, r in enumerate(list(repos)):
            if r.get("name") == name_or_url or r.get("url") == name_or_url:
                repos.pop(i)
                self._data["repositories"] = repos
                return True
        return False

    def update_repository(self, name_or_url: str, **kwargs) -> bool:
        r = self.find_repository(name_or_url)
        if not r:
            return False
        for k, v in kwargs.items():
            if v is None:
                continue
            r[k] = v
        return True

    # persist config ----------------------------------------------------------

    def save_system(self, path: Path = _SYSTEM_CONFIG) -> None:
        _write_yaml(path, self.to_dict())

    def save_user(self, path: Path = _USER_CONFIG) -> None:
        _write_yaml(path, self.to_dict())

    # validation --------------------------------------------------------------

    def validate(self) -> Tuple[bool, List[str]]:
        """
        Validate config; returns (is_valid, list_of_problems)
        """
        problems: List[str] = []
        # check essential dirs exist or are creatable
        for key in ("build_root","ports_dir","log_dir","packages_dir","distfiles_cache","toolchain_dir"):
            p = Path(self.get(key))
            if not p.exists():
                try:
                    p.mkdir(parents=True, exist_ok=True)
                except Exception:
                    problems.append(f"path {p} doesn't exist and cannot be created")
        # repo validation
        for repo in self.list_repositories():
            if "name" not in repo or "url" not in repo:
                problems.append(f"repository entry incomplete: {repo}")
            if repo.get("type") not in ("git","http","https","ftp"):
                problems.append(f"unsupported repo type for {repo.get('name')}: {repo.get('type')}")
        # sandbox_mode valid
        if self.get("sandbox_mode") not in ("fakeroot","bwrap","both","none"):
            problems.append("sandbox_mode must be one of: fakeroot, bwrap, both, none")
        return (len(problems) == 0, problems)

    # helpers used by other modules ------------------------------------------

    def paths(self) -> Dict[str, Path]:
        """Return common paths as Path objects"""
        keys = ("build_root","ports_dir","log_dir","packages_dir","distfiles_cache","toolchain_dir")
        return {k: Path(self.get(k)) for k in keys}

    def sandbox_mode(self) -> str:
        return str(self.get("sandbox_mode","both"))

    # systemd unit generator --------------------------------------------------

    def systemd_sync_unit(self, service_name: str = "pyport-sync", user: Optional[str] = None, exec_path: str = "/usr/bin/pyport-sync"):
        """
        Generate a simple systemd service + timer unit pair as text.
        user: if provided, generate user-level unit
        exec_path: path to the pyport sync executable/cli
        """
        if user is None:
            user = getpass.getuser()
        unit = (f"[Unit]\nDescription=PyPort periodic sync service\nAfter=network.target\n\n"
                f"[Service]\nType=oneshot\nUser={user}\nExecStart={exec_path} sync\n\n"
                f"[Install]\nWantedBy=multi-user.target\n")
        timer = ("[Unit]\nDescription=Run PyPort sync periodically\n\n"
                 "[Timer]\nOnCalendar=daily\nPersistent=true\n\n"
                 "[Install]\nWantedBy=timers.target\n")
        return unit, timer

    # convenience: export/import ------------------------------------------------

    def export_json(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")

    def import_json(self, path: Path) -> None:
        txt = path.read_text(encoding="utf-8")
        d = json.loads(txt)
        self._merge(d)
        self._normalize()

# CLI ------------------------------------------------------------------------

def _cli():
    parser = argparse.ArgumentParser(prog="pyport-config", description="Gerenciador de configuração do PyPort")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_show = sub.add_parser("show", help="mostrar configuração combinada (system + user + defaults)")
    p_show.add_argument("--json", action="store_true", help="imprime JSON")

    p_get = sub.add_parser("get", help="obter valor de chave")
    p_get.add_argument("key", help="chave a obter")

    p_set = sub.add_parser("set", help="definir chave no arquivo do usuário (~/.config/pyport/config.yaml)")
    p_set.add_argument("key", help="chave")
    p_set.add_argument("value", help="valor (string/int/true/false)")

    p_repo = sub.add_parser("repo", help="gerenciar repositórios")
    repo_sub = p_repo.add_subparsers(dest="action", required=True)
    r_add = repo_sub.add_parser("add", help="adicionar repositório")
    r_add.add_argument("name"); r_add.add_argument("url"); r_add.add_argument("--type", default="git")
    r_add.add_argument("--branch", default=None)
    r_rm = repo_sub.add_parser("remove", help="remover repositório por nome ou url")
    r_rm.add_argument("name_or_url")
    r_list = repo_sub.add_parser("list", help="listar repositórios")

    p_validate = sub.add_parser("validate", help="validar configuração")

    p_save = sub.add_parser("save", help="salvar configuração atual no arquivo do usuário")
    p_save.add_argument("--path", default=str(_USER_CONFIG), help="caminho para salvar (default ~/.config/pyport/config.yaml)")

    p_unit = sub.add_parser("systemd-unit", help="gerar exemplo de unit systemd para sync")
    p_unit.add_argument("--user", action="store_true", help="gera unit para user (não root)")

    args = parser.parse_args()

    cfg = Config.load()
    if args.cmd == "show":
        d = cfg.to_dict()
        if args.json:
            print(json.dumps(d, indent=2))
        else:
            for k, v in d.items():
                print(f"{k}: {v}")
        return

    if args.cmd == "get":
        print(cfg.get(args.key, "" ))
        return

    if args.cmd == "set":
        # basic conversion
        val = args.value
        if val.lower() in ("true","false"):
            v = val.lower() == "true"
        else:
            try:
                v = int(val)
            except Exception:
                v = val
        cfg.set(args.key, v)
        cfg.save_user()
        print(f"salvo {args.key}={v} em {str(_USER_CONFIG)}")
        return

    if args.cmd == "repo":
        if args.action == "add":
            try:
                cfg.add_repository(args.name, args.url, typ=args.type, branch=args.branch)
                cfg.save_user()
                print(f"repo {args.name} adicionada")
            except Exception as e:
                print("erro:", e)
            return
        if args.action == "remove":
            ok = cfg.remove_repository(args.name_or_url)
            if ok:
                cfg.save_user()
                print("removido")
            else:
                print("não encontrado")
            return
        if args.action == "list":
            for r in cfg.list_repositories():
                print(f"- {r.get('name')} ({r.get('type')}): {r.get('url')}")
            return

    if args.cmd == "validate":
        ok, problems = cfg.validate()
        if ok:
            print("config válida")
        else:
            print("config inválida:")
            for p in problems:
                print(" -", p)
        return

    if args.cmd == "save":
        path = Path(args.path).expanduser()
        cfg.save_user(path)
        print("config salva em", path)
        return

    if args.cmd == "systemd-unit":
        user = getpass.getuser() if args.user else None
        unit, timer = cfg.systemd_sync_unit(user=user, exec_path="/usr/bin/pyport")
        print("----- service -----")
        print(unit)
        print("----- timer -----")
        print(timer)
        return

# Module convenience: singleton instance loaded from system+user
_DEFAULT_CFG_INSTANCE: Optional[Config] = None

def get_default_config() -> Config:
    global _DEFAULT_CFG_INSTANCE
    if _DEFAULT_CFG_INSTANCE is None:
        _DEFAULT_CFG_INSTANCE = Config.load()
    return _DEFAULT_CFG_INSTANCE

# If executed as script -> CLI
if __name__ == "__main__":
    _cli()
