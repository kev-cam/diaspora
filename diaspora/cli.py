# SPDX-License-Identifier: GPL-3.0-or-later
"""The diaspora command line."""

import argparse
import json
import os
import sys
import time

from . import __version__, scan, triage
from . import ledger as L
from .agent import Agent
from .config import CONF_NAME, KINDS, LOCAL_NAME, Config
from .gitutil import DiasporaError, Repo
from .session import Session, record
from .ui import UI
from .workspace import Tester, Workspace

COMMANDS = ("import", "list", "export", "status", "show", "continue", "skip",
            "abort", "promote", "reject", "defer", "reopen", "withhold",
            "fetch", "log", "test", "init", "help")


# -- shared helpers -------------------------------------------------------------

def make_agent(repo, conf, ui, args):
    enabled = not getattr(args, "no_agent", False)
    agent = Agent(conf.agent, repo.state_dir("logs", "triage"),
                  verbose=getattr(args, "verbose", False), enabled=enabled)
    if enabled and conf.agent.enabled and not conf.agent.found:
        ui.warn("agent command %r not found: no agent triage, no porting"
                % conf.agent.command[0])
    return agent


def pick_target(repo, conf, args):
    target = getattr(args, "target", None) or (
        None if getattr(args, "here", False) else conf.target) \
        or repo.current_branch()
    if not target:
        raise DiasporaError("HEAD is detached: name the branch imports go "
                            "to with --target or diaspora.target")
    if not repo.rev(target):
        raise DiasporaError("target branch %s does not exist" % target)
    return target


def pick_peers(conf, names):
    if not conf.peers:
        raise DiasporaError("%s names no peers; add a [peer \"name\"] "
                            "section" % CONF_NAME)
    return [conf.peer(n) for n in names] if names \
        else list(conf.peers.values())


def gather(repo, conf, ui, agent, peers, target, args, direction="import"):
    """Fetch, scan and triage; returns [(peer, commits, dropped)]."""
    ledger = L.Ledger.at(repo, target, conf.ledger)
    quiet = getattr(args, "json", False)
    out = []
    for peer in peers:
        if not args.no_fetch:
            if not quiet:
                ui.say("Fetching %s (%s %s) ..." % (peer.name, peer.url,
                                                    peer.branch))
            scan.fetch(repo, peer)
        finder = scan.import_candidates if direction == "import" \
            else scan.export_candidates
        commits, dropped = finder(repo, conf, peer, target, ledger)
        triage.triage(repo, conf, peer, target, commits, agent,
                      None if quiet else ui.say, direction,
                      ask=not getattr(args, "pick", None))
        out.append((peer, commits, dropped))
    return out


def summarize(ui, peer, commits, dropped, direction):
    counts = {"import": 0, "review": 0, "ignore": 0}
    for commit in commits:
        counts[commit.disposition] += 1
    if direction == "import":
        text = "%s: %d commit%s to look at: %d suggested, %d to review, " \
               "%d ignored by policy" % (
                   peer.name, len(commits), "" if len(commits) == 1 else "s",
                   counts["import"], counts["review"], counts["ignore"])
    else:
        text = "%s: %d of our commits it does not have: %d to offer, %d " \
               "held back by policy" % (
                   peer.name, len(commits), counts["import"],
                   counts["ignore"] + counts["review"])
    ui.say(text)
    if dropped:
        ui.say("  not listed: " + ", ".join(
            "%d %s" % (n, why) for why, n in sorted(dropped.items())))


def visible(found, args):
    """The commits to list, oldest first per peer, and how many were cut."""
    shown = []
    for _peer, commits, _dropped in found:
        for commit in commits:
            if args.all or (commit.disposition != "ignore"
                            and commit.status != L.DEFERRED):
                shown.append(commit)
    cut = 0
    if args.max and len(shown) > args.max:
        cut = len(shown) - args.max
        shown = shown[:args.max]
    return shown, cut


