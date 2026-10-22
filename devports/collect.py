"""Snapshot local dev servers and docker containers, grouped by project.

Everything here reads /proc, /sys/fs/cgroup, `ss` and `docker ps`; nothing needs root.
`build_tree` is pure so grouping rules can be tested without a live system.
"""
from __future__ import annotations

import os
import re
import subprocess
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field

HOME = os.path.expanduser("~")
TICK = os.sysconf("SC_CLK_TCK")
NCPU = os.cpu_count() or 1

# Walking up from a listening process stops at these: they're what you launched from, not the server.
LAUNCHERS = {
    "bash", "zsh", "fish", "sh", "dash", "nu", "tmux: server", "tmux", "zellij", "herdr", "screen",
    "alacritty", "ghostty", "kitty", "foot", "wezterm-gui", "systemd", "init", "sshd", "code",
    "claude", "codex", "opencode", "Hyprland", "hyprland", "uwsm", "login", "sudo",
    # Other terminals, editors and agents that can spawn a server directly (e.g. via `sh -c`):
    # stopping a server must never take them down with it.
    "konsole", "xterm", "rio", "wezterm", "tilix", "gnome-terminal-", "kgx", "ptyxis",
    "nvim", "vim", "hx", "helix", "emacs", "cursor", "zed", "zed-editor", "windsurf",
    "gemini", "aider", "amp", "goose", "crush",
}
SHELLS = {"bash", "zsh", "fish", "sh", "dash", "nu"}
INTERPRETERS = re.compile(r"^(node|nodejs|bun|deno|python[\d.]*|ruby|php|java)$")
SOURCE_FILE = re.compile(r"\.(m?[jt]s|cjs|cts|mts|py|rb|php)$")
# Interpreter flags that consume the next argument.
VALUE_FLAGS = {"-r", "--require", "--import", "--loader", "--experimental-loader", "-X", "-W", "--env-file"}


# Where people usually keep their checkouts. Whichever of these exist are watched by default.
COMMON_ROOTS = ["~/Projects", "~/projects", "~/Code", "~/code", "~/src", "~/dev", "~/Developer",
                "~/repos", "~/work", "~/git", "~/workspace"]


def default_roots(env: str | None = None) -> list[str]:
    """DEVPORTS_ROOTS (colon separated) if set, else the common project folders that exist, else ~."""
    raw = os.environ.get("DEVPORTS_ROOTS", "") if env is None else env
    wanted = raw.split(":") if raw.strip() else COMMON_ROOTS
    roots = []
    for p in wanted:
        r = os.path.realpath(os.path.expanduser(p.strip())) if p.strip() else ""
        if r and r not in roots and (raw.strip() or os.path.isdir(r)):
            roots.append(r)
    return roots or [os.path.realpath(os.path.expanduser("~"))]


def read(path: str, default: str = "") -> str:
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return default


def short_path(path: str) -> str:
    return "~" + path[len(HOME):] if path == HOME or path.startswith(HOME + "/") else path


# ---------------------------------------------------------------- /proc helpers

@dataclass(frozen=True)
class ProcStat:
    comm: str
    ppid: int
    ticks: int   # utime + stime
    start: int   # clock ticks after boot; with pid, identifies a process across pid reuse


def proc_stat(pid: int) -> ProcStat | None:
    s = read(f"/proc/{pid}/stat")
    if not s:
        return None
    comm = s[s.index("(") + 1:s.rindex(")")]
    f = s[s.rindex(")") + 2:].split()
    return ProcStat(comm, int(f[1]), int(f[11]) + int(f[12]), int(f[19]))


def all_procs() -> dict[int, ProcStat]:
    procs = {}
    for d in os.listdir("/proc"):
        if d.isdigit() and (st := proc_stat(int(d))):
            procs[int(d)] = st
    return procs


def cmdline(pid: int) -> list[str]:
    return [a for a in read(f"/proc/{pid}/cmdline").split("\0") if a]


def cwd(pid: int) -> str:
    try:
        return os.readlink(f"/proc/{pid}/cwd")
    except OSError:
        return ""


def pss_mib(pid: int) -> float:
    """Proportional set size: shared pages are split between their users, so sums don't double count."""
    m = re.search(r"^Pss:\s+(\d+)", read(f"/proc/{pid}/smaps_rollup"), re.M)
    return int(m.group(1)) / 1024 if m else 0.0


def boot_time() -> float:
    m = re.search(r"^btime (\d+)", read("/proc/stat"), re.M)
    return float(m.group(1)) if m else time.time() - float(read("/proc/uptime", "0").split()[0])


