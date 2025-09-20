"""
pyport info module - evolved version

Exibe informações detalhadas de pacotes:
- Metadados do Portfile.yaml
- Dependências via dependency.py
- Arquivos instalados (metadata.json)
- Estado do pacote (instalado ou não)
- Saída formatada em tabela ou JSON
"""

import sys, json
from pathlib import Path
from typing import Dict, Any

import yaml
from dependency import DependencyGraph

PORTFILES_ROOT = Path("/usr/ports")
METADATA_ROOT = Path("/pyport/metadata")
LOG_DIR = Path("/pyport/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
INFO_LOG = LOG_DIR / "info.log"


def log(msg: str):
    print(f"[info] {msg}")
    with open(INFO_LOG, "a") as f:
        f.write(msg + "\n")


def load_portfile(pkg: str) -> Dict[str, Any]:
    for portfile in PORTFILES_ROOT.glob(f"**/Portfile.yaml"):
        try:
            with open(portfile) as f:
                data = yaml.safe_load(f)
                if data.get("name") == pkg:
                    return data
        except Exception as e:
            log(f"erro lendo {portfile}: {e}")
    return {}


def load_metadata(pkg: str) -> Dict[str, Any]:
    meta_file = METADATA_ROOT / f"{pkg}.json"
    if meta_file.exists():
        try:
            with open(meta_file) as f:
                return json.load(f)
        except Exception as e:
            log(f"erro lendo {meta_file}: {e}")
    return {}


def show_info(pkg: str, output_json: bool = False):
    port = load_portfile(pkg)
    if not port:
        print(f"Pacote '{pkg}' não encontrado no repositório de ports.")
        return

    meta = load_metadata(pkg)
    installed = bool(meta)

    graph = DependencyGraph()
    deps = port.get("depends", {}).get("runtime", [])
    dep_tree = graph.resolve_dependencies(pkg, deps)

    info = {
        "name": port.get("name"),
        "version": port.get("version"),
        "description": port.get("description", ""),
        "category": port.get("category", "misc"),
        "homepage": port.get("homepage", ""),
        "license": port.get("license", ""),
        "installed": installed,
        "files": meta.get("files", []) if installed else [],
        "dependencies": dep_tree
    }

    if output_json:
        print(json.dumps(info, indent=2))
    else:
        print(f"📦 {info['name']} {info['version']}")
        print(f"📂 Categoria: {info['category']}")
        print(f"🔗 Homepage: {info['homepage']}")
        print(f"📜 Licença: {info['license']}")
        print(f"📝 Descrição: {info['description']}")
        print(f"✅ Instalado: {'sim' if installed else 'não'}")

        print("\n🔗 Dependências:")
        if dep_tree:
            for d in dep_tree:
                print(f"  - {d}")
        else:
            print("  (nenhuma)")

        if installed:
            print("\n📂 Arquivos instalados:")
            for f in info["files"][:20]:  # mostra só os primeiros 20
                print(f"  {f}")
            if len(info["files"]) > 20:
                print(f"  ... ({len(info['files'])} arquivos no total)")

    log(f"info consultado para '{pkg}' (instalado={installed})")
    return info


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 info.py <pacote> [--json]")
        sys.exit(1)

    pkg = sys.argv[1]
    out_json = "--json" in sys.argv
    show_info(pkg, output_json=out_json)
