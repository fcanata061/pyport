"""
Módulo `upgrade.py` para PyPort
Versão: evoluída, integrada com os módulos existentes do repositório.

Este arquivo implementa:
- UpgradeManager: orquestra upgrades (detecção, verificação, backup, aplicar, relatório, rollback)
- Verifier: checa dependências/compatibilidades usando dependency.py
- Migrator: aplica migrações de configuração usando configure.py
- Rollback: restaura backups em caso de falha
- Reporter: gera relatório JSON e resumo

O módulo é defensivo: tenta usar funções existentes dos módulos do repositório
(se disponibilizadas com nomes comuns). Quando uma função esperada não existe,
usa implementações fallback simples e loga avisos.

Coloque este arquivo em pyport/upgrade.py (ou pyport/upgrade/__init__.py) e
importe UpgradeManager no CLI para adicionar o comando `upgrade`.

"""

import os
import sys
import json
import shutil
import time
import datetime
import tempfile
import traceback
from pathlib import Path
from typing import Dict, List, Optional, Any

# Import dos módulos do projeto (assumindo estão no mesmo pacote ou PYTHONPATH)
try:
    import update as upd
except Exception:
    upd = None

try:
    import dependency as dep
except Exception:
    dep = None

try:
    import install as inst
except Exception:
    inst = None

try:
    import remove as rem
except Exception:
    rem = None

try:
    import packager as pack
except Exception:
    pack = None

try:
    import fetch as fetch_mod
except Exception:
    fetch_mod = None

try:
    import configure as configure_mod
except Exception:
    configure_mod = None

try:
    import hooks as hooks_mod
except Exception:
    hooks_mod = None

try:
    import sandbox as sandbox_mod
except Exception:
    sandbox_mod = None

try:
    import fakeroot as fakeroot_mod
except Exception:
    fakeroot_mod = None

try:
    import logger as logger_mod
except Exception:
    logger_mod = None

# configuração padrao para caminhos
LOG_DIR = Path('/pyport/logs')
STATE_DIR = Path('/pyport/state')
BACKUP_BASE = Path('/var/lib/pyport/upgrade_backups')
REPORT_DIR = LOG_DIR

for d in (LOG_DIR, STATE_DIR, BACKUP_BASE, REPORT_DIR):
    try:
        d.mkdir(parents=True, exist_ok=True)
    except Exception:
        # Em ambientes onde não há permissão para /var/lib ou /pyport, cairemos
        # para um tempdir local
        pass

# Logger helper (fallback para print)
def get_logger(name: str = "pyport.upgrade"):
    if logger_mod and hasattr(logger_mod, 'get_logger'):
        try:
            return logger_mod.get_logger(name)
        except Exception:
            pass

    # fallback simples
    class SimpleLogger:
        def __init__(self, name):
            self.name = name
        def _log(self, level, *args):
            ts = datetime.datetime.utcnow().isoformat()
            print(f"{ts} {self.name} {level}:", *args, file=sys.stderr)
        def debug(self, *a): self._log('DEBUG', *a)
        def info(self, *a): self._log('INFO', *a)
        def warning(self, *a): self._log('WARN', *a)
        def error(self, *a): self._log('ERROR', *a)
        def exception(self, *a): self._log('EXC', *a)
    return SimpleLogger(name)

logger = get_logger("pyport.upgrade")

# Utilitários

def timestamp():
    return datetime.datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')


def safe_call(module, fn_name, *args, default=None, **kwargs):
    """Tenta chamar module.fn_name(*args, **kwargs) e retorna default caso não exista."""
    if not module:
        return default
    fn = getattr(module, fn_name, None)
    if not fn:
        return default
    try:
        return fn(*args, **kwargs)
    except Exception as e:
        logger.warning(f"Erro ao chamar {module.__name__}.{fn_name}: {e}")
        logger.debug(traceback.format_exc())
        return default


