"""Command line entry point: TUI by default, plus `list` and `kill` for scripts and agents."""
from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time

from . import __version__, actions
from .collect import NCPU, Node, Sampler, compact_ports, default_roots, short_path


def roots_from(args) -> list[str] | None:
    """--root DIR (repeatable) wins over DEVPORTS_ROOTS and the defaults."""
    return default_roots(":".join(args.root)) if args.root else None


def sample(show_all: bool, roots: list[str] | None = None, quick: bool = False):
    """One snapshot. CPU% needs two readings a second apart; quick skips that and reports 0 CPU."""
    sampler = Sampler(show_all, roots)
    if not quick:
        time.sleep(1)
    return sampler.snapshot()


def cmd_list(args) -> int:
    snap = sample(args.all, roots_from(args), getattr(args, "quick", False))
    if args.json:
        print(json.dumps({"system": dataclasses.asdict(snap.system),
                          "nodes": [dataclasses.asdict(n) for n in snap.nodes]}, indent=2))
        return 0
    from .tui import fmt_age, fmt_mem
    rows = []

    def add(n: Node, depth: int):
        rows.append(("  " * depth + n.name, compact_ports(n.ports), f"{n.cpu:.1f}", fmt_mem(n.mem),
                     fmt_age(n.started, snap.taken), str(n.pid or (n.container or "")[:12])))
        if depth < args.depth:
            for c in sorted(n.children, key=lambda c: -c.mem):
                add(c, depth + 1)

    for n in sorted(snap.nodes, key=lambda n: -n.mem):
        add(n, 0)
    if not rows:
        watched = "everything" if args.all else ", ".join(short_path(r) for r in roots_from(args) or default_roots())
        print(f"no dev servers or containers running (watching {watched}; -a includes every listener)")
        return 0
    head = ("NAME", "PORTS", "CPU%", "MEM", "UP", "PID/ID")
    widths = [max(len(r[i]) for r in [head, *rows]) for i in range(len(head))]
    widths[0], widths[1] = min(widths[0], 48), min(widths[1], 24)
    for r in [head, *rows]:
        print("  ".join(c[:widths[i]].ljust(widths[i]) if i < 2 else c.rjust(widths[i])
                        for i, c in enumerate(r)).rstrip())
    total_mem = sum(n.mem for n in snap.nodes)
    print(f"\ntotal {fmt_mem(total_mem)} ({total_mem / snap.system.mem_total * 100:.0f}% of RAM), "
          f"{sum(n.cpu for n in snap.nodes):.1f}% CPU (100% = one core, {NCPU} cores)")
    return 0


def cmd_kill(args) -> int:
    snap = Sampler(True).snapshot()
    matches = []
    for top in snap.nodes:
        for n in top.walk():
            if n.kind in ("server", "container") and args.port in n.ports:
                matches.append(n)
    if not matches:
        print(f"nothing devports can see is listening on :{args.port}", file=sys.stderr)
        return 1
    for n in matches:
        where = short_path(n.path) if n.path else n.kind
        print(f"{n.name} ({where}): {actions.describe(n)}")
        print("  " + actions.stop(n, args.force))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="devports", description="Local dev servers and Docker containers, grouped by "
                                "project. Runs the TUI, or `list` / `kill` for scripts and agents.")
    p.add_argument("-a", "--all", action="store_true", help="include listeners outside the project roots")
    p.add_argument("-r", "--root", action="append", metavar="DIR",
                   help="project folder to watch (repeatable). Default: $DEVPORTS_ROOTS, else whichever of "
                        "~/Projects, ~/code, ~/src, ~/dev, ~/repos, … exist")
    p.add_argument("-i", "--interval", type=float, default=2.0, help="TUI refresh seconds (default 2)")
    p.add_argument("--version", action="version", version=f"devports {__version__}")
    sub = p.add_subparsers(dest="cmd")
    ls = sub.add_parser("list", help="print a table and exit")
    ls.add_argument("-a", "--all", action="store_true", default=argparse.SUPPRESS)
    ls.add_argument("-r", "--root", action="append", metavar="DIR", default=argparse.SUPPRESS)
    ls.add_argument("-d", "--depth", type=int, default=1, help="tree depth to print (default 1)")
    ls.add_argument("--json", action="store_true", help="full tree as JSON")
    ls.add_argument("-q", "--quick", action="store_true", help="don't wait a second to measure CPU (reports 0)")
    kill = sub.add_parser("kill", help="stop whatever serves PORT (process tree or container)")
    kill.add_argument("port", type=int)
    kill.add_argument("-f", "--force", action="store_true", help="SIGKILL / docker kill")
    args = p.parse_args(argv)

    if args.cmd == "list":
        return cmd_list(args)
    if args.cmd == "kill":
        return cmd_kill(args)
    if not sys.stdout.isatty():
        return cmd_list(argparse.Namespace(all=args.all, root=args.root, depth=1, json=False))
    from .tui import run
    run(args.all, args.interval, roots_from(args))
    return 0


if __name__ == "__main__":
    sys.exit(main())