def resolve_picks(found, picks):
    every = [c for _p, commits, _d in found for c in commits]
    out = []
    for pick in picks:
        hits = [c for c in every if c.sha.startswith(pick.lower())]
        if len(hits) != 1:
            raise DiasporaError(
                "%s: %s" % (pick, "matches several candidates" if hits else
                            "not a candidate commit of any peer (already "
                            "decided, already here, or filtered out; see "
                            "`diaspora list --all`)"))
        if hits[0] not in out:
            out.append(hits[0])
    order = {c.sha: n for n, c in enumerate(every)}
    return sorted(out, key=lambda c: order[c.sha])


def agent_cost(ui, agent):
    if agent.cost:
        ui.say("Agent: %d call%s, $%.2f." % (
            agent.calls, "" if agent.calls == 1 else "s", agent.cost))


def exit_code(session, status):
    if status in ("paused", "blocked"):
        return 3
    if session.data and any(i["state"] == "failed"
                            for i in session.data["items"]):
        return 4
    return 0


# -- commands -------------------------------------------------------------------

def cmd_import(repo, ui, args, direction="import"):
    conf = Config.load(repo)
    agent = make_agent(repo, conf, ui, args)
    target = pick_target(repo, conf, args)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    session = Session(repo, conf, ui, agent, interactive=interactive
                      and not args.yes, assume_yes=args.yes,
                      verbose=args.verbose)
    listing = args.cmd == "list"
    if session.exists() and not listing:
        raise DiasporaError(
            "a diaspora run is in progress (see `diaspora status`): "
            "`diaspora continue`, `diaspora promote` or `diaspora abort`")
    if args.strategy:
        conf.strategy = args.strategy
    if args.no_promote:
        conf.promote = "no"

    peers = pick_peers(conf, args.peers)
    if direction == "export":
        if len(peers) != 1:
            raise DiasporaError("name one peer to export to: %s"
                                % " ".join(conf.peers))
    found = gather(repo, conf, ui, agent, peers, target, args, direction)
    shown, cut = visible(found, args)

    if getattr(args, "json", False):
        print(json.dumps({
            "target": target, "direction": direction,
            "peers": [{"name": p.name, "url": p.url, "branch": p.branch,
                       "not_listed": dict(d),
                       "commits": [c.as_dict() for c in commits
                                   if args.all or c in shown]}
                      for p, commits, d in found]}, indent=1))
        return 0

    ui.say("")
    for peer, commits, dropped in found:
        summarize(ui, peer, commits, dropped, direction)
    if direction == "import" and not conf.has_requirements():
        ui.warn("%s requires no testing ([require] default = ...): imports "
                "would land untested" % CONF_NAME)
    ui.say("")

    picks = getattr(args, "pick", None)
    if picks:
        selected, entries = resolve_picks(found, picks), []
    elif not shown:
        ui.say("Nothing to %s." % direction)
        agent_cost(ui, agent)
        return 0
    elif listing or not (interactive or args.yes):
        marked = {c.sha for c in shown if c.disposition == "import"
                  and c.status == "pending"}
        ui.table(shown, marked)
        if cut:
            ui.say("... and %d more (--max 0 lists everything)" % cut)
        if not listing:
            ui.say("\nNot a terminal: `diaspora %s --yes` takes the [x] "
                   "commits, `--pick <commit>` names them." % direction)
        agent_cost(ui, agent)
        return 0
    elif args.yes:
        selected = [c for c in shown if c.disposition == "import"
                    and c.status == "pending"]
        entries = []
        ui.table(shown, {c.sha for c in selected})
    else:
        if cut:
            ui.say("(the first %d; %d more wait behind them, --max 0 lists "
                   "everything)" % (len(shown), cut))
        selected, entries = ui.choose(repo, shown, direction)
        if selected is None:
            selected = []
            ui.say("Nothing %sed." % direction)

    code = 0
    if selected:
        ui.say("")
        peer = peers[0] if direction == "export" else None
        status = session.start(direction, target, selected, entries, peer)
        code = exit_code(session, status)
    elif entries:
        record(repo, conf, ui, agent, target, entries)
    agent_cost(ui, agent)
    return code


