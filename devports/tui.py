"""Curses TUI. Uses the terminal's own palette, so it follows the Omarchy theme."""
from __future__ import annotations

import curses
import locale
import os
import threading
import time

from . import actions
from .collect import Node, Sampler, Snapshot, compact_ports, short_path

ICONS = {"project": "", "server": "", "process": "", "stack": "", "container": ""}
KIND_LABEL = {"project": "project", "server": "server", "process": "process",
              "stack": "docker stack", "container": "container"}
SORTS = ["mem", "cpu", "name", "uptime"]
HINTS = [("↑↓", "move"), ("←→", "fold"), ("x", "stop"), ("X", "kill"), ("r", "restart"),
         ("o", "open"), ("s", "sort"), ("a", "all"), ("q", "quit")]
DETAIL_H = 5
# docker status -> (header warning, empty-list hint). A missing docker CLI is not a problem.
DOCKER_PROBLEMS = {
    "denied": ("docker: permission denied", "Docker: add yourself to the docker group, then log in again."),
    "down": ("docker not running", "Docker isn't running, so containers aren't shown."),
}

ACCENT, GOOD, WARN, BAD, DOCKER = 1, 2, 3, 4, 5


def fmt_mem(mb: float) -> str:
    return f"{mb / 1024:.1f}G" if mb >= 1024 else f"{mb:.0f}M"


def fmt_age(started: float | None, now: float) -> str:
    if not started:
        return "-"
    s = int(max(now - started, 0))
    if s >= 86400:
        return f"{s // 86400}d{s % 86400 // 3600}h"
    if s >= 3600:
        return f"{s // 3600}h{s % 3600 // 60}m"
    return f"{s // 60}m{s % 60:02d}s" if s < 600 else f"{s // 60}m"


def fit(text: str, width: int) -> str:
    if width <= 0:
        return ""
    return text if len(text) <= width else text[:max(width - 1, 0)] + "…"


def heat_cpu(cpu: float) -> int:
    return BAD if cpu >= 50 else WARN if cpu >= 10 else 0


def heat_mem(mem: float) -> int:
    return BAD if mem >= 2048 else WARN if mem >= 800 else 0


