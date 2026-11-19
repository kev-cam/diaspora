# SPDX-License-Identifier: GPL-3.0-or-later
"""Thin wrappers around the git command line."""

import os
import re
import subprocess


class DiasporaError(Exception):
    """An error the user can act on; printed without a traceback."""


class GitError(DiasporaError):
    def __init__(self, cmd, rc, out, err):
        self.cmd, self.rc, self.out, self.err = cmd, rc, out, err
        text = (err or out or "").strip()
        super().__init__("%s failed (exit %d)%s" % (
            " ".join(cmd[:4]), rc, ": " + text if text else ""))


def run(cmd, cwd=None, check=True, input=None, env=None, timeout=None):
    proc = subprocess.run(cmd, cwd=cwd, input=input, env=env, timeout=timeout,
                          capture_output=True, text=True, errors="replace")
    if check and proc.returncode:
        raise GitError(cmd, proc.returncode, proc.stdout, proc.stderr)
    return proc


def owner(path):
    """The name of the user owning `path` ("" if unknown)."""
    try:
        import pwd
        return pwd.getpwuid(os.stat(path).st_uid).pw_name
    except (ImportError, KeyError, OSError):
        return ""


def me():
    try:
        import pwd
        return pwd.getpwuid(os.getuid()).pw_name
    except (ImportError, KeyError, AttributeError):
        return os.environ.get("USER", "")


class NotARepo(DiasporaError):
    pass


def group_of(path):
    """(group name, whether this process is in it, whether this user is
    listed in it in the group database) for `path`'s group."""
    try:
        gid = os.stat(path).st_gid
    except OSError:
        return "", False, False
    name, listed = str(gid), False
    try:
        import grp
        entry = grp.getgrgid(gid)
        name, listed = entry.gr_name, me() in entry.gr_mem
    except (ImportError, KeyError):
        pass
    return name, gid in os.getgroups() or gid == os.getegid(), listed


def shared_with_me(top):
    """Whether `top`, a repository of another user's, is shared with this
    one: its group is ours and we may write the checkout and its .git.
    Such a repository is as much ours as theirs, so git's ownership check
    (which keeps a stranger's hooks and config away from us) is moot."""
    _, member, _ = group_of(top)
    if not member:
        return False
    for where in (top, os.path.join(top, ".git")):
        try:
            if os.path.isdir(where) and not os.stat(where).st_mode & 0o020:
                return False            # (the group may not write it)
        except OSError:
            return False
    return True


def trust(path):
    """Tell git (ours and every child's) that `path` is safe, as
    `git config --global --add safe.directory` would, for this run only."""
    count = int(os.environ.get("GIT_CONFIG_COUNT", "0") or 0)
    for i in range(count):
        if (os.environ.get("GIT_CONFIG_KEY_%d" % i) == "safe.directory" and
                os.environ.get("GIT_CONFIG_VALUE_%d" % i) == path):
            return False
    os.environ["GIT_CONFIG_KEY_%d" % count] = "safe.directory"
    os.environ["GIT_CONFIG_VALUE_%d" % count] = path
    os.environ["GIT_CONFIG_COUNT"] = str(count + 1)
    return True


class NotYours(DiasporaError):
    """A repository of another user's that git or diaspora cannot use."""

    def __init__(self, top, why):
        self.top = top
        who, user = owner(top) or "another user", me() or "you"
        group, member, listed = group_of(top)
        head = "%s belongs to %s, not %s: %s." % (top, who, user, why)
        if listed and not member:
            fix = ("It is shared with group %s, which %s joined after "
                   "logging in: log out and in again (or run `newgrp %s`)."
                   % (group, user, group))
        elif member:
            fix = ("It is in your group %s but not writable by the group; "
                   "%s (or root) can fix that: chmod -R g+rwX %s && find %s "
                   "-type d -exec chmod g+s {} + && git -C %s config "
                   "core.sharedRepository group"
                   % (group, who, top, top, top))
        else:
            fix = ("Run diaspora as %s, or share the repository with a "
                   "group you are both in (as root: chgrp -R <group> %s && "
                   "chmod -R g+rwX %s && git -C %s config "
                   "core.sharedRepository group)." % (who, top, top, top))
        super().__init__(head + "  " + fix)


DUBIOUS = re.compile(r"dubious ownership in repository at '([^']+)'")