def cmd_export(repo, ui, args):
    return cmd_import(repo, ui, args, direction="export")


def cmd_fetch(repo, ui, args):
    conf = Config.load(repo)
    for peer in pick_peers(conf, args.peers):
        ui.say("Fetching %s (%s %s) ..." % (peer.name, peer.url,
                                            peer.branch))
        scan.fetch(repo, peer)
    return 0


def cmd_status(repo, ui, args):
    conf = Config.load(repo)
    target = pick_target(repo, conf, args)
    ledger = L.Ledger.at(repo, target, conf.ledger)
    ui.say("Target branch: %s (%s)" % (target, repo.rev(target)[:10]))
    ui.say("Workspace:     %s%s" % (
        Workspace(repo, conf).path,
        " (inplace)" if conf.workspace == "inplace" else ""))
    for peer in conf.peers.values():
        ui.say("")
        ui.say("Peer %s: %s %s" % (peer.name, peer.url, peer.branch))
        tip = repo.rev(peer.ref)
        if not tip:
            ui.say("  not fetched yet (`diaspora fetch %s`)" % peer.name)
            continue
        ahead = repo.git("rev-list", "--count", "--no-merges", peer.ref,
                         "^" + target, *scan.since_args(repo, peer))
        behind = repo.git("rev-list", "--count", "--no-merges", target,
                          "^" + peer.ref)
        ui.say("  at %s (%s); it has %s commits we lack, we have %s it "
               "lacks" % (tip[:10], repo.git(
                   "show", "-s", "--format=%cs", tip), ahead, behind))
        for direction in ("import", "export"):
            counts = {}
            for item in ledger.latest(direction, peer.name).values():
                counts[item["verdict"]] = counts.get(item["verdict"], 0) + 1
            if counts:
                ui.say("  %s ledger: %s" % (direction, ", ".join(
                    "%d %s" % (n, v) for v, n in sorted(counts.items()))))
        ui.say("  imports: %s; ignores: %s; exports: %s" % (
            " ".join(peer.imports) or "nothing",
            " ".join(peer.ignore) or "nothing",
            " ".join(peer.exports) or "nothing"))
    ui.say("")
    if conf.tests:
        ui.say("Test levels: " + ", ".join(conf.tests))
        ui.say("Required for a bugfix: %s" % (" ".join(
            conf.chain(conf.levels_for(next(iter(conf.peers), ""),
                                       "bugfix"))) or "nothing"))
    session = Session(repo, conf, ui, make_agent(repo, conf, ui, args))
    if session.exists():
        data = session.load().data
        ui.say("")
        ui.say("Run %s in progress (%s, stage %s) on branch %s:" % (
            data["id"], data["dir"], data["stage"], data["branch"]))
        for item in data["items"]:
            ui.say("  %-9s %s %s  %s" % (
                item["state"], item["sha"][:10], item["subject"][:50],
                item.get("note") or ""))
        if any(i["state"] == "paused" for i in data["items"]):
            ui.say("Resolve the paused commit in %s, `git add`, then "
                   "`diaspora continue` (or `diaspora skip`)."
                   % session.ws.path)
        elif data["stage"] == "ready":
            ui.say("`diaspora promote` copies it to %s; `diaspora abort` "
                   "drops it." % data["target"])
        else:
            ui.say("`diaspora continue` goes on; `diaspora abort` drops it.")
    else:
        ui.say("No run in progress.")
    return 0


