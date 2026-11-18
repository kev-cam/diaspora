# SPDX-License-Identifier: GPL-3.0-or-later
"""The user's own settings: ~/.config/diaspora.

So far: the repositories diaspora looks after, so that it can be started
anywhere and asked for one by name (`diaspora -C xyce`), or offer a choice
when it is not started in one.  Same syntax as diaspora.conf:

    [repo "xyce"]
        path = /usr/local/src/xyce

~/.config/diaspora may also be a directory holding a file `config`;
$XDG_CONFIG_HOME replaces ~/.config, and $DIASPORA_CONFIG names the file
outright.
"""

import os

from .config import CONF_NAME, Config
from .gitutil import DiasporaError


def path():
    """The user config file (which need not exist)."""
    named = os.environ.get("DIASPORA_CONFIG")
    if named:
        return os.path.expanduser(named)
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(
        os.path.expanduser("~"), ".config")
    where = os.path.join(base, "diaspora")
    if os.path.isdir(where):
        where = os.path.join(where, "config")
    return where


class UserConf:
    def __init__(self, entries=(), source=None):
        self.source = source
        self.repos = {}             # name -> path, in the file's order
        for section, sub, key, value in entries:
            if section != "repo":
                continue
            if not sub:
                raise DiasporaError('%s: [repo] needs a name: [repo "name"]'
                                    % source)
            if key == "path":
                self.repos[sub] = os.path.abspath(os.path.expanduser(value))

    @classmethod
    def load(cls):
        where = path()
        if not os.path.isfile(where):
            return cls(source=where)
        return cls(Config.read_entries(where), where)

    def find(self, name):
        """The path of the repository called `name`, or None."""
        if name in self.repos:
            return self.repos[name]
        low = [n for n in self.repos if n.lower() == name.lower()]
        return self.repos[low[0]] if len(low) == 1 else None

    def listing(self):
        """[(name, path, note)]: note says what is wrong, if anything."""
        out = []
        for name, where in self.repos.items():
            if not os.path.isdir(where):
                note = "missing"
            elif not os.path.isfile(os.path.join(where, CONF_NAME)):
                note = "no %s" % CONF_NAME
            else:
                note = ""
            out.append((name, where, note))
        return out
