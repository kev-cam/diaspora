# SPDX-License-Identifier: GPL-3.0-or-later
"""diaspora.conf: peers, import/export policy, test levels.

The file looks like a git config file (`[section "name"]`, `key = value`)
but is read more simply: a value is everything to the end of its line,
taken literally, so shell commands need no quoting.  Comments are whole
lines starting with `#` or `;`.  A trailing backslash continues a value
on the next line.  A key may be repeated; what that means depends on the
key (lists add up, `run` lines are joined with `&&`).
"""

import os
import re
import shutil
from dataclasses import dataclass, field

from .gitutil import DiasporaError, PathSet

CONF_NAME = "diaspora.conf"
LOCAL_NAME = "diaspora.local.conf"
LEDGER_NAME = "diaspora.ledger"
LOG_NAME = "diaspora.log"

KINDS = ("bugfix", "security", "perf", "feature", "test", "refactor",
         "docs", "build", "ci", "style", "other")
DEFAULT_IMPORT = ("bugfix", "security", "perf")
DEFAULT_IGNORE = ("docs", "ci", "style")
DEFAULT_EXPORT = ("bugfix", "security", "perf")

_SECTION = re.compile(r'^\[\s*([A-Za-z][A-Za-z0-9-]*)(?:\s+"([^"]*)")?\s*\]$')
_KEY = re.compile(r"^[a-z][a-z0-9-]*$")
_TRUE = {"true", "yes", "on", "1", ""}
_FALSE = {"false", "no", "off", "0"}


def parse_bool(value, what):
    low = str(value).strip().lower()
    if low in _TRUE:
        return True
    if low in _FALSE:
        return False
    raise DiasporaError("%s: expected true or false, got %r" % (what, value))


def parse_duration(value, what):
    """'90s', '45m', '2h', or plain seconds."""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", str(value))
    if not m:
        raise DiasporaError("%s: bad duration %r (use 90s, 45m, 2h)"
                            % (what, value))
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600,
                                "d": 86400}[m.group(2)]


def split_words(values):
    out = []
    for value in values:
        out.extend(w for w in re.split(r"[\s,]+", value) if w)
    return out


@dataclass
class Peer:
    name: str
    url: str
    branch: str = "HEAD"
    since: str = None
    imports: tuple = DEFAULT_IMPORT      # kinds suggested for import
    ignore: tuple = DEFAULT_IGNORE       # kinds never listed
    exports: tuple = DEFAULT_EXPORT      # kinds offered to the peer
    paths: PathSet = field(default_factory=PathSet)
    exclude: PathSet = field(default_factory=PathSet)
    export_paths: PathSet = field(default_factory=PathSet)
    export_exclude: PathSet = field(default_factory=PathSet)
    export_test: list = field(default_factory=list)
    max_lines: int = 0

    @property
    def ref(self):
        return "refs/diaspora/%s/%s" % (self.name, self.branch)


@dataclass
class TestLevel:
    name: str
    run: str
    needs: list = field(default_factory=list)
    timeout: float = None
    retry: int = 0
    # "exit": the command's exit status decides.  "no-new-failures": the
    # command lists the failing tests in $DIASPORA_FAILURES and the level
    # passes when none fails that did not already fail before the import.
    mode: str = "exit"
    reference_age: float = 86400.0   # how long a measured reference is good
    runs: list = field(default_factory=list)   # the run lines, as written

    @property
    def compares(self):
        return self.mode == "no-new-failures"


@dataclass
class AgentConf:
    command: list = field(default_factory=lambda: ["claude"])
    protocol: str = "claude"            # claude | plain
    triage: str = "auto"                # auto | all | off
    port: bool = True
    model: str = None
    triage_model: str = None
    triage_batch: int = 1
    triage_max: int = 100
    triage_jobs: int = 4
    timeout: float = 1800.0
    fix_attempts: int = 1
    triage_args: list = None
    port_args: list = None

    @property
    def enabled(self):
        return bool(self.command) and self.command[0] != "none"

    @property
    def path(self):
        """Where the command is: on PATH, or for a bare `claude`, where
        Claude Code's installers put it (often not on PATH); None if
        nowhere."""
        if not self.enabled:
            return None
        name = self.command[0]
        hit = shutil.which(os.path.expanduser(name))
        if hit or os.sep in name:
            return hit
        home = os.path.expanduser("~")
        for where in AGENT_PLACES.get(name, ()):
            where = os.path.join(home, where)
            if os.path.isfile(where) and os.access(where, os.X_OK):
                return where
        return None

    @property
    def found(self):
        return self.path is not None

    @property
    def argv(self):
        """The command to run, found."""
        return [self.path or self.command[0]] + self.command[1:]


