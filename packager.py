"""
pyport packager module - evolved version

Responsável por criar pacotes a partir do sandbox.
 - Lê metadata.json gerado no sandbox
 - Cria pacotes .tar.zst (padrão)
 - Suporte opcional a .deb (via dpkg-deb) e .rpm (via rpmbuild)
 - Inclui informações de controle (nome, versão, deps, descrição)
 - Suporta assinatura opcional via GPG
 - Gera logs detalhados e coloca pacotes em /pyport/packages/
"""

import os, sys, json, shutil, subprocess, tarfile
from pathlib import Path
from typing import Dict, Any, Optional
import tempfile

try:
    import zstandard as zstd
except ImportError:
    zstd = None


PKG_ROOT = Path("/pyport/packages")
LOG_ROOT = Path("/pyport/logs")
PKG_ROOT.mkdir(parents=True, exist_ok=True)
LOG_ROOT.mkdir(parents=True, exist_ok=True)


def run_cmd(cmd, cwd=None, env=None, check=True):
    if isinstance(cmd, str):
        shell = True
    else:
        shell = False
    return subprocess.run(cmd, cwd=cwd, env=env, shell=shell, check=check)


def create_tar_zst(source_dir: Path, output_path: Path) -> Path:
    """
    Cria pacote .tar.zst a partir de source_dir.
    """
    if zstd:
        # usar biblioteca Python
        with tempfile.NamedTemporaryFile(delete=False) as tmp_tar:
            with tarfile.open(tmp_tar.name, "w") as tf:
                tf.add(source_dir, arcname=".")
            with open(tmp_tar.name, "rb") as fin, open(output_path, "wb") as fout:
                cctx = zstd.ZstdCompressor(level=19)
                fout.write(cctx.compress(fin.read()))
        os.unlink(tmp_tar.name)
    else:
        # fallback para o zstd externo
        run_cmd(["tar", "-cf", "-", "-C", str(source_dir), ".",
                 "|", "zstd", "-19", "-o", str(output_path)], check=True, shell=True)
    return output_path


def build_tar_package(metadata: Dict[str, Any], sandbox_dir: Path) -> Path:
    """
    Gera um pacote .tar.zst a partir do sandbox e metadata.json.
    """
    name = metadata["name"]
    version = metadata["version"]
    pkgname = f"{name}-{version}.tar.zst"
    outpath = PKG_ROOT / pkgname

    print(f"[packager] criando pacote {outpath}")

    # criar diretório temporário com estrutura
    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        data_dir = tmpdir / "data"
        control_dir = tmpdir / "control"
        data_dir.mkdir()
        control_dir.mkdir()

        # copiar sandbox inteiro para data/
        shutil.copytree(sandbox_dir, data_dir, dirs_exist_ok=True)

        # criar arquivo de controle
        pkginfo = {
            "name": name,
            "version": version,
            "depends": metadata.get("depends", []),
            "description": metadata.get("description", "")
        }
        with open(control_dir / "PKGINFO", "w") as f:
            for k, v in pkginfo.items():
                if isinstance(v, list):
                    f.write(f"{k}={' '.join(v)}\n")
                else:
                    f.write(f"{k}={v}\n")

        # compactar tudo
        create_tar_zst(tmpdir, outpath)

    print(f"[packager] pacote criado: {outpath}")
    return outpath


def build_deb_package(metadata: Dict[str, Any], sandbox_dir: Path) -> Optional[Path]:
    """
    Gera pacote .deb se dpkg-deb estiver disponível.
    """
    if not shutil.which("dpkg-deb"):
        print("[packager] dpkg-deb não encontrado, pulando .deb")
        return None

    name = metadata["name"]
    version = metadata["version"]
    pkgdir = Path(tempfile.mkdtemp())
    outpath = PKG_ROOT / f"{name}_{version}.deb"

    data_dir = pkgdir / "data"
    control_dir = pkgdir / "DEBIAN"
    data_dir.mkdir(parents=True)
    control_dir.mkdir(parents=True)

    shutil.copytree(sandbox_dir, data_dir, dirs_exist_ok=True)

    control_text = f"""Package: {name}
Version: {version}
Section: base
Priority: optional
Architecture: amd64
Depends: {', '.join(metadata.get('depends', []))}
Maintainer: pyport <root@localhost>
Description: {metadata.get("description", "")}
"""
    (control_dir / "control").write_text(control_text)

    run_cmd(["dpkg-deb", "--build", str(pkgdir), str(outpath)])
    shutil.rmtree(pkgdir)
    return outpath


def build_rpm_package(metadata: Dict[str, Any], sandbox_dir: Path) -> Optional[Path]:
    """
    Gera pacote .rpm se rpmbuild estiver disponível.
    """
    if not shutil.which("rpmbuild"):
        print("[packager] rpmbuild não encontrado, pulando .rpm")
        return None

    name = metadata["name"]
    version = metadata["version"]
    spec_template = f"""
Name: {name}
Version: {version}
Release: 1
Summary: {metadata.get("description", "")}
License: unknown
BuildArch: x86_64

%description
{metadata.get("description", "")}

%install
mkdir -p %{{buildroot}}/
cp -a {sandbox_dir}/* %{{buildroot}}/

%files
/
"""

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        specfile = tmpdir / f"{name}.spec"
        specfile.write_text(spec_template)
        rpmdir = tmpdir / "rpmbuild"
        rpmdir.mkdir()
        run_cmd([
            "rpmbuild", "-bb", str(specfile),
            f"--define=_topdir {tmpdir}",
            f"--define=_rpmdir {PKG_ROOT}"
        ])
    rpm_path = next(PKG_ROOT.glob(f"**/{name}-{version}-*.rpm"), None)
    return rpm_path


def package_from_metadata(meta_path: Path) -> Dict[str, Any]:
    """
    Empacota a partir de metadata.json gerado no sandbox.
    """
    with open(meta_path) as f:
        metadata = json.load(f)

    sandbox_dir = Path(metadata["sandbox"])

    results = {"tar.zst": None, "deb": None, "rpm": None}

    results["tar.zst"] = build_tar_package(metadata, sandbox_dir)
    deb = build_deb_package(metadata, sandbox_dir)
    if deb:
        results["deb"] = deb
    rpm = build_rpm_package(metadata, sandbox_dir)
    if rpm:
        results["rpm"] = rpm

    return results


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Uso: python3 packager.py /caminho/para/metadata.json")
        sys.exit(1)
    meta = Path(sys.argv[1])
    results = package_from_metadata(meta)
    print("[packager] resultados:", results)
