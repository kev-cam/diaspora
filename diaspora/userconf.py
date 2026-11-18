# SPDX-License-Identifier: GPL-3.0-or-later
"""The user's own settings: ~/.config/diaspora.

So far: the repositories diaspora looks after, so that it can be started
anywhere and asked for one by name (`diaspora -C xyce`), or offer a choice
when it is not started in one.  A repository with a diaspora.conf that
diaspora is used in is added by itself (and the file made if there is
none).  Same syntax as diaspora.conf:

    [repo "xyce"]
        path = /usr/local/src/xyce

~/.config/diaspora may also be a directory holding a file `config`;
$XDG_CONFIG_HOME replaces ~/.config, and $DIASPORA_CONFIG names the file
outright.
"""

import os

from .config import CONF_NAME, Config
from .gitutil import DiasporaError

HEADER = """\
# ~/.config/diaspora - the repositories diaspora looks after.
# `diaspora -C <name> ...` works on one from anywhere; started elsewhere,
# diaspora asks which, and the `diaspora gui` page switches between them.
# diaspora adds a repository here the first time it is used in it.
"""


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

    def remember(self, repo):
        """Add `repo` (a Repo with a diaspora.conf) if it is not listed.
        Returns its name here; None if it was not added (or could not be)."""
        top = repo.top
        common = repo.common
        if os.path.basename(common) == ".git":
            top = os.path.dirname(common)   # a worktree: its main checkout
        top = os.path.abspath(top)
        if top in self.repos.values() or not os.path.isfile(
                os.path.join(top, CONF_NAME)):
            return None
        cache = os.path.join(os.environ.get("XDG_CACHE_HOME") or
                             os.path.join(os.path.expanduser("~"), ".cache"),
                             "diaspora")
        if top.startswith(cache + os.sep):
            return None                     # an import workspace
        name = os.path.basename(top)
        if self.find(name):
            name = "%s-%s" % (os.path.basename(os.path.dirname(top)), name)
            base, num = name, 2
            while self.find(name):
                name, num = "%s-%d" % (base, num), num + 1
        where = self.source
        try:
            fresh = not os.path.exists(where)
            if fresh:
                os.makedirs(os.path.dirname(where) or ".", exist_ok=True)
            with open(where, "a", encoding="utf-8") as handle:
                if fresh:
                    handle.write(HEADER)
                else:
                    with open(where, encoding="utf-8") as old:
                        text = old.read()
                    if text and not text.endswith("\n"):
                        handle.write("\n")
                handle.write('[repo "%s"]\n\tpath = %s\n' % (name, top))
        except OSError:
            return None
        self.repos[name] = top
        return name

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