def cmd_show(repo, ui, args):
    conf = Config.load(repo)
    commit = scan.one(repo, repo.rev(args.commit) or args.commit)
    commit.kind, commit.confidence = triage.classify(commit)
    cached = triage.Cache(repo).data.get(commit.sha)
    if cached:
        triage._apply(commit, cached)
    ui.show(repo, commit, full=args.diff)
    target = pick_target(repo, conf, args)
    ledger = L.Ledger.at(repo, target, conf.ledger)
    for direction in ("import", "export"):
        item = ledger.latest(direction).get(commit.sha)
        if item:
            ui.say("\nledger (%s): %s %s%s" % (
                direction, item["verdict"], item["date"],
                " - " + item["note"] if item.get("note") else ""))
    if not cached:
        ui.say("\nkind (from the message): %s" % commit.kind)
    return 0


def _session(repo, ui, args):
    conf = Config.load(repo)
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    yes = getattr(args, "yes", False)
    session = Session(repo, conf, ui, make_agent(repo, conf, ui, args),
                      interactive=interactive and not yes, assume_yes=yes,
                      verbose=getattr(args, "verbose", False))
    return session.load()


def cmd_continue(repo, ui, args):
    session = _session(repo, ui, args)
    return exit_code(session, session.resume())


def cmd_skip(repo, ui, args):
    session = _session(repo, ui, args)
    return exit_code(session, session.skip())


def cmd_abort(repo, ui, args):
    _session(repo, ui, args).abort()
    return 0


def cmd_promote(repo, ui, args):
    _session(repo, ui, args).promote()
    return 0


def cmd_verdict(repo, ui, args):
    conf = Config.load(repo)
    target = pick_target(repo, conf, args)
    verdict = {"reject": L.REJECTED, "defer": L.DEFERRED,
               "reopen": L.PENDING, "withhold": L.WITHHELD}[args.cmd]
    direction = "export" if args.cmd == "withhold" or args.export \
        else "import"
    entries = []
    for name in args.commits:
        sha = repo.rev(name)
        if not sha:
            raise DiasporaError("%s: no such commit (fetch the peer first?)"
                                % name)
        if args.peer:
            peer = conf.peer(args.peer)
        elif direction == "export" and len(conf.peers) == 1:
            peer = next(iter(conf.peers.values()))
        elif direction == "export":
            raise DiasporaError("say which peer with --peer")
        else:
            homes = [p for p in conf.peers.values() if repo.rev(p.ref)
                     and repo.is_ancestor(sha, p.ref)]
            if len(homes) != 1:
                raise DiasporaError(
                    "%s is %s; say which with --peer" % (
                        name, "on several peers" if homes
                        else "not on any fetched peer branch"))
            peer = homes[0]
        commit = scan.one(repo, sha)
        commit.kind, _ = triage.classify(commit)
        entries.append(L.entry(peer.name, sha, verdict, commit.subject,
                               commit.kind, note=args.message,
                               direction=direction))
    record(repo, conf, ui, make_agent(repo, conf, ui, args), target, entries)
    return 0


def cmd_log(repo, ui, args):
    conf = Config.load(repo)
    target = pick_target(repo, conf, args)
    ledger = L.Ledger.at(repo, target, conf.ledger)
    rows = []
    for direction in ("import", "export"):
        rows += ledger.latest(direction, args.peer).values()
    rows.sort(key=lambda e: e.get("date", ""))
    if args.json:
        print(json.dumps(rows, indent=1))
        return 0
    for item in rows:
        ui.say("%s %-6s %-14s %s@%s %s%s" % (
            item.get("date", ""), item.get("dir", "import"), item["verdict"],
            item["peer"], item["commit"][:10], item.get("subject", "")[:60],
            "  [%s]" % item["note"] if item.get("note") else ""))
    if not rows:
        ui.say("The ledger (%s on %s) is empty." % (conf.ledger, target))
    return 0


