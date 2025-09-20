"""
pyport sync module - evolved version

Sincroniza Portfiles de múltiplos repositórios:
- Git, HTTP, HTTPS, FTP
- Verificação de integridade com checksum
- Histórico salvo em /pyport/state/sync.json
- Logs em /pyport/logs/sync.log
- Notificação desktop opcional
"""

import os, sys, subprocess, hashlib, tarfile, json, shutil
from pathlib import Path
from urllib.request import urlopen, urlretrieve
from datetime import datetime

import yaml

# Diretórios principais
PORTFILES_ROOT = Path("/usr/ports")
STATE_DIR = Path("/pyport/state")
LOG_DIR = Path("/pyport/logs")

STATE_DIR.mkdir(parents=True, exist_ok=True)
LOG_DIR.mkdir(parents=True, exist_ok=True)

SYNC_LOG = LOG_DIR / "sync.log"
SYNC_STATE = STATE_DIR / "sync.json"


def log(msg: str):
    print(f"[sync] {msg}")
    with open(SYNC_LOG, "a") as f:
        f.write(msg + "\n")


def notify(msg: str):
    try:
        subprocess.run(["notify-send", "PyPort Sync", msg], check=False)
    except FileNotFoundError:
        log("notify-send não disponível")


def load_config() -> dict:
    """
    Carrega configuração global do PyPort (lista de repositórios).
    """
    cfg_file = Path("/etc/pyport/config.yaml")
    if not cfg_file.exists():
        log("Nenhum arquivo de configuração encontrado em /etc/pyport/config.yaml")
        return {}
    try:
        with open(cfg_file) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        log(f"Erro carregando config: {e}")
        return {}


def save_state(state: dict):
    with open(SYNC_STATE, "w") as f:
        json.dump(state, f, indent=2)


def get_state() -> dict:
    if SYNC_STATE.exists():
        try:
            with open(SYNC_STATE) as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def checksum_file(path: Path, algo="sha256") -> str:
    h = hashlib.new(algo)
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def sync_git(repo_url: str, dest: Path):
    if dest.exists():
        log(f"Atualizando repositório Git em {dest}")
        subprocess.run(["git", "-C", str(dest), "pull"], check=False)
    else:
        log(f"Clonando repositório Git de {repo_url}")
        subprocess.run(["git", "clone", repo_url, str(dest)], check=False)


def sync_tarball(url: str, dest: Path, checksum: str = None):
    tmpfile, _ = urlretrieve(url)
    if checksum:
        got = checksum_file(Path(tmpfile))
        if got != checksum:
            log(f"Checksum inválido para {url} (esperado {checksum}, obtido {got})")
            return

    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tmpfile, "r:*") as tar:
        tar.extractall(dest)
    log(f"Baixado e extraído {url} em {dest}")


def sync_all(repo_name: str = None):
    cfg = load_config()
    repos = cfg.get("repositories", [])

    if not repos:
        log("Nenhum repositório configurado.")
        return

    state = get_state()

    for repo in repos:
        name = repo.get("name")
        url = repo.get("url")
        typ = repo.get("type", "git")
        checksum = repo.get("checksum")

        if repo_name and repo_name != name:
            continue

        dest = PORTFILES_ROOT / name
        try:
            if typ == "git":
                sync_git(url, dest)
            elif typ in ("http", "https", "ftp"):
                sync_tarball(url, dest, checksum)
            else:
                log(f"Tipo de repositório não suportado: {typ}")

            state[name] = {
                "url": url,
                "last_sync": datetime.utcnow().isoformat()
            }
            save_state(state)
            log(f"Repositório {name} sincronizado com sucesso.")
        except Exception as e:
            log(f"Erro sincronizando {name}: {e}")

    notify("Sincronização concluída.")


if __name__ == "__main__":
    if len(sys.argv) > 1:
        repo = sys.argv[1]
        sync_all(repo_name=repo)
    else:
        sync_all()
