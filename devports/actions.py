"""Stop, kill and restart what a tree node represents."""
from __future__ import annotations

import os
import signal
import subprocess

from .collect import Node, proc_stat


def targets(node: Node) -> tuple[list[tuple[int, int]], list[str]]:
    """(pid, start) pairs and container ids under node, deduplicated in tree order."""
    procs, containers = [], []
    for n in node.walk():
        procs.extend(p for p in n.procs if p not in procs)
        if n.container and n.container not in containers:
            containers.append(n.container)
    return procs, containers


def describe(node: Node) -> str:
    procs, containers = targets(node)
    parts = []
    if procs:
        parts.append(f"{len(procs)} process{'es' if len(procs) != 1 else ''}")
    if containers:
        parts.append(f"{len(containers)} container{'s' if len(containers) != 1 else ''}")
    return " + ".join(parts) or "nothing"


def _protected() -> set[int]:
    """Our own process and its ancestors: never signal the terminal devports is running in."""
    pids, pid = set(), os.getpid()
    while pid > 1 and (st := proc_stat(pid)):
        pids.add(pid)
        pid = st.ppid
    return pids | {0, 1}


def signal_procs(procs: list[tuple[int, int]], force: bool) -> tuple[int, list[str]]:
    """Signal each process that is still the one we sampled (same pid *and* start time)."""
    sig = signal.SIGKILL if force else signal.SIGTERM
    protected = _protected()
    sent, errors = 0, []
    # Children before parents, so wrappers like pnpm don't respawn or complain about a lost child.
    for pid, start in reversed(procs):
        st = proc_stat(pid)
        if pid in protected or not st or st.start != start:
            continue
        try:
            os.kill(pid, sig)
            sent += 1
        except ProcessLookupError:
            pass
        except PermissionError as e:
            errors.append(f"{pid}: {e.strerror}")
    return sent, errors


def docker(verb: str, ids: list[str]) -> tuple[bool, str]:
    if not ids:
        return True, ""
    try:
        r = subprocess.run(["docker", verb, *ids], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as e:
        return False, str(e)
    return r.returncode == 0, r.stderr.strip().splitlines()[-1] if r.returncode else ""


def stop(node: Node, force: bool = False) -> str:
    """Graceful stop (SIGTERM / docker stop) or force (SIGKILL / docker kill). Returns a status line."""
    procs, containers = targets(node)
    msgs = []
    if procs:
        sent, errors = signal_procs(procs, force)
        msgs.append(f"{'killed' if force else 'sent SIGTERM to'} {sent} process{'es' if sent != 1 else ''}")
        msgs.extend(errors[:2])
    if containers:
        ok, err = docker("kill" if force else "stop", containers)
        n = len(containers)
        msgs.append(f"{'killed' if force else 'stopped'} {n} container{'s' if n != 1 else ''}" if ok
                    else f"docker: {err}")
    return f"{node.name}: " + ("; ".join(msgs) or "nothing to stop")


def restart(node: Node) -> str:
    _, containers = targets(node)
    if not containers:
        return "Only containers can be restarted. Processes don't record how they were started."
    ok, err = docker("restart", containers)
    return f"{node.name}: restarted {len(containers)} container(s)" if ok else f"docker: {err}"


def open_url(port: int) -> None:
    subprocess.Popen(["xdg-open", f"http://localhost:{port}"], stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)
