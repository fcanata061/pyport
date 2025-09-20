"""
pyport search module - evolved version

Permite pesquisar pacotes nos Portfiles:
- Busca por nome (parcial ou exato)
- Busca por descrição
- Filtros por categoria e versão
- Saída formatada em tabela ou JSON
- Logs em /pyport/logs/search.log
"""

import sys, json, re
from pathlib import Path
from typing import Dict, Any, List

import yaml

PORTFILES_ROOT = Path("/usr/ports")
LOG_DIR = Path("/pyport/logs")
LOG_DIR.mkdir(parents=True, exist_ok=True)
SEARCH_LOG = LOG_DIR / "search.log"


def log(msg: str):
    print(f"[search] {msg}")
    with open(SEARCH_LOG, "a") as f:
        f.write(msg + "\n")


def load_portfile(portfile: Path) -> Dict[str, Any]:
    try:
        with open(portfile) as f:
            return yaml.safe_load(f)
    except Exception as e:
        log(f"erro ao ler {portfile}: {e}")
        return {}


def highlight(text: str, query: str) -> str:
    """
    Destaca ocorrências da query no texto.
    """
    return re.sub(f"({re.escape(query)})", r"\033[1;32m\1\033[0m", text, flags=re.I)


def search_ports(query: str, by_description: bool = False,
                 category: str = None, min_version: str = None,
                 max_version: str = None, output_json: bool = False) -> List[Dict[str, Any]]:
    results = []
    for portfile in PORTFILES_ROOT.glob("**/Portfile.yaml"):
        meta = load_portfile(portfile)
        if not meta:
            continue

        name = meta.get("name", "")
        desc = meta.get("description", "")
        ver = meta.get("version", "0.0.0")
        cat = meta.get("category", "misc")

        if category and meta.get("category") != category:
            continue

        if min_version and ver < min_version:
            continue
        if max_version and ver > max_version:
            continue

        haystack = desc if by_description else name
        if re.search(query, haystack, re.I):
            results.append({
                "name": name,
                "version": ver,
                "description": desc,
                "category": cat,
                "path": str(portfile.parent)
            })

    if output_json:
        print(json.dumps(results, indent=2))
    else:
        if not results:
            print("Nenhum pacote encontrado.")
        else:
            print(f"{'Pacote':20} {'Versão':10} {'Categoria':15} Descrição")
            print("-" * 80)
            for r in results:
                n = highlight(r["name"], query)
                d = highlight(r["description"], query)
                print(f"{n:20} {r['version']:10} {r['category']:15} {d}")

    log(f"busca: '{query}' resultados: {len(results)}")
    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 search.py <termo> [--desc] [--cat=...] [--json]")
        sys.exit(1)

    query = sys.argv[1]
    by_desc = "--desc" in sys.argv
    output_json = "--json" in sys.argv

    cat = None
    for arg in sys.argv[2:]:
        if arg.startswith("--cat="):
            cat = arg.split("=", 1)[1]

    search_ports(query, by_description=by_desc, category=cat, output_json=output_json)