def cmd_test(repo, ui, args):
    conf = Config.load(repo)
    target = pick_target(repo, conf, args)
    levels = args.levels or conf.levels_for("", "bugfix") or list(conf.tests)
    for name in levels:
        if name not in conf.tests:
            raise DiasporaError("no test level %r in %s (defined: %s)" % (
                name, CONF_NAME, " ".join(conf.tests) or "none"))
    if not levels:
        raise DiasporaError("%s defines no test levels" % CONF_NAME)
    if os.path.exists(os.path.join(repo.state_dir(), "session.json")):
        raise DiasporaError("a diaspora run is in progress; its workspace "
                            "is in use")
    ws = Workspace(repo, conf)
    sid = time.strftime("%Y%m%d-%H%M%S")
    branch = "diaspora/test/" + sid
    ui.say("Running %s on %s in %s" % (" ".join(conf.chain(levels)), target,
                                       ws.path))
    restore = ws.begin(branch, repo.rev(target))
    try:
        tester = Tester(conf, ws, os.path.join(repo.state_dir("logs"),
                                               "test-" + sid),
                        ui.step, verbose=args.verbose)
        ok, level, log = tester.run(levels, "target")
    finally:
        ws.end(restore)
        repo.proc("branch", "-D", branch, check=False)
    if not ok:
        ui.say("Level %s fails on %s; log: %s" % (level, target, log))
        return 1
    ui.say("All passed.")
    return 0


TEMPLATE = """\
# diaspora.conf - the peers of this repository, what moves between them
# and what testing an import must pass.
# Values run to the end of the line, as written (no quoting); comments are
# whole lines.  https://github.com/kev-cam/diaspora

[diaspora]
	# branch that imports land on (default: the branch you are on)
	target = {target}
	# kinds of change suggested for import / never listed; the rest is
	# listed for review.  Kinds: {kinds}
	import = bugfix security perf
	ignore = docs ci style
	# what we offer to peers (diaspora export)
	export = bugfix security perf

{peers}
# Test levels.  Commands run in the import workspace ($DIASPORA_SRC), a
# worktree kept between runs; $DIASPORA_BUILD is a build directory next
# to it.
{tests}
# What testing an import requires, by kind of change.
[require]
	default = {default}
	docs = none

# How this repository has moved away from its peers.
# rewritten: port the intent, the diff will not fit.  removed: changes
# there are not applicable.
#[divergence]
#	rewritten = src/jit/**
#	removed = contrib/**

# Paths that never leave this repository.
#[export]
#	exclude = private/**

# The agent that triages and ports (default: claude).  `command = none`
# runs without one.
#[agent]
#	command = claude
#	triage-model = haiku
"""