class App:
    def __init__(self, scr, show_all: bool, interval: float, roots: list[str] | None = None):
        self.scr = scr
        self.interval = interval
        self.sampler = Sampler(show_all, roots)
        self.snap: Snapshot | None = None
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.expanded: set[str] = set()
        self.seen: set[str] = set()
        self.rows: list[tuple[Node, int]] = []
        self.sel = 0
        self.sel_key: str | None = None
        self.top = 0
        self.sort = 0
        self.message = ("", 0.0)
        self.confirm: tuple[str, object] | None = None
        self.pending: dict[str, tuple[str, float]] = {}
        self.list_y = 2
        self.list_h = 1

    # ------------------------------------------------------------ background work

    def worker(self):
        time.sleep(0.5)  # CPU% needs an interval between readings
        while True:
            try:
                snap = self.sampler.snapshot()
                with self.lock:
                    self.snap = snap
            except Exception as e:  # keep the UI alive; show what broke
                self.flash(f"refresh failed: {e}")
            self.wake.wait(self.interval)
            self.wake.clear()

    def refresh_soon(self, *delays: float):
        def go():
            for d in delays:
                time.sleep(d)
                self.wake.set()
        threading.Thread(target=go, daemon=True).start()

    def run_bg(self, node: Node, label: str, fn):
        self.pending[node.key] = (label, time.time() + 20)

        def go():
            try:
                self.flash(fn())
            except Exception as e:
                self.flash(f"{node.name}: {e}")
            self.refresh_soon(0, 1.5, 4)
        threading.Thread(target=go, daemon=True).start()

    def flash(self, text: str, secs: float = 6):
        self.message = (text, time.time() + secs)

    # ------------------------------------------------------------ model

    def sort_key(self, n: Node):
        s = SORTS[self.sort]
        if s == "cpu":
            return (-n.cpu, -n.mem)
        if s == "name":
            return (n.name.lower(),)
        if s == "uptime":
            return (n.started or float("inf"),)
        return (-n.mem, -n.cpu)

    def flatten(self, nodes: list[Node], depth: int = 0, out=None):
        out = [] if out is None else out
        for n in sorted(nodes, key=self.sort_key):
            if n.key not in self.seen:
                self.seen.add(n.key)
                if n.kind == "project":
                    self.expanded.add(n.key)
            out.append((n, depth))
            if n.children and n.key in self.expanded:
                self.flatten(n.children, depth + 1, out)
        return out

    def current(self) -> Node | None:
        return self.rows[self.sel][0] if 0 <= self.sel < len(self.rows) else None

    def parent_index(self) -> int | None:
        if not self.rows:
            return None
        depth = self.rows[self.sel][1]
        for i in range(self.sel - 1, -1, -1):
            if self.rows[i][1] < depth:
                return i
        return None

    def sync_rows(self):
        with self.lock:
            snap = self.snap
        if not snap:
            return snap
        self.rows = self.flatten(snap.nodes)
        keys = [n.key for n, _ in self.rows]
        if self.sel_key in keys:
            self.sel = keys.index(self.sel_key)
        self.sel = min(max(self.sel, 0), max(len(self.rows) - 1, 0))
        self.sel_key = self.rows[self.sel][0].key if self.rows else None
        live = set(keys)
        now = time.time()
        self.pending = {k: v for k, v in self.pending.items() if k in live and v[1] > now}
        return snap

    def move(self, delta: int):
        if self.rows:
            self.sel = min(max(self.sel + delta, 0), len(self.rows) - 1)
            self.sel_key = self.rows[self.sel][0].key

    # ------------------------------------------------------------ drawing

    def put(self, y: int, x: int, text: str, attr: int = 0):
        h, w = self.scr.getmaxyx()
        if 0 <= y < h and x < w:
            try:
                self.scr.addstr(y, x, text[:max(w - x, 0)], attr)
            except curses.error:
                pass  # writing the bottom-right cell raises after succeeding

    def color(self, pair: int) -> int:
        return curses.color_pair(pair) if pair and curses.has_colors() else 0

    def heat(self, pair: int) -> int:
        # Themes may remap ANSI red/yellow (Hackerman's red is green), so heat is also bold.
        return self.color(pair) | (curses.A_BOLD if pair else 0)

    def bar(self, y: int, x: int, frac: float, width: int, pair: int) -> int:
        filled = round(min(max(frac, 0), 1) * width)
        self.put(y, x, "━" * filled, self.color(pair) | curses.A_BOLD)
        self.put(y, x + filled, "─" * (width - filled), curses.A_DIM)
        return x + width

    def columns(self, w: int) -> list[tuple[str, int]]:
        cols = [("PORTS", 17), ("CPU%", 6), ("MEM", 7), ("UP", 7), ("PID/ID", 12)]
        if w < 100:
            cols.pop()
        if w < 82:
            cols.pop()
        return cols

    def draw_header(self, snap: Snapshot | None, w: int):
        self.put(0, 1, "devports", self.color(ACCENT) | curses.A_BOLD)
        x = 11
        if snap:
            s = snap.system
            self.put(0, x, "cpu ", curses.A_DIM)
            x = self.bar(0, x + 4, s.cpu / 100, 10, heat_cpu(s.cpu) or GOOD)
            self.put(0, x + 1, f"{s.cpu:3.0f}%")
            x += 7
            self.put(0, x, "ram ", curses.A_DIM)
            frac = s.mem_used / s.mem_total if s.mem_total else 0
            x = self.bar(0, x + 4, frac, 10, BAD if frac > .85 else WARN if frac > .7 else GOOD)
            ram = f"{s.mem_used / 1024:.1f}/{s.mem_total / 1024:.0f}G"
            self.put(0, x + 1, ram)
            x += len(ram) + 3
            dev_mem = sum(n.mem for n in snap.nodes)
            dev_cpu = sum(n.cpu for n in snap.nodes)
            self.put(0, x, "dev ", curses.A_DIM)
            self.put(0, x + 4, f"{fmt_mem(dev_mem)} · {dev_cpu:.0f}%", curses.A_BOLD)
            if s.docker in DOCKER_PROBLEMS:
                self.put(0, x + 20, DOCKER_PROBLEMS[s.docker][0], self.color(WARN))
        scope = "all listeners" if self.sampler.show_all else self.roots_label()
        right = f"sort {SORTS[self.sort]} · {scope} · {time.strftime('%H:%M:%S')}"
        self.put(0, max(w - len(right) - 1, x + 30 if snap else 12), right, curses.A_DIM)

    def roots_label(self) -> str:
        return ", ".join(short_path(r) for r in self.sampler.roots)

    def draw_list(self, snap: Snapshot | None, h: int, w: int):
        cols = self.columns(w)
        name_w = w - sum(cw + 1 for _, cw in cols) - 2
        x = 1 + name_w
        self.put(1, 1, "NAME", curses.A_BOLD | curses.A_DIM)
        for title, cw in cols:
            self.put(1, x + 1, title.rjust(cw) if title != "PORTS" else title.ljust(cw), curses.A_BOLD | curses.A_DIM)
            x += cw + 1

        self.list_y, self.list_h = 2, max(h - 3 - DETAIL_H, 1)
        if not snap:
            self.put(self.list_y + 1, 3, "sampling…", curses.A_DIM)
            return
        if not self.rows:
            lines = ["No dev servers or containers running."]
            if not self.sampler.show_all:
                lines += [f"Watching {self.roots_label()}.",
                          "Press a to include everything that's listening, or set DEVPORTS_ROOTS "
                          "(e.g. DEVPORTS_ROOTS=~/code:~/work) to watch other folders."]
            if snap.system.docker in DOCKER_PROBLEMS:
                lines.append(DOCKER_PROBLEMS[snap.system.docker][1])
            for i, line in enumerate(lines):
                self.put(self.list_y + 1 + i, 3, fit(line, w - 4), curses.A_DIM)
            return

        if self.sel < self.top:
            self.top = self.sel
        elif self.sel >= self.top + self.list_h:
            self.top = self.sel - self.list_h + 1
        self.top = max(0, min(self.top, max(len(self.rows) - self.list_h, 0)))

        now = time.time()
        for i, (n, depth) in enumerate(self.rows[self.top:self.top + self.list_h]):
            y = self.list_y + i
            selected = self.top + i == self.sel
            base = curses.A_REVERSE if selected else 0
            if selected:
                self.put(y, 0, " " * w, base)
            arrow = ("▾ " if n.key in self.expanded else "▸ ") if n.children else "  "
            icon_pair = DOCKER if n.kind in ("stack", "container") else ACCENT
            x = 1 + depth * 2
            self.put(y, x, arrow, base | curses.A_DIM)
            self.put(y, x + 2, ICONS[n.kind] + " ", base | self.color(icon_pair))
            name_attr = base | (curses.A_BOLD if n.kind == "project" else 0)
            if n.kind == "process":
                name_attr |= curses.A_DIM
            if n.kind == "container" and "unhealthy" in n.status:
                name_attr |= self.color(BAD)
            label = n.name
            if n.kind in ("project", "stack") and n.children:
                label += f"  ({len(n.children)})"
            self.put(y, x + 4, fit(label, name_w - (x + 4) + 1), name_attr)
            if n.key in self.pending:
                tag = f" {self.pending[n.key][0]} "
                self.put(y, max(1 + name_w - len(tag), x + 4), tag, self.color(WARN) | curses.A_BOLD | base)

            cx = 1 + name_w
            for title, cw in cols:
                attr = base
                if title == "PORTS":
                    text = fit(compact_ports(n.ports), cw).ljust(cw)
                    attr |= self.color(ACCENT) if n.kind != "process" else curses.A_DIM
                elif title == "CPU%":
                    text = f"{n.cpu:.1f}".rjust(cw)
                    attr |= self.heat(heat_cpu(n.cpu))
                elif title == "MEM":
                    text = fmt_mem(n.mem).rjust(cw)
                    attr |= self.heat(heat_mem(n.mem))
                elif title == "UP":
                    text = fmt_age(n.started, now).rjust(cw)
                    attr |= curses.A_DIM
                else:
                    ident = n.container[:12] if n.container else str(n.pid or "")
                    text = ident.rjust(cw)
                    attr |= curses.A_DIM
                self.put(y, cx + 1, text, attr)
                cx += cw + 1

        if len(self.rows) > self.list_h:
            pos = f" {self.sel + 1}/{len(self.rows)} "
            self.put(self.list_y + self.list_h - 1, w - len(pos) - 1, pos, curses.A_DIM | curses.A_REVERSE)

    def detail_lines(self, n: Node) -> list[tuple[str, str]]:
        lines: list[tuple[str, str]] = []
        if n.kind == "project":
            servers = sum(1 for c in n.walk() if c.kind == "server")
            conts = sum(1 for c in n.walk() if c.kind == "container")
            lines.append(("path", short_path(n.path)))
            parts = [f"{servers} server{'s' if servers != 1 else ''}" if servers else "",
                     f"{conts} container{'s' if conts != 1 else ''}" if conts else ""]
            lines.append(("runs", " · ".join(p for p in parts if p)))
        elif n.kind in ("server", "process"):
            lines.append(("cwd", short_path(n.path) if n.path else "-"))
            lines.append(("cmd", n.command))
            lines.append(("pid", f"{n.pid}" + (f" · {len(n.procs)} processes in tree" if len(n.procs) > 1 else "")))
        elif n.kind == "stack":
            lines.append(("path", short_path(n.path) if n.path else "-"))
            health = [c.status for c in n.children]
            bad = sum("unhealthy" in s for s in health)
            lines.append(("stack", f"compose project {n.command} · {len(n.children)} containers"
                          + (f" · {bad} unhealthy" if bad else "")))
        elif n.kind == "container":
            lines.append(("image", n.image))
            lines.append(("status", n.status))
            lines.append(("name", f"{n.command} · {n.container[:12] if n.container else ''}"))
        if n.ports:
            urls = "  ".join(f"localhost:{p}" for p in n.ports[:6]) + ("  …" if len(n.ports) > 6 else "")
            lines.append(("open", urls))
        return lines

    def draw_detail(self, h: int, w: int):
        y0 = h - 1 - DETAIL_H
        self.put(y0, 0, "─" * w, curses.A_DIM)
        n = self.current()
        if not n:
            return
        self.put(y0, 2, f" {KIND_LABEL[n.kind]}: {n.name} ", self.color(ACCENT) | curses.A_BOLD)
        for i, (label, text) in enumerate(self.detail_lines(n)[:DETAIL_H - 1]):
            self.put(y0 + 1 + i, 2, label.rjust(6), curses.A_DIM)
            self.put(y0 + 1 + i, 10, fit(text, w - 11))

    def draw_footer(self, h: int, w: int):
        y = h - 1
        if self.confirm:
            self.put(y, 0, " " * w, self.color(WARN) | curses.A_REVERSE)
            self.put(y, 1, fit(self.confirm[0], w - 2), self.color(WARN) | curses.A_REVERSE | curses.A_BOLD)
            return
        text, until = self.message
        if text and time.time() < until:
            self.put(y, 1, fit(text, w - 2), self.color(ACCENT))
            return
        x = 1
        for key, label in HINTS:
            if x + len(key) + len(label) + 3 > w:
                break
            self.put(y, x, key, curses.A_BOLD | self.color(ACCENT))
            self.put(y, x + len(key) + 1, label, curses.A_DIM)
            x += len(key) + len(label) + 3

    def draw(self):
        snap = self.sync_rows()
        h, w = self.scr.getmaxyx()
        self.scr.erase()
        if h < 10 or w < 50:
            self.put(0, 0, "devports: window too small")
        else:
            self.draw_header(snap, w)
            self.draw_list(snap, h, w)
            self.draw_detail(h, w)
            self.draw_footer(h, w)
        self.scr.noutrefresh()
        curses.doupdate()

    # ------------------------------------------------------------ input

    def ask(self, prompt: str, fn):
        self.confirm = (prompt, fn)

    def stop_selected(self, force: bool):
        n = self.current()
        if not n:
            return
        what = actions.describe(n)
        if what == "nothing":
            self.flash("Nothing here to stop.")
            return
        verb = "Force kill" if force else "Stop"
        label = "killing…" if force else "stopping…"
        self.ask(f"{verb} {n.name} ({what})?  y / n",
                 lambda: self.run_bg(n, label, lambda: actions.stop(n, force)))

    def restart_selected(self):
        n = self.current()
        if not n:
            return
        _, containers = actions.targets(n)
        if not containers:
            self.flash("Only containers can be restarted. Processes don't record how they were started.")
            return
        self.run_bg(n, "restarting…", lambda: actions.restart(n))

    def open_selected(self):
        n = self.current()
        if not n or not n.ports:
            self.flash("No port to open here.")
            return
        actions.open_url(n.ports[0])
        others = compact_ports(n.ports[1:])
        self.flash(f"Opened http://localhost:{n.ports[0]}" + (f". Select a child row for {others}." if others else ""))

    def toggle(self, n: Node | None, expand: bool | None = None):
        if not n or not n.children:
            return
        want = (n.key not in self.expanded) if expand is None else expand
        (self.expanded.add if want else self.expanded.discard)(n.key)

    def on_mouse(self):
        try:
            _, mx, my, _, bstate = curses.getmouse()
        except curses.error:
            return
        if bstate & getattr(curses, "BUTTON4_PRESSED", 0):
            self.move(-3)
        elif bstate & getattr(curses, "BUTTON5_PRESSED", 0):
            self.move(3)
        elif bstate & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
            idx = self.top + my - self.list_y
            if self.list_y <= my < self.list_y + self.list_h and 0 <= idx < len(self.rows):
                if idx == self.sel:
                    self.toggle(self.rows[idx][0])
                self.sel = idx
                self.sel_key = self.rows[idx][0].key

    def handle(self, ch: int) -> bool:
        """Returns False to quit."""
        if self.confirm:
            prompt, fn = self.confirm
            self.confirm = None
            if ch in (ord("y"), ord("Y")):
                fn()
            else:
                self.flash("Cancelled.", 2)
            return True
        if ch in (ord("q"), 27):
            return False
        if ch in (curses.KEY_UP, ord("k")):
            self.move(-1)
        elif ch in (curses.KEY_DOWN, ord("j")):
            self.move(1)
        elif ch == curses.KEY_PPAGE:
            self.move(-self.list_h)
        elif ch == curses.KEY_NPAGE:
            self.move(self.list_h)
        elif ch in (curses.KEY_HOME, ord("g")):
            self.move(-len(self.rows))
        elif ch in (curses.KEY_END, ord("G")):
            self.move(len(self.rows))
        elif ch in (curses.KEY_RIGHT, ord("l")):
            n = self.current()
            if n and n.children and n.key not in self.expanded:
                self.toggle(n, True)
            elif n and n.children:
                self.move(1)
        elif ch in (curses.KEY_LEFT, ord("h")):
            n = self.current()
            if n and n.key in self.expanded:
                self.toggle(n, False)
            elif (p := self.parent_index()) is not None:
                self.sel = p
                self.sel_key = self.rows[p][0].key
        elif ch in (10, 13, curses.KEY_ENTER, ord(" ")):
            self.toggle(self.current())
        elif ch == ord("x"):
            self.stop_selected(False)
        elif ch == ord("X"):
            self.stop_selected(True)
        elif ch == ord("r"):
            self.restart_selected()
        elif ch == ord("o"):
            self.open_selected()
        elif ch == ord("s"):
            self.sort = (self.sort + 1) % len(SORTS)
        elif ch == ord("a"):
            self.sampler.show_all = not self.sampler.show_all
            self.flash("Showing every listening process." if self.sampler.show_all
                       else "Showing projects only.", 3)
            self.wake.set()
        elif ch in (ord("R"), curses.KEY_F5):
            self.wake.set()
        elif ch == curses.KEY_MOUSE:
            self.on_mouse()
        return True

    def run(self):
        curses.curs_set(0)
        if curses.has_colors():
            curses.start_color()
            curses.use_default_colors()
            for pair, fg in ((ACCENT, curses.COLOR_CYAN), (GOOD, curses.COLOR_GREEN), (WARN, curses.COLOR_YELLOW),
                             (BAD, curses.COLOR_RED), (DOCKER, curses.COLOR_BLUE)):
                curses.init_pair(pair, fg, -1)
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
        curses.mouseinterval(0)
        self.scr.keypad(True)
        self.scr.timeout(250)
        threading.Thread(target=self.worker, daemon=True).start()
        while True:
            self.draw()
            ch = self.scr.getch()
            if ch == -1 or ch == curses.KEY_RESIZE:
                continue
            if not self.handle(ch):
                return


def run(show_all: bool = False, interval: float = 2.0, roots: list[str] | None = None):
    os.environ.setdefault("ESCDELAY", "25")
    locale.setlocale(locale.LC_ALL, "")
    curses.wrapper(lambda scr: App(scr, show_all, interval, roots).run())