def system_cpu_ticks() -> tuple[int, int]:
    """(busy, total) jiffies across all cpus."""
    vals = [int(v) for v in read("/proc/stat").splitlines()[0].split()[1:]]
    idle = vals[3] + (vals[4] if len(vals) > 4 else 0)
    return sum(vals) - idle, sum(vals)


def meminfo() -> tuple[float, float]:
    """(used, total) MiB."""
    info = dict(re.findall(r"^(\w+):\s+(\d+)", read("/proc/meminfo"), re.M))
    total = int(info.get("MemTotal", 0)) / 1024
    return total - int(info.get("MemAvailable", 0)) / 1024, total


# ---------------------------------------------------------------- parsing

def parse_ss(text: str) -> dict[int, set[int]]:
    """pid -> listening ports, from `ss -tlnpH`. Only processes we own carry a pid without root."""
    ports: dict[int, set[int]] = defaultdict(set)
    for line in text.splitlines():
        cols = line.split()
        if len(cols) < 5 or ":" not in cols[3]:
            continue
        port = cols[3].rsplit(":", 1)[1]
        if not port.isdigit():
            continue
        # The users:(...) column holds process names that may contain spaces ("next-server (v16)").
        for pid in re.findall(r"pid=(\d+)", " ".join(cols[5:])):
            ports[int(pid)].add(int(port))
    return dict(ports)