def cmd_init(repo, ui, args):
    path = os.path.join(repo.top, CONF_NAME)
    if os.path.exists(path) and not args.force:
        raise DiasporaError("%s already exists (--force overwrites it)"
                            % path)
    peers = []
    for remote in repo.git("remote").split():
        if remote == "origin":
            continue
        url = repo.git("remote", "get-url", remote)
        head = repo.git("symbolic-ref", "-q", "--short",
                        "refs/remotes/%s/HEAD" % remote, check=False)
        branch = head.split("/", 1)[1] if "/" in head else ""
        if not branch:
            for guess in ("main", "master"):
                if repo.rev("refs/remotes/%s/%s" % (remote, guess)):
                    branch = guess
                    break
        peers.append('[peer "%s"]\n\turl = %s\n%s' % (
            remote, url, "\tbranch = %s\n" % branch if branch else ""))
    if not peers:
        peers.append('#[peer "upstream"]\n#\turl = https://example.org/'
                     'project.git\n#\tbranch = main\n#\tsince = 2026-01-01\n')
    top = repo.top

    def has(name):
        return os.path.exists(os.path.join(top, name))

    if has("CMakeLists.txt"):
        build = ('cmake -S "$DIASPORA_SRC" -B "$DIASPORA_BUILD" && '
                 'cmake --build "$DIASPORA_BUILD" -j"$(nproc)"')
        check = 'ctest --test-dir "$DIASPORA_BUILD" -j"$(nproc)"'
    elif has("configure.ac") or has("configure"):
        build = ('mkdir -p "$DIASPORA_BUILD" && cd "$DIASPORA_BUILD" && '
                 '{ [ -f Makefile ] || "$DIASPORA_SRC/configure"; } && '
                 'make -j"$(nproc)"')
        check = 'make -C "$DIASPORA_BUILD" check'
    elif has("Cargo.toml"):
        build, check = "cargo build", "cargo test"
    elif has("pyproject.toml") or has("setup.py"):
        build, check = "python3 -m compileall -q .", "python3 -m pytest -q"
    elif has("Makefile") or has("GNUmakefile"):
        build, check = 'make -j"$(nproc)"', "make check"
    else:
        build, check = "true  # how to compile", "true  # how to test"
    tests = ('[test "compile"]\n\trun = %s\n'
             '[test "full-regression"]\n\tneeds = compile\n\trun = %s\n'
             '\ttimeout = 2h\n'
             '# If the suite has failing tests on your own branch, have the '
             'command list\n# them in "$DIASPORA_FAILURES" and gate on '
             '"none new":\n#\tpass = no-new-failures\n' % (build, check))
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(TEMPLATE.format(
            target=repo.current_branch() or "main", kinds=" ".join(KINDS),
            peers="\n".join(peers), tests=tests,
            default="full-regression"))
    exclude = os.path.join(repo.common, "info", "exclude")
    try:
        os.makedirs(os.path.dirname(exclude), exist_ok=True)
        with open(exclude, "a+", encoding="utf-8") as handle:
            handle.seek(0)
            if LOCAL_NAME not in handle.read():
                handle.write(LOCAL_NAME + "\n")
    except OSError:
        pass
    Config.load(repo)             # fail now if the template does not parse
    ui.say("Wrote %s; edit the peers and the test commands, then run "
           "`diaspora`." % path)
    ui.say("Machine-specific settings can go in %s (not committed)."
           % LOCAL_NAME)
    return 0


# -- argument parsing -----------------------------------------------------------