# where Claude Code's installers put `claude`, under the user's home
AGENT_PLACES = {"claude": (".local/bin/claude", ".claude/local/claude",
                           ".npm-global/bin/claude", "bin/claude")}


class Config:
    def __init__(self, repo, entries, path):
        self.repo = repo
        self.path = path
        self._sec = {}
        for section, sub, key, value in entries:
            self._sec.setdefault((section, sub), {}).setdefault(
                key, []).append(value)
        self._build()

    # -- loading ------------------------------------------------------------
    @staticmethod
    def parse(text, source):
        """[(section, subsection, key, value)] of a config text."""
        entries, section, sub = [], None, ""
        lines = text.splitlines()
        num = 0
        while num < len(lines):
            line = lines[num].strip()
            num += 1
            start = num
            if not line or line[0] in "#;":
                continue
            while line.endswith("\\") and num < len(lines):
                line = line[:-1].rstrip() + " " + lines[num].strip()
                num += 1
            m = _SECTION.match(line)
            if m:
                section, sub = m.group(1).lower(), m.group(2) or ""
                continue
            key, sep, value = line.partition("=")
            key = key.strip().lower()
            if line.startswith("[") or not _KEY.match(key):
                raise DiasporaError("%s line %d: cannot read %r" % (
                    source, start, lines[start - 1].strip()))
            if section is None:
                raise DiasporaError("%s line %d: %r is outside any [section]"
                                    % (source, start, key))
            entries.append((section, sub, key,
                            value.strip() if sep else "true"))
        return entries

    @staticmethod
    def read_entries(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return Config.parse(handle.read(), os.path.basename(path))
        except OSError as exc:
            raise DiasporaError("cannot read %s: %s" % (path, exc))

    @classmethod
    def load(cls, repo):
        path = os.path.join(repo.top, CONF_NAME)
        if not os.path.exists(path):
            raise DiasporaError(
                "no %s in %s (run `diaspora init` to write a starting point)"
                % (CONF_NAME, repo.top))
        entries = cls.read_entries(path)
        local = os.path.join(repo.top, LOCAL_NAME)
        if os.path.exists(local):
            # a key set in the local file replaces that key, not adds to it
            mine = cls.read_entries(local)
            keys = {entry[:3] for entry in mine}
            entries = [e for e in entries if e[:3] not in keys] + mine
        return cls(repo, entries, path)

    def comments(self):
        """{(section, sub): the comment lines just above its header}.

        From diaspora.conf, then diaspora.local.conf (whose comment wins).
        Only comments that touch the header, with no blank line between.
        """
        out = {}
        for path in (self.path, os.path.join(os.path.dirname(self.path),
                                             LOCAL_NAME)):
            try:
                with open(path, encoding="utf-8") as handle:
                    lines = handle.read().splitlines()
            except OSError:
                continue
            block = []
            for line in lines:
                text = line.strip()
                if text[:1] in ("#", ";"):
                    block.append(text[1:].strip())
                    continue
                m = _SECTION.match(text)
                if m and block:
                    out[(m.group(1).lower(), m.group(2) or "")] = \
                        "\n".join(block)
                block = []
        return out

    # -- raw access ---------------------------------------------------------
    def _values(self, section, sub, key):
        return self._sec.get((section, sub), {}).get(key, [])

    def _one(self, section, sub, key, default=None):
        values = self._values(section, sub, key)
        return values[-1] if values else default

    def _words(self, section, sub, key):
        return split_words(self._values(section, sub, key))

    def _subs(self, section):
        seen = []
        for sec, sub in self._sec:
            if sec == section and sub and sub not in seen:
                seen.append(sub)
        return seen

    def _err(self, msg):
        raise DiasporaError("%s: %s" % (os.path.basename(self.path), msg))

    def _kinds(self, section, sub, key, default):
        values = self._values(section, sub, key)
        if not values:
            return default
        words = split_words(values)
        if words == ["all"]:
            return KINDS
        if words in ([], ["none"]):
            return ()
        for word in words:
            if word not in KINDS:
                self._err("%s.%s: unknown kind %r (kinds: %s)"
                          % (section + ("." + sub if sub else ""), key, word,
                             " ".join(KINDS)))
        return tuple(words)

    # -- interpretation -----------------------------------------------------
    def _build(self):
        one, words = self._one, self._words
        d = "diaspora"
        self.target = one(d, "", "target")
        self.workspace = one(d, "", "workspace", "worktree")
        if self.workspace not in ("worktree", "inplace"):
            self._err("diaspora.workspace must be worktree or inplace")
        self.worktree = one(d, "", "worktree")
        self.build = one(d, "", "build")
        self.strategy = one(d, "", "strategy", "each")
        if self.strategy not in ("each", "batch"):
            self._err("diaspora.strategy must be each or batch")
        self.promote = one(d, "", "promote", "ask")
        if self.promote not in ("ask", "yes", "no"):
            self._err("diaspora.promote must be ask, yes or no")
        self.ledger = one(d, "", "ledger", LEDGER_NAME)
        self.log = one(d, "", "log", LOG_NAME)

        def_import = self._kinds(d, "", "import", DEFAULT_IMPORT)
        def_ignore = self._kinds(d, "", "ignore", DEFAULT_IGNORE)
        def_export = self._kinds(d, "", "export", DEFAULT_EXPORT)
        def_max = int(one(d, "", "max-lines", "0"))

        # test levels
        self.tests = {}
        for name in self._subs("test"):
            runs = self._values("test", name, "run")
            if not runs:
                self._err('[test "%s"] has no run command' % name)
            timeout = one("test", name, "timeout")
            mode = one("test", name, "pass", "exit")
            if mode not in ("exit", "no-new-failures"):
                self._err('[test "%s"] pass must be exit or no-new-failures'
                          % name)
            self.tests[name] = TestLevel(
                name=name, run=" && ".join(runs),
                needs=words("test", name, "needs"),
                timeout=parse_duration(timeout, "test.%s.timeout" % name)
                if timeout else None,
                retry=int(one("test", name, "retry", "0")), mode=mode,
                runs=list(runs),
                reference_age=parse_duration(
                    one("test", name, "reference-age", "1d"),
                    "test.%s.reference-age" % name))
        for level in self.tests.values():
            for need in level.needs:
                if need not in self.tests:
                    self._err('[test "%s"] needs unknown level %r'
                              % (level.name, need))

        # what testing an import requires: [require] and [require "peer"]
        self.require = {}
        for sub in [""] + self._subs("require"):
            table = {}
            for key in self._sec.get(("require", sub), {}):
                if key != "default" and key not in KINDS:
                    self._err("require.%s: not a kind" % key)
                table[key] = self._levels("require", sub, key)
            self.require[sub] = table
        legacy = self._values(d, "", "test")
        if legacy and "default" not in self.require[""]:
            self.require[""]["default"] = self._levels(d, "", "test")

        # peers
        self.peers = {}
        for name in self._subs("peer"):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", name):
                self._err("peer name %r: use letters, digits, . _ -" % name)
            url = one("peer", name, "url")
            remote = one("peer", name, "remote")
            if not url and remote:
                url = self.repo.git("remote", "get-url", remote, check=False)
            if not url:
                self._err('[peer "%s"] has no url' % name)
            test = self._values("peer", name, "test")
            if test:
                self.require.setdefault(name, {}).setdefault(
                    "default", self._levels("peer", name, "test"))
            imports = self._kinds("peer", name, "import", def_import)
            ignore = self._kinds("peer", name, "ignore", def_ignore)
            review = self._kinds("peer", name, "review", ())
            self.peers[name] = Peer(
                name=name, url=url,
                branch=one("peer", name, "branch", "HEAD"),
                since=one("peer", name, "since"),
                imports=tuple(k for k in imports if k not in review),
                ignore=tuple(k for k in ignore
                             if k not in review and k not in imports),
                exports=self._kinds("peer", name, "export", def_export),
                paths=PathSet(words("peer", name, "paths")),
                exclude=PathSet(words("peer", name, "exclude")),
                export_paths=PathSet(words("peer", name, "export-paths")),
                export_exclude=PathSet(words("peer", name, "export-exclude")),
                export_test=self._levels("peer", name, "export-test"),
                max_lines=int(one("peer", name, "max-lines", def_max)))

        self.rewritten = PathSet(words("divergence", "", "rewritten"))
        self.removed = PathSet(words("divergence", "", "removed"))
        # files every commit adds a line to (test lists, NEWS): both sides
        # changing one is not an overlap worth a warning
        self.lists = PathSet(words("divergence", "", "lists"))
        # paths that never leave this repository (read by peers, too)
        self.export_exclude = PathSet(words("export", "", "exclude"))
        self.export_paths = PathSet(words("export", "", "paths"))

        a = "agent"
        command = split_words(self._values(a, "", "command"))
        if not command:
            # not named here: the user's own choice (~/.config/diaspora;
            # it differs from one user to the next), else Claude Code
            from .userconf import UserConf
            try:
                command = split_words(UserConf.load().agent_command)
            except DiasporaError:
                command = []
        command = command or ["claude"]
        protocol = one(a, "", "protocol") or (
            "claude" if os.path.basename(command[0]) == "claude" else "plain")
        if protocol not in ("claude", "plain"):
            self._err("agent.protocol must be claude or plain")
        triage = one(a, "", "triage", "auto")
        if triage not in ("auto", "all", "off"):
            self._err("agent.triage must be auto, all or off")
        t_args, p_args = one(a, "", "triage-args"), one(a, "", "port-args")
        self.agent = AgentConf(
            command=command, protocol=protocol, triage=triage,
            port=parse_bool(one(a, "", "port", "true"), "agent.port"),
            model=one(a, "", "model"),
            triage_model=one(a, "", "triage-model"),
            triage_batch=int(one(a, "", "triage-batch", "1")),
            triage_max=int(one(a, "", "triage-max", "100")),
            triage_jobs=int(one(a, "", "triage-jobs", "4")),
            timeout=parse_duration(one(a, "", "timeout", "30m"),
                                   "agent.timeout"),
            fix_attempts=int(one(a, "", "fix-attempts", "1")),
            triage_args=t_args.split() if t_args is not None else None,
            port_args=p_args.split() if p_args is not None else None)

    def _levels(self, section, sub, key):
        words = self._words(section, sub, key)
        if words == ["none"]:
            return []
        for word in words:
            if word not in self.tests:
                self._err("%s%s.%s: unknown test level %r (defined: %s)" % (
                    section, "." + sub if sub else "", key, word,
                    " ".join(self.tests) or "none"))
        return words

    # -- questions ------------------------------------------------------------
    def peer(self, name):
        if name not in self.peers:
            raise DiasporaError("no peer %r in %s (peers: %s)" % (
                name, CONF_NAME, " ".join(self.peers) or "none"))
        return self.peers[name]

    def levels_for(self, peer_name, kind):
        """Test levels an import of this kind from this peer must pass.

        Most specific wins: [require "peer"] kind, [require] kind,
        the peer's default, the global default.
        """
        for sub, key in ((peer_name, kind), ("", kind),
                         (peer_name, "default"), ("", "default")):
            table = self.require.get(sub, {})
            if key in table:
                return list(table[key])
        return []

    def has_requirements(self):
        return any(self.require.values())

    def chain(self, levels):
        """Expand `needs`, dependencies first, in a stable order."""
        out = []

        def visit(name, trail):
            if name in trail:
                self._err("test levels need each other: %s"
                          % " -> ".join(trail + [name]))
            for need in self.tests[name].needs:
                visit(need, trail + [name])
            if name not in out:
                out.append(name)

        for name in self.tests:           # declaration order
            if name in levels:
                visit(name, [])
        return out

    @staticmethod
    def peer_export_rules(repo, ref):
        """The [export] section a peer publishes in its own diaspora.conf."""
        proc = repo.proc("show", "%s:%s" % (ref, CONF_NAME), check=False)
        if proc.returncode:
            return PathSet(), PathSet()
        try:
            entries = Config.parse(proc.stdout, CONF_NAME)
        except DiasporaError:
            return PathSet(), PathSet()
        paths, exclude = [], []
        for section, sub, key, value in entries:
            if section == "export" and not sub:
                if key == "paths":
                    paths += split_words([value])
                elif key == "exclude":
                    exclude += split_words([value])
        return PathSet(paths), PathSet(exclude)
