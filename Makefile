PREFIX ?= /usr
BINDIR = $(PREFIX)/bin
LIBDIR = $(PREFIX)/lib/pyport

PYFILES = $(wildcard pyport/*.py)

all:
	@echo "Use 'make install' para instalar o pyport ou 'make uninstall' para remover."

install:
	@echo "Instalando pyport em $(BINDIR) e $(LIBDIR)..."
	mkdir -p $(DESTDIR)$(LIBDIR)
	cp -r pyport/*.py $(DESTDIR)$(LIBDIR)/
	cp bin/pyport $(DESTDIR)$(BINDIR)/pyport
	chmod +x $(DESTDIR)$(BINDIR)/pyport
	@echo "Instalação concluída."

uninstall:
	@echo "Removendo pyport..."
	rm -f $(DESTDIR)$(BINDIR)/pyport
	rm -rf $(DESTDIR)$(LIBDIR)
	@echo "Remoção concluída."

clean:
	@echo "Nada para limpar."
