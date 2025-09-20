"""
pyport update module - evolved version

Verifica novas versões dos pacotes do repositório.
- Lê todos os Portfile.yaml em /usr/ports
- Detecta versão atual e compara com upstream
- Suporte a fontes git, http e ftp
- Gera relatório em /pyport/logs/update_report.json
- Envia notificação via notify-send
"""

import os, sys, subprocess, json, re
from pathlib import Path
from typing import Dict, Any, List
import yaml
import requests

PORTFILES_ROOT = Path("/usr/ports")
LOG_DIR = Path("/pyport/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
REPORT_FILE = LOG_DIR / "update_report.json"


def log(msg: str):
    print(f"[update] {msg}")


def load_portfile(portfile: Path) -> Dict[str, Any]:
    with open(portfile) as f:
        return yaml.safe_load(f)


def get_latest_from_git(url: str) -> str:
    """
    Obtém a versão mais recente de um repo git (última tag).
    """
    try:
        output = subprocess.check_output(["git", "ls-remote", "--tags", url], text=True)
        tags = re.findall(r"refs/tags/([^\n^{}]+)", output)
        if tags:
            # assume que a última tag é a mais recente
            return sorted(tags, key=lambda x: [int(s) if s.isdigit() else s for s in re.split(r"[\.-]", x)])[-1]
    except Exception as e:
        log(f"erro git {url}: {e}")
    return None


def get_latest_from_http(url: str) -> str:
    """
    Obtém versão de uma página HTTP (heurística: procura padrões de versão).
    """
    try:
        r = requests.get(url, timeout=10)
        if r.status_code == 200:
            versions = re.findall(r"\d+\.\d+(\.\d+)?", r.text)
            if versions:
                return sorted(versions, key=lambda v: [int(x) for x in v.split(".")])[-1]
    except Exception as e:
        log(f"erro http {url}: {e}")
    return None


def check_updates():
    updates = {}
    for portfile in PORTFILES_ROOT.glob("**/Portfile.yaml"):
        meta = load_portfile(portfile)
        name, version = meta["name"], meta["version"]
        log(f"checando {name} (atual {version})")

        latest = None
        for src in meta.get("source", []):
            if src.startswith("git+"):
                latest = get_latest_from_git(src[4:])
            elif src.startswith("http") or src.startswith("ftp"):
                latest = get_latest_from_http(src.rsplit("/", 1)[0])
            if latest:
                break

        if latest and latest != version:
            log(f"{name}: nova versão {latest} disponível (atual {version})")
            updates[name] = {"current": version, "latest": latest, "source": src}

    REPORT_FILE.write_text(json.dumps(updates, indent=2))

    if updates:
        subprocess.run([
            "notify-send",
            "PyPort Updates",
            f"{len(updates)} pacotes com novas versões disponíveis"
        ])
    else:
        log("todos os pacotes estão atualizados")

    return updates


if __name__ == "__main__":
    updates = check_updates()
    print(json.dumps(updates, indent=2))