class Repo:
    """A git repository: the checkout diaspora was started in."""

    def __init__(self, path="."):
        for _ in range(4):
            proc = run(["git", "rev-parse", "--show-toplevel",
                        "--git-common-dir"], cwd=path, check=False)
            match = proc.returncode and DUBIOUS.search(proc.stderr or "")
            if not match:
                break
            where = match.group(1)
            if not (shared_with_me(where) and trust(where)):
                raise NotYours(where, "git will not work in another "
                               "user's repository")
        if proc.returncode:
            raise NotARepo("not inside a git repository: %s"
                           % os.path.abspath(path))
        top, common = proc.stdout.splitlines()[:2]
        self.top = top
        self.common = os.path.normpath(
            os.path.join(os.path.abspath(path), common))
        self.state = os.path.join(self.common, "diaspora")

    def check_writable(self):
        """Raise NotYours unless this user can write the checkout and its
        .git (diaspora fetches, commits and keeps its state there)."""
        bad = [p for p in (self.top, self.common, os.path.join(
            self.common, "objects"), os.path.join(self.common, "refs"))
            if os.path.isdir(p) and not os.access(p, os.W_OK)]
        if os.path.isdir(self.state) and not os.access(self.state, os.W_OK):
            bad.append(self.state)
        if bad:
            raise NotYours(self.top, "you cannot write to %s" % bad[0])

    # -- plumbing ---------------------------------------------------------
    def proc(self, *args, cwd=None, check=True, input=None, env=None):
        return run(["git"] + list(args), cwd=cwd or self.top, check=check,
                   input=input, env=env)

    def git(self, *args, **kw):
        return self.proc(*args, **kw).stdout.rstrip("\n")

    def ok(self, *args, **kw):
        return self.proc(*args, check=False, **kw).returncode == 0

    # -- queries ----------------------------------------------------------
    def rev(self, name, cwd=None):
        proc = self.proc("rev-parse", "-q", "--verify", name + "^{commit}",
                         cwd=cwd, check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def short(self, sha):
        return sha[:10]

    def current_branch(self, cwd=None):
        proc = self.proc("symbolic-ref", "-q", "--short", "HEAD", cwd=cwd,
                         check=False)
        return proc.stdout.strip() if proc.returncode == 0 else None

    def is_ancestor(self, older, newer):
        return self.ok("merge-base", "--is-ancestor", older, newer)

    def worktrees(self):
        """{branch name: worktree path} for every checked-out branch."""
        out, path = {}, None
        for line in self.git("worktree", "list", "--porcelain").splitlines():
            if line.startswith("worktree "):
                path = line[9:]
            elif line.startswith("branch refs/heads/") and path:
                out[line[18:]] = path
        return out

    def state_dir(self, *parts):
        path = os.path.join(self.state, *parts)
        os.makedirs(path, exist_ok=True)
        return path

    def identity_ok(self):
        return self.ok("var", "GIT_COMMITTER_IDENT")


# -- path globs -------------------------------------------------------------

def glob_to_regex(pattern):
    """Translate a path glob to a regex.

    `*` and `?` stay inside one path component, `**` crosses components,
    a trailing `/` means everything below.  A pattern without `/` matches
    the file name at any depth (as in .gitignore).
    """
    pat = pattern.strip()
    if pat.endswith("/"):
        pat += "**"
    anchored = "/" in pat
    pat = pat.lstrip("/")
    out, i = [], 0
    while i < len(pat):
        ch = pat[i]
        if pat.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pat.startswith("**", i):
            out.append(".*")
            i += 2
        elif ch == "*":
            out.append("[^/]*")
            i += 1
        elif ch == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(ch))
            i += 1
    body = "".join(out)
    return re.compile(("^" if anchored else "^(?:.*/)?") + body + "$")


class PathSet:
    """A list of globs; empty matches nothing."""

    def __init__(self, patterns=()):
        self.patterns = list(patterns)
        self._res = [glob_to_regex(p) for p in self.patterns]

    def __bool__(self):
        return bool(self._res)

    def match(self, path):
        return any(r.match(path) for r in self._res)

    def any(self, paths):
        return any(self.match(p) for p in paths)

    def all(self, paths):
        paths = list(paths)
        return bool(paths) and all(self.match(p) for p in paths)
