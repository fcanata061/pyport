# Caminhos configuráveis
PREFIX ?= /usr
BINDIR = $(PREFIX)/bin
LIBDIR = $(PREFIX)/lib/pyport

PYFILES = $(wildcard pyport/*.py)

all:
	@echo "PyPort pronto para instalar."
	@echo "Use:"
	@echo "  make install            -> instala em $(PREFIX)"
	@echo "  make install PREFIX=/opt/pyport"
	@echo "  make install PREFIX=$(HOME)/.local"
	@echo "  make uninstall          -> remove a instalação"
	@echo "  make reinstall          -> reinstala (uninstall + install)"
	@echo ""

install:
	@echo "📦 Instalando PyPort em:"
	@echo "  Binário: $(DESTDIR)$(BINDIR)/pyport"
	@echo "  Módulos: $(DESTDIR)$(LIBDIR)"
	mkdir -p $(DESTDIR)$(LIBDIR)
	cp -r pyport/*.py $(DESTDIR)$(LIBDIR)/
	mkdir -p $(DESTDIR)$(BINDIR)
	cp bin/pyport $(DESTDIR)$(BINDIR)/pyport
	chmod +x $(DESTDIR)$(BINDIR)/pyport
	@echo "✅ Instalação concluída."

uninstall:
	@echo "🗑️  Removendo PyPort de $(PREFIX)..."
	rm -f $(DESTDIR)$(BINDIR)/pyport
	rm -rf $(DESTDIR)$(LIBDIR)
	@echo "❌ Remoção concluída."

reinstall: uninstall install
	@echo "🔄 Reinstalação concluída."

clean:
	@echo "🧹 Nada para limpar."