class Verifier:
    """Verificador de compatibilidade / dependências antes de aplicar upgrade."""
    def __init__(self, logger=None):
        self.logger = logger or get_logger('pyport.upgrade.verifier')

    def check(self, updates: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        """Verifica se há conflitos de dependência para os updates.
        `updates` é um dict: {pkg_name: {'old_version':.., 'new_version':.., 'source':..}}
        Retorna dict de conflitos por pacote (vazio se não houver).
        """
        conflicts = {}
        for pkg, info in updates.items():
            self.logger.debug('Verificando dependencias para', pkg, info)
            # usa dependency.resolve ou dependency.check_conflicts se existir
            if dep and hasattr(dep, 'check_conflicts'):
                try:
                    result = dep.check_conflicts(pkg, info.get('new_version'))
                    if result:
                        conflicts[pkg] = result
                except Exception as e:
                    self.logger.warning(f"dependency.check_conflicts falhou para {pkg}: {e}")
                    # não aborta aqui; registra o pacote como "precisa checar manualmente"
                    conflicts[pkg] = {'error': str(e)}
            elif dep and hasattr(dep, 'resolve'):
                try:
                    ok = dep.resolve(pkg, info.get('new_version'))
                    if not ok:
                        conflicts[pkg] = {'reason': 'resolve reported unresolvable dependencies'}
                except Exception as e:
                    self.logger.warning(f"dependency.resolve falhou para {pkg}: {e}")
                    conflicts[pkg] = {'error': str(e)}
            else:
                # fallback: não temos um resolvedor; apenas logar uma advertencia
                self.logger.info(f"Nenhum resolvedor de dependencias detectado; proceda com cautela para {pkg}")
        return conflicts


class Migrator:
    """Responsável por migrar configurações se necessário.
    Usa `configure` quando disponível; caso contrário, faz ações conservadoras.
    """
    def __init__(self, logger=None):
        self.logger = logger or get_logger('pyport.upgrade.migrator')

    def migrate(self, pkg: str, old_version: str, new_version: str) -> None:
        self.logger.debug(f"Verificando migração para {pkg}: {old_version} -> {new_version}")
        if configure_mod and hasattr(configure_mod, 'migrate_package_config'):
            try:
                configure_mod.migrate_package_config(pkg, old_version, new_version)
                self.logger.info(f"Migração de configuração executada para {pkg}")
                return
            except Exception as e:
                self.logger.warning(f"Falha ao migrar configuração via configure.migrate_package_config: {e}")
        # fallback simples: procurar arquivos de config em /etc/ ou em state e criar backup
        # (Implementação conservadora: nada além de backup é aplicada automaticamente)
        cfg_paths = []
        etc_path = Path('/etc') / pkg
        if etc_path.exists():
            cfg_paths.append(str(etc_path))
        state_cfg = STATE_DIR / f"{pkg}.json"
        if state_cfg.exists():
            cfg_paths.append(str(state_cfg))
        if cfg_paths:
            self.logger.warning(f"Configs detectadas para {pkg} em {cfg_paths}. Verifique migracoes manualmente.")


class Rollback:
    """Lógica de rollback/restauração de backups.
    Backup_info é a estrutura gerada por UpgradeManager.backup_state().
    """
    def __init__(self, logger=None):
        self.logger = logger or get_logger('pyport.upgrade.rollback')

    def restore(self, backup_info: Dict[str, Any]) -> None:
        if not backup_info:
            self.logger.warning('Nenhuma informação de backup disponível, não é possível restaurar automaticamente')
            return
        for pkg, info in backup_info.get('packages', {}).items():
            bdir = Path(info.get('backup_dir', ''))
            if not bdir.exists():
                self.logger.warning(f'Backup para {pkg} não encontrado em {bdir}')
                continue
            try:
                target = info.get('restore_target')
                if target:
                    # restaura arquivos
                    self.logger.info(f'Restaurando arquivos de {pkg} -> {target}')
                    if Path(target).exists():
                        # remove o que foi instalado durante falha
                        try:
                            if Path(target).is_dir():
                                shutil.rmtree(target)
                            else:
                                Path(target).unlink()
                        except Exception:
                            self.logger.debug('Falha ao limpar target antes de restaurar', traceback.format_exc())
                    # copiar do backup
                    if bdir.is_dir():
                        shutil.copytree(bdir, target)
                    else:
                        shutil.copy2(bdir, target)
                # tentar reinstalar a versão antiga usando install se possível
                old_ver = info.get('old_version')
                if inst and hasattr(inst, 'install_package') and old_ver:
                    self.logger.info(f'Reinstalando {pkg} na versão {old_ver} via install.install_package')
                    try:
                        inst.install_package(pkg, old_ver)
                    except Exception as e:
                        self.logger.warning(f'Erro ao reinstalar {pkg}: {e}')
            except Exception as e:
                self.logger.warning(f'Erro ao restaurar backup de {pkg}: {e}')
                self.logger.debug(traceback.format_exc())


class Reporter:
    def __init__(self, logger=None, report_dir: Path = REPORT_DIR):
        self.logger = logger or get_logger('pyport.upgrade.reporter')
        self.report_dir = Path(report_dir)
        try:
            self.report_dir.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass

    def generate(self, summary: Dict[str, Any], filename: Optional[str] = None) -> Path:
        if filename is None:
            filename = f"upgrade_report_{timestamp()}.json"
        path = self.report_dir / filename
        try:
            with open(path, 'w', encoding='utf-8') as f:
                json.dump(summary, f, indent=2, ensure_ascii=False)
            self.logger.info(f'Relatório de upgrade salvo em {path}')
        except Exception as e:
            self.logger.warning(f'Falha ao salvar relatório em {path}: {e}')
        return path


class UpgradeManager:
    """Gerencia o processo de upgrade: detecção, verificação, backup, aplicar, verificar e reportar."""

    def __init__(self, packages: Optional[List[str]] = None, dry_run: bool = False, force: bool = False):
        self.packages = packages
        self.dry_run = dry_run
        self.force = force
        self.logger = get_logger('pyport.upgrade.manager')
        self.verifier = Verifier(self.logger)
        self.migrator = Migrator(self.logger)
        self.rollbacker = Rollback(self.logger)
        self.reporter = Reporter(self.logger)
        self.backup_info = {'timestamp': timestamp(), 'packages': {}}

    # --- Utilities to interact with existing modules ---
    def _detect_updates(self) -> Dict[str, Dict[str, Any]]:
        """Detecta updates a partir do módulo update.py ou implementa fallback.
        Retorna um dict {pkg: {'old_version':..., 'new_version':..., 'source':...}}
        """
        self.logger.debug('Detectando atualizacoes usando update.py')
        updates = {}
        # tenta funções comumente usadas
        if upd:
            # cheque por function names usados em análise anterior
            for fn in ('check_for_updates', 'find_updates', 'scan_for_updates', 'check_updates'):
                fn_impl = getattr(upd, fn, None)
                if fn_impl:
                    try:
                        res = fn_impl(self.packages) if self.packages else fn_impl()
                        if isinstance(res, dict):
                            updates = res
                            self.logger.debug('Updates detectados via', fn)
                            break
                        # possível que retorne lista/tuplas
                    except Exception as e:
                        self.logger.warning(f'Falha ao chamar update.{fn}: {e}')
            # fallback: update.py pode expor um `scan_ports` que retorna lista de metas
            if not updates and hasattr(upd, 'scan_ports'):
                try:
                    ports = upd.scan_ports()
                    # supondo que ports seja lista de dicts com name/version/source
                    for p in ports:
                        name = p.get('name') or p.get('pkg') or p.get('package')
                        old_v = p.get('version')
                        new_v = p.get('available_version') or p.get('latest')
                        if name and new_v and old_v and new_v != old_v:
                            if (self.packages is None) or (name in self.packages):
                                updates[name] = {'old_version': old_v, 'new_version': new_v, 'source': p.get('source')}
                except Exception as e:
                    self.logger.debug('scan_ports falhou', e)
        # se nada encontrado, tentar ler um estado local (fallback)
        if not updates:
            state_file = STATE_DIR / 'installed_state.json'
            if state_file.exists():
                try:
                    s = json.load(open(state_file, 'r', encoding='utf-8'))
                    for name, info in s.items():
                        if self.packages and name not in self.packages:
                            continue
                        current = info.get('version')
                        # sem repositório remoto não há como saber versão nova;
                        # marcamos None para que CLI mostre que não há dados
                        updates[name] = {'old_version': current, 'new_version': None, 'source': None}
                except Exception as e:
                    self.logger.debug('Falha ao ler installed_state.json', e)
        return updates

    def _fetch(self, pkg: str, new_version: str) -> Dict[str, Any]:
        self.logger.debug(f'Buscando fontes para {pkg} {new_version}')
        if fetch_mod and hasattr(fetch_mod, 'fetch'):
            return safe_call(fetch_mod, 'fetch', pkg, new_version, default={}) or {}
        if fetch_mod and hasattr(fetch_mod, 'fetch_source'):
            return safe_call(fetch_mod, 'fetch_source', pkg, new_version, default={}) or {}
        # fallback: nada para buscar
        self.logger.info('Nenhum fetcher disponível; assumindo que a fonte já está local ou será tratada pelo install')
        return {}

    def _install(self, pkg: str, new_version: str, source_info: Dict[str, Any]) -> None:
        self.logger.debug(f'Instalando {pkg} {new_version}, source={source_info}')
        # preferir install.install_package(pkg, version, source=...)
        if inst and hasattr(inst, 'install_package'):
            return safe_call(inst, 'install_package', pkg, new_version, source=source_info)
        if inst and hasattr(inst, 'install'):
            return safe_call(inst, 'install', pkg, version=new_version, source=source_info)
        # fallback: se packager tem build, usar e depois instalar
        if pack and hasattr(pack, 'build_package'):
            build = safe_call(pack, 'build_package', pkg, new_version, default=None)
            if build:
                if inst and hasattr(inst, 'install_pkgfile'):
                    return safe_call(inst, 'install_pkgfile', build)
        # se nada disso existir, tentamos chamar um shell install command generico
        self.logger.warning('Nenhum método de instalação detectado - instalacao manual requerida')
        raise RuntimeError('Install method not available')

    def _remove(self, pkg: str, old_version: str) -> None:
        self.logger.debug(f'Removendo {pkg} {old_version} se necessário')
        if rem and hasattr(rem, 'remove_package'):
            return safe_call(rem, 'remove_package', pkg, old_version)
        if rem and hasattr(rem, 'remove'):
            return safe_call(rem, 'remove', pkg)
        # fallback: tentar remover diretório de instalação conhecido
        inst_dir = Path('/usr/local') / 'pyport' / 'packages' / pkg
        if inst_dir.exists():
            try:
                if inst_dir.is_dir():
                    shutil.rmtree(inst_dir)
            except Exception as e:
                self.logger.warning(f'Falha ao remover {inst_dir}: {e}')

    def backup_state_for_pkg(self, pkg: str, old_version: Optional[str]) -> Dict[str, Any]:
        """Cria backup dos arquivos/estado relevantes para o pacote. Retorna metadados do backup."""
        binfo = {'old_version': old_version, 'backup_dir': None, 'restore_target': None}
        ts = timestamp()
        target_paths = []
        # obter caminhos conhecidos via install module
        if inst and hasattr(inst, 'get_package_paths'):
            try:
                pths = inst.get_package_paths(pkg, old_version)
                if pths:
                    target_paths.extend(pths)
            except Exception as e:
                self.logger.debug('inst.get_package_paths falhou', e)
        # fallback: possíveis locais padrões
        default_candidates = [Path('/etc') / pkg, Path('/usr/local') / pkg, Path('/opt') / pkg]
        for c in default_candidates:
            if c.exists():
                target_paths.append(str(c))
        # criar backup
        if not target_paths:
            self.logger.info(f'Nenhum arquivo alvo encontrado para backup de {pkg}; pulando backup de arquivos')
            return binfo
        bdir = BACKUP_BASE / f"{pkg}_{ts}"
        try:
            bdir.mkdir(parents=True, exist_ok=True)
            for p in target_paths:
                pth = Path(p)
                dest = bdir / pth.name
                if pth.is_dir():
                    shutil.copytree(pth, dest)
                elif pth.is_file():
                    shutil.copy2(pth, dest)
            binfo['backup_dir'] = str(bdir)
            # definimos restore_target como o primeiro target por simplicidade
            binfo['restore_target'] = target_paths[0]
            self.logger.info(f'Backup de {pkg} realizado em {bdir}')
        except Exception as e:
            self.logger.warning(f'Falha ao criar backup para {pkg}: {e}')
        return binfo

    def perform_upgrade(self):
        self.logger.info('Iniciando fluxo de upgrade')

        updates = self._detect_updates()
        if not updates:
            self.logger.info('Nenhuma atualizacao detectada')
            return {'status': 'no-updates', 'details': {}}

        # filtrar pacotes sem new_version se o usuario pediu pacotes especificos
        filtered = {}
        for pkg, info in updates.items():
            if self.packages and pkg not in self.packages:
                continue
            filtered[pkg] = info
        updates = filtered

        self.logger.info(f'Pacotes a considerar para upgrade: {list(updates.keys())}')

        # 1) Verificador
        conflicts = self.verifier.check(updates)
        if conflicts and not self.force:
            self.logger.error('Conflitos detectados; abortando upgrade. Use --force para tentar prosseguir.')
            return {'status': 'conflicts', 'details': conflicts}

        # 2) modo dry_run: simula apenas
        if self.dry_run:
            self.logger.info('Dry-run ativo; mostrando o que seria feito:')
            summary = {'timestamp': timestamp(), 'dry_run': True, 'planned': {}}
            for pkg, info in updates.items():
                summary['planned'][pkg] = {'old_version': info.get('old_version'), 'new_version': info.get('new_version')}
            self.reporter.generate(summary)
            return {'status': 'dry-run', 'details': summary}

        # 3) Backup: para cada pacote criamos backup antes de mudar
        for pkg, info in updates.items():
            old_v = info.get('old_version')
            binfo = self.backup_state_for_pkg(pkg, old_v)
            self.backup_info['packages'][pkg] = binfo

        # 4) Pré-hooks
        if hooks_mod and hasattr(hooks_mod, 'run_hook'):
            try:
                hooks_mod.run_hook('pre-upgrade', {'packages': list(updates.keys())})
            except Exception as e:
                self.logger.warning(f'pre-upgrade hook falhou: {e}')

        # 5) Aplicar upgrades
        success = {}
        failures = {}
        for pkg, info in updates.items():
            old_v = info.get('old_version')
            new_v = info.get('new_version')
            source = info.get('source')
            try:
                self.logger.info(f'Iniciando atualizacao de {pkg}: {old_v} -> {new_v}')
                # buscar fontes
                fetch_info = self._fetch(pkg, new_v)
                # remover versão antiga (opcional)
                try:
                    self._remove(pkg, old_v)
                except Exception:
                    self.logger.debug('Remocao previa pode ter falhado ou nao ser necessária')
                # instalar
                self._install(pkg, new_v, fetch_info)
                # migrar configuracoes se necessário
                try:
                    self.migrator.migrate(pkg, old_v, new_v)
                except Exception:
                    self.logger.warning(f'Migracao de {pkg} falhou; verificar manualmente')
                self.logger.info(f'{pkg} atualizado com sucesso para {new_v}')
                success[pkg] = {'old_version': old_v, 'new_version': new_v}
            except Exception as e:
                self.logger.error(f'Falha ao atualizar {pkg}: {e}')
                self.logger.debug(traceback.format_exc())
                failures[pkg] = {'old_version': old_v, 'error': str(e)}
                # iniciar rollback parcial para esse pacote
                try:
                    self.rollbacker.restore({'packages': {pkg: self.backup_info['packages'].get(pkg)}})
                except Exception as e2:
                    self.logger.warning(f'Rollback falhou para {pkg}: {e2}')
                # se não é forçado, abortar todo o processo
                if not self.force:
                    self.logger.error('Abortando upgrades restantes devido a falha e flag --force nao especificada')
                    break

        # 6) pós-hooks
        if hooks_mod and hasattr(hooks_mod, 'run_hook'):
            try:
                hooks_mod.run_hook('post-upgrade', {'success': list(success.keys()), 'failures': list(failures.keys())})
            except Exception as e:
                self.logger.warning(f'post-upgrade hook falhou: {e}')

        # 7) gerar relatório
        summary = {'timestamp': timestamp(), 'success': success, 'failures': failures, 'backup_info': self.backup_info}
        rpath = self.reporter.generate(summary)

        status = 'partial-success' if failures else 'success'
        return {'status': status, 'report': str(rpath), 'summary': summary}


# Pequeno CLI helper caso o usuário queira executar diretamente este arquivo
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(prog='pyport-upgrade', description='Gerenciador de upgrade para PyPort')
    parser.add_argument('packages', nargs='*', help='Lista de pacotes para atualizar; se vazia, todos com updates')
    parser.add_argument('--dry-run', action='store_true', help='Mostrar o que seria feito sem aplicar')
    parser.add_argument('--force', action='store_true', help='Tentar continuar mesmo com conflitos/erros')
    args = parser.parse_args()
    mgr = UpgradeManager(packages=args.packages or None, dry_run=args.dry_run, force=args.force)
    res = mgr.perform_upgrade()
    print(json.dumps(res, indent=2, ensure_ascii=False))