def listeners() -> dict[int, set[int]]:
    try:
        out = subprocess.run(["ss", "-tlnpH"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return {}
    return parse_ss(out)


def parse_docker_ports(text: str) -> list[int]:
    """Host ports published by a container, e.g. '0.0.0.0:54322->5432/tcp, [::]:54322->5432/tcp'."""
    ports = set()
    for lo, hi in re.findall(r":(\d+)(?:-(\d+))?->", text):
        ports.update(range(int(lo), int(hi or lo) + 1))
    return sorted(ports)


def compact_ports(ports) -> str:
    """[3000, 54321, 54322, 54323] -> '3000,54321-54323'."""
    ps = sorted(set(ports))
    out, i = [], 0
    while i < len(ps):
        j = i
        while j + 1 < len(ps) and ps[j + 1] == ps[j] + 1:
            j += 1
        out.append(str(ps[i]) if i == j else f"{ps[i]}-{ps[j]}")
        i = j + 1
    return ",".join(out)


def friendly_cmd(argv: list[str], comm: str = "", limit: int = 60, cwd: str = "") -> str:
    """A readable name for a command: drop the interpreter and paths, keep the tool and its args."""
    if not argv:
        return comm
    args = list(argv)
    # Processes that rewrite their title (next-server, chromium) put everything in argv[0].
    if len(args) == 1 and " " in args[0]:
        args = args[0].split()
    while len(args) > 1 and INTERPRETERS.match(os.path.basename(args[0])):
        args.pop(0)
        while args and args[0].startswith("-"):
            flag = args.pop(0)
            if flag == "-m":
                break  # python -m module: the module is the program
            if flag in VALUE_FLAGS and args:
                args.pop(0)
    if not args:
        return comm
    head = args[0]
    rel = head if not head.startswith("/") else os.path.relpath(head, cwd) if cwd and head.startswith(cwd + "/") else ""
    if rel and SOURCE_FILE.search(rel) and "node_modules" not in rel and not rel.startswith("."):
        head = rel   # packages/server/src/index.ts says more than index.ts
    elif "/" in head:
        head = os.path.basename(head)
    return " ".join([head, *args[1:]])[:limit]


def project_dir(path: str) -> str:
    """The enclosing git checkout of path (or path itself)."""
    d = path
    while d and d not in ("/", HOME):
        if os.path.exists(os.path.join(d, ".git")):
            return d
        d = os.path.dirname(d)
    return path


def in_roots(path: str, roots: list[str]) -> bool:
    return any(path == r or path.startswith(r + "/") for r in roots)


def container_in_scope(c: "Container", roots: list[str], show_all: bool) -> bool:
    """Stacks follow the same rule as servers: shown when their folder is inside a project root.
    Containers with no folder (a plain `docker run`) are always shown."""
    return show_all or not c.workdir or in_roots(os.path.realpath(c.workdir), roots)


def is_launcher(pid: int, procs: dict[int, ProcStat], argv_of=cmdline) -> bool:
    """True for the shell/terminal you launched from. `sh -c` wrappers spawned by npm/pnpm don't count."""
    comm = procs[pid].comm
    if comm not in LAUNCHERS:
        return False
    if comm in SHELLS:
        return "-c" not in argv_of(pid)
    return True


def tree_root(pid: int, procs: dict[int, ProcStat], argv_of=cmdline, cwd_of=cwd) -> int:
    """Climb from a listening process through its wrappers (npm, pnpm, sh -c) to what you launched.

    Stops at a known shell/terminal, and at any parent working outside the server's project, so an
    unrecognised terminal or IDE is never mistaken for part of the server (and never gets killed).
    """
    here = cwd_of(pid)
    anchor = project_dir(here)

    def same_project(p: int) -> bool:
        d = cwd_of(p)
        if anchor in ("/", HOME, ""):
            return d == here
        return d == anchor or d.startswith(anchor + "/")

    root = pid
    while True:
        parent = procs[root].ppid if root in procs else 1
        if (parent <= 1 or parent not in procs or is_launcher(parent, procs, argv_of)
                or not same_project(parent)):
            return root
        root = parent


def descendants(root: int, children: dict[int, list[int]]) -> list[int]:
    out, stack = [], [root]
    while stack:
        p = stack.pop()
        out.append(p)
        stack.extend(children.get(p, ()))
    return out


# ---------------------------------------------------------------- docker

@dataclass
class Container:
    id: str
    name: str
    image: str
    state: str
    status: str
    ports: list[int]
    port_text: str
    compose_project: str = ""
    service: str = ""
    workdir: str = ""
    supabase: bool = False


DOCKER_FORMAT = "\t".join([
    "{{.ID}}", "{{.Names}}", "{{.Image}}", "{{.State}}", "{{.Status}}", "{{.Ports}}",
    '{{.Label "com.docker.compose.project"}}', '{{.Label "com.docker.compose.service"}}',
    '{{.Label "com.docker.compose.project.working_dir"}}', '{{.Label "com.supabase.cli.workdir"}}',
    '{{.Label "com.supabase.cli.project"}}',
])


def parse_docker_ps(text: str) -> list[Container]:
    out = []
    for line in text.splitlines():
        f = line.split("\t")
        if len(f) < 11:
            continue
        cid, name, image, state, status, ports, proj, svc, wd, sb_wd, sb_proj = f[:11]
        out.append(Container(
            id=cid, name=name, image=image, state=state, status=status,
            ports=parse_docker_ports(ports), port_text=ports,
            compose_project=proj or sb_proj, service=svc,
            workdir=wd or sb_wd, supabase=bool(sb_proj or sb_wd),
        ))
    return out


def docker_problem(stderr: str) -> str:
    """Why `docker ps` failed: 'denied' (not in the docker group) or 'down' (daemon not running)."""
    return "denied" if "permission denied" in stderr.lower() else "down"


def list_containers() -> tuple[list[Container], str]:
    """(running containers, docker status). Status is ok, missing (no docker CLI), denied or down."""
    try:
        r = subprocess.run(["docker", "ps", "--no-trunc", "--format", DOCKER_FORMAT],
                           capture_output=True, text=True, timeout=5)
    except FileNotFoundError:
        return [], "missing"
    except (OSError, subprocess.TimeoutExpired):
        return [], "down"
    if r.returncode != 0:
        return [], docker_problem(r.stderr)
    return parse_docker_ps(r.stdout), "ok"


_cgroup_paths: dict[str, str | None] = {}


def cgroup_dir(cid: str) -> str | None:
    if cid not in _cgroup_paths:
        _cgroup_paths[cid] = next((p for p in (
            f"/sys/fs/cgroup/system.slice/docker-{cid}.scope",
            f"/sys/fs/cgroup/docker/{cid}",
        ) if os.path.isdir(p)), None)
    return _cgroup_paths[cid]


def cgroup_usage(cid: str) -> tuple[int, float, int] | None:
    """(cpu usage_usec, memory MiB, first pid) for a container, read straight from its cgroup."""
    d = cgroup_dir(cid)
    if not d:
        return None
    m = re.search(r"^usage_usec (\d+)", read(f"{d}/cpu.stat"), re.M)
    current = int(read(f"{d}/memory.current", "0").strip() or 0)
    inactive = re.search(r"^inactive_file (\d+)", read(f"{d}/memory.stat"), re.M)
    # Same formula as `docker stats` on cgroup v2: page cache that can be dropped doesn't count.
    mem = max(0, current - (int(inactive.group(1)) if inactive else 0)) / 2**20
    pids = read(f"{d}/cgroup.procs").split()
    return (int(m.group(1)) if m else 0), mem, (int(pids[0]) if pids else 0)


# ---------------------------------------------------------------- tree

@dataclass
class Node:
    key: str                 # stable across refreshes; used for selection and expansion
    kind: str                # project | server | process | stack | container
    name: str
    ports: list[int] = field(default_factory=list)
    cpu: float = 0.0         # percent of one core
    mem: float = 0.0         # MiB
    started: float | None = None   # epoch seconds
    pid: int | None = None
    procs: list[tuple[int, int]] = field(default_factory=list)  # (pid, start ticks) to signal
    container: str | None = None   # container id
    path: str = ""
    command: str = ""
    status: str = ""
    image: str = ""
    children: list["Node"] = field(default_factory=list)

    def walk(self):
        yield self
        for c in self.children:
            yield from c.walk()


@dataclass
class ProcInfo:
    pid: int
    start: int
    comm: str
    argv: list[str]
    cpu: float
    mem: float
    started: float
    ports: set[int]


@dataclass
class ServerInfo:
    root: ProcInfo
    cwd: str
    project: str | None     # git root, or None when outside the project roots
    ports: set[int]
    procs: list[ProcInfo]


@dataclass
class ContainerInfo:
    c: Container
    cpu: float
    mem: float
    started: float | None


def _rollup(node: Node) -> Node:
    if node.children:
        for c in node.children:
            _rollup(c)
        node.cpu = sum(c.cpu for c in node.children)
        node.mem = sum(c.mem for c in node.children)
        node.ports = sorted({p for c in node.children for p in c.ports} | set(node.ports))
        starts = [c.started for c in node.children if c.started]
        node.started = min(starts) if starts else node.started
        node.procs = [p for c in node.children for p in c.procs] or node.procs
    return node


def _server_node(s: ServerInfo) -> Node:
    rel = os.path.relpath(s.cwd, s.project) if s.project else ""
    name = friendly_cmd(_listening_argv(s), s.root.comm, cwd=s.cwd)
    if rel and rel != ".":
        name = f"{name}  [{rel}]"
    node = Node(
        key=f"server:{s.root.pid}:{s.root.start}", kind="server", name=name,
        ports=sorted(s.ports), pid=s.root.pid, path=s.cwd,
        command=" ".join(_listening_argv(s)), started=s.root.started,
    )
    if len(s.procs) == 1:
        p = s.procs[0]
        node.cpu, node.mem, node.procs = p.cpu, p.mem, [(p.pid, p.start)]
        return node
    for p in s.procs:
        node.children.append(Node(
            key=f"process:{p.pid}:{p.start}", kind="process", name=friendly_cmd(p.argv, p.comm, cwd=s.cwd),
            ports=sorted(p.ports), cpu=p.cpu, mem=p.mem, started=p.started, pid=p.pid,
            procs=[(p.pid, p.start)], command=" ".join(p.argv) or p.comm, path=s.cwd,
        ))
    return node


def container_label(c: Container) -> str:
    """'supabase_studio_shop' in stack shop -> 'studio'."""
    if c.service:
        return c.service
    name = c.name
    if c.compose_project:
        for sep in ("_", "-"):
            name = name.removesuffix(sep + c.compose_project).removeprefix(c.compose_project + sep)
    if c.supabase:
        name = name.removeprefix("supabase_")
    return name or c.name


def _listening_argv(s: ServerInfo) -> list[str]:
    """Name a server after the process holding the port, not the npm/pnpm wrapper above it."""
    for p in s.procs:
        if p.ports:
            return p.argv or [p.comm]
    return s.root.argv or [s.root.comm]


def _container_node(ci: ContainerInfo) -> Node:
    c = ci.c
    return Node(
        key=f"container:{c.id}", kind="container", name=container_label(c), ports=c.ports,
        cpu=ci.cpu, mem=ci.mem, started=ci.started, container=c.id, status=c.status,
        image=c.image, command=c.name, path=c.workdir,
    )


def build_tree(servers: list[ServerInfo], containers: list[ContainerInfo]) -> list[Node]:
    """Group servers and containers by project (git checkout); anything without one goes top level."""
    top: list[Node] = []
    projects: dict[str, Node] = {}

    def project(path: str) -> Node:
        if path not in projects:
            projects[path] = Node(key=f"project:{path}", kind="project",
                                  name=os.path.basename(path) or path, path=path)
            top.append(projects[path])
        return projects[path]

    for s in servers:
        node = _server_node(s)
        (project(s.project).children if s.project else top).append(node)

    stacks: dict[str, list[ContainerInfo]] = defaultdict(list)
    for ci in containers:
        if ci.c.compose_project:
            stacks[ci.c.compose_project].append(ci)
        elif ci.c.workdir:
            project(project_dir(ci.c.workdir)).children.append(_container_node(ci))
        else:
            top.append(_container_node(ci))

    for name, members in stacks.items():
        tool = "supabase" if any(m.c.supabase for m in members) else "compose"
        workdirs = Counter(m.c.workdir for m in members if m.c.workdir)
        workdir = workdirs.most_common(1)[0][0] if workdirs else ""
        stack = Node(key=f"stack:{name}", kind="stack", name=f"{tool} ({name})", path=workdir,
                     command=name, children=[_container_node(m) for m in members])
        (project(project_dir(workdir)).children if workdir else top).append(stack)

    return [_rollup(n) for n in top]


# ---------------------------------------------------------------- sampling

@dataclass
class System:
    cpu: float          # percent of the whole machine
    mem_used: float     # MiB
    mem_total: float
    docker: str         # ok | missing (not installed) | denied (not in docker group) | down


@dataclass
class Snapshot:
    nodes: list[Node]
    system: System
    taken: float


class Sampler:
    """CPU percentages need two readings; each snapshot is measured against the previous one."""

    def __init__(self, show_all: bool = False, roots: list[str] | None = None):
        self.show_all = show_all
        self.roots = roots or default_roots()
        self.btime = boot_time()
        self._ticks: dict[tuple[int, int], int] = {}
        self._cg: dict[str, int] = {}
        self._sys = system_cpu_ticks()
        self._t = time.monotonic()
        self._prime()

    def _prime(self):
        for pid, st in all_procs().items():
            self._ticks[(pid, st.start)] = st.ticks

    def snapshot(self) -> Snapshot:
        now = time.monotonic()
        dt = max(now - self._t, 1e-3)
        procs = all_procs()
        ports_by_pid = listeners()
        docker, docker_status = list_containers()

        children: dict[int, list[int]] = defaultdict(list)
        for pid, st in procs.items():
            children[st.ppid].append(pid)

        def info(pid: int) -> ProcInfo:
            st = procs[pid]
            prev = self._ticks.get((pid, st.start))
            cpu = (st.ticks - prev) / TICK / dt * 100 if prev is not None else 0.0
            return ProcInfo(pid, st.start, st.comm, cmdline(pid), max(cpu, 0.0), pss_mib(pid),
                            self.btime + st.start / TICK, ports_by_pid.get(pid, set()))

        groups: dict[int, dict] = {}
        for pid, ports in ports_by_pid.items():
            if pid not in procs:
                continue
            here = cwd(pid)
            inside = in_roots(here, self.roots)
            if not inside and not self.show_all:
                continue
            root = tree_root(pid, procs)
            g = groups.setdefault(root, {"cwd": here, "project": project_dir(here) if inside else None,
                                         "ports": set()})
            g["ports"] |= ports

        servers = []
        for root, g in groups.items():
            members = [info(p) for p in descendants(root, children) if p in procs]
            servers.append(ServerInfo(members[0], g["cwd"], g["project"], g["ports"], members))

        conts = []
        cg_now: dict[str, int] = {}
        for c in (c for c in docker if container_in_scope(c, self.roots, self.show_all)):
            usage = cgroup_usage(c.id)
            if usage is None:
                conts.append(ContainerInfo(c, 0.0, 0.0, None))
                continue
            used, mem, pid = usage
            cg_now[c.id] = used
            prev = self._cg.get(c.id)
            cpu = (used - prev) / 1e6 / dt * 100 if prev is not None else 0.0
            st = proc_stat(pid) if pid else None
            conts.append(ContainerInfo(c, max(cpu, 0.0), mem, self.btime + st.start / TICK if st else None))

        busy, total = system_cpu_ticks()
        sys_cpu = (busy - self._sys[0]) / max(total - self._sys[1], 1) * 100
        used, mtotal = meminfo()

        self._ticks = {(pid, st.start): st.ticks for pid, st in procs.items()}
        self._cg = cg_now
        self._sys = (busy, total)
        self._t = now
        return Snapshot(build_tree(servers, conts), System(sys_cpu, used, mtotal, docker_status),
                        time.time())