def build_parser():
    parser = argparse.ArgumentParser(
        prog="diaspora",
        description="Bring fixes and features over from peer repositories "
                    "(and offer ours to them).  Peers, policy and the "
                    "required testing are read from diaspora.conf.",
        epilog="With no command, `import` is run: it lists what the peers "
               "have for us and asks what to take.")
    parser.add_argument("-C", dest="dir", default=".", metavar="DIR",
                        help="run as if started in DIR")
    parser.add_argument("--version", action="version",
                        version="diaspora " + __version__)
    sub = parser.add_subparsers(dest="cmd", metavar="command")

    def scanning(p, picks=True):
        p.add_argument("peers", nargs="*", help="peers to look at "
                       "(default: all in diaspora.conf)")
        p.add_argument("--no-fetch", action="store_true",
                       help="use the peer branches as last fetched")
        p.add_argument("--all", action="store_true",
                       help="also list ignored and deferred commits")
        p.add_argument("--max", type=int, default=50, metavar="N",
                       help="list at most N commits (0: no limit; "
                            "default 50)")
        p.add_argument("--no-agent", action="store_true",
                       help="no AI agent: classify from commit messages "
                            "only, stop on conflicts")
        p.add_argument("--target", metavar="BRANCH",
                       help="branch to import into (default: "
                            "diaspora.target, else the current branch)")
        p.add_argument("--here", action="store_true",
                       help="import into the branch you are on, whatever "
                            "diaspora.target says")
        p.add_argument("-v", "--verbose", action="store_true",
                       help="show test and agent output as it runs")
        if picks:
            p.add_argument("-y", "--yes", action="store_true",
                           help="do not ask: take the suggested commits and "
                                "promote what passes")
            p.add_argument("--pick", action="append", metavar="COMMIT",
                           help="take this commit (repeatable); no list")
            p.add_argument("--strategy", choices=("each", "batch"),
                           help="test every import (each) or all together "
                                "(batch)")
            p.add_argument("--no-promote", action="store_true",
                           help="leave the result on the import branch")
        else:
            p.set_defaults(yes=False, pick=None, strategy=None,
                           no_promote=False)

    p = sub.add_parser("import", help="list what peers have for us, pick, "
                       "apply, test, copy to the target branch (default)")
    scanning(p)
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("list", help="list candidate imports and stop")
    scanning(p, picks=False)
    p.add_argument("--json", action="store_true", help="machine-readable")
    p.set_defaults(func=cmd_import)
    p = sub.add_parser("export", help="put our commits on a branch based "
                       "on a peer, ready to offer")
    scanning(p)
    p.set_defaults(func=cmd_export)

    p = sub.add_parser("status", help="peers, ledger and the run in "
                       "progress")
    p.set_defaults(func=cmd_status)
    p = sub.add_parser("show", help="show a candidate commit")
    p.add_argument("commit")
    p.add_argument("--diff", action="store_true", help="with the full diff")
    p.set_defaults(func=cmd_show)

    for name, func, text in (
            ("continue", cmd_continue, "go on after resolving a conflict"),
            ("skip", cmd_skip, "leave out the commit that is waiting"),
            ("abort", cmd_abort, "drop the run in progress"),
            ("promote", cmd_promote, "copy a finished run to the target")):
        p = sub.add_parser(name, help=text)
        p.add_argument("-y", "--yes", action="store_true",
                       help="do not ask before promoting")
        p.add_argument("--no-agent", action="store_true")
        p.add_argument("-v", "--verbose", action="store_true")
        p.set_defaults(func=func)

    for name, text in (
            ("reject", "never suggest these peer commits again"),
            ("defer", "hide these peer commits until asked (--all)"),
            ("reopen", "forget the verdict on these commits"),
            ("withhold", "never offer these commits of ours to a peer")):
        p = sub.add_parser(name, help=text)
        p.add_argument("commits", nargs="+", metavar="commit")
        p.add_argument("-m", "--message", help="why")
        p.add_argument("--peer", help="the peer concerned")
        p.add_argument("--export", action="store_true",
                       help="the verdict is about offering, not importing")
        p.add_argument("--target", metavar="BRANCH")
        p.set_defaults(func=cmd_verdict)

    p = sub.add_parser("fetch", help="fetch the peer branches")
    p.add_argument("peers", nargs="*")
    p.set_defaults(func=cmd_fetch)
    p = sub.add_parser("log", help="show the ledger")
    p.add_argument("--peer")
    p.add_argument("--json", action="store_true")
    p.add_argument("--target", metavar="BRANCH")
    p.set_defaults(func=cmd_log)
    p = sub.add_parser("test", help="run test levels on the target branch "
                       "in the workspace")
    p.add_argument("levels", nargs="*")
    p.add_argument("--target", metavar="BRANCH")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_test)
    p = sub.add_parser("init", help="write a starting diaspora.conf")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_init)
    return parser


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    # no command means `import`
    pos = 0
    while pos < len(argv) and argv[pos] == "-C":
        pos += 2
    if pos >= len(argv) or (argv[pos] not in COMMANDS and argv[pos] not in (
            "-h", "--help", "--version")):
        argv.insert(pos, "import")
    parser = build_parser()
    if argv[pos] == "help":
        parser.print_help()
        return 0
    args = parser.parse_args(argv)
    ui = UI()
    try:
        repo = Repo(args.dir)
        return args.func(repo, ui, args) or 0
    except DiasporaError as exc:
        ui.warn(str(exc))
        return 1
    except KeyboardInterrupt:
        ui.warn("interrupted; `diaspora status` shows where things stand")
        return 130


if __name__ == "__main__":
    sys.exit(main())
