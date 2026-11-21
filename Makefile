# SPDX-License-Identifier: GPL-3.0-or-later
# diaspora is plain Python 3 (standard library only); nothing to build.

PREFIX ?= /usr/local
PYTHON ?= python3
MANDIR ?= $(PREFIX)/share/man

.PHONY: all check install link uninstall

all:
	@echo "nothing to build; 'make check' runs the tests, 'make install' installs under $(PREFIX)"

check:
	$(PYTHON) -m unittest discover -s tests

# copy: bin/diaspora and the package under share/diaspora
install:
	install -d $(DESTDIR)$(PREFIX)/bin $(DESTDIR)$(PREFIX)/share/diaspora/diaspora
	install -m 644 diaspora/*.py $(DESTDIR)$(PREFIX)/share/diaspora/diaspora/
	install -m 755 bin/diaspora $(DESTDIR)$(PREFIX)/bin/diaspora
	install -d $(DESTDIR)$(MANDIR)/man1
	install -m 644 man/diaspora.1 $(DESTDIR)$(MANDIR)/man1/diaspora.1

# or run straight from this checkout: bin/diaspora finds the package next to it
link:
	install -d $(DESTDIR)$(PREFIX)/bin
	ln -sfn $(CURDIR)/bin/diaspora $(DESTDIR)$(PREFIX)/bin/diaspora
	install -d $(DESTDIR)$(MANDIR)/man1
	ln -sfn $(CURDIR)/man/diaspora.1 $(DESTDIR)$(MANDIR)/man1/diaspora.1

uninstall:
	rm -f $(DESTDIR)$(PREFIX)/bin/diaspora
	rm -f $(DESTDIR)$(MANDIR)/man1/diaspora.1
	rm -rf $(DESTDIR)$(PREFIX)/share/diaspora
