import os
import tempfile
import unittest

from devports import actions
from devports.collect import (
    HOME, Container, ContainerInfo, ProcInfo, ProcStat, ServerInfo, build_tree, compact_ports,
    container_in_scope, container_label, default_roots, docker_problem, friendly_cmd, parse_docker_ports, parse_docker_ps,
    parse_ss, tree_root,
)


class ParseSs(unittest.TestCase):
    def test_process_names_with_spaces(self):
        text = (
            'LISTEN 0 511 *:3000 *:* users:(("next-server (v1",pid=101,fd=22))\n'
            'LISTEN 0 511 [::1]:4200 [::]:* users:(("ng serve dashbo",pid=202,fd=37))\n'
            'LISTEN 0 4096 0.0.0.0:54322 0.0.0.0:*\n'
            'LISTEN 0 511 [::]:3000 [::]:* users:(("next-server (v1",pid=101,fd=23))\n'
        )
        self.assertEqual(parse_ss(text), {101: {3000}, 202: {4200}})


class Ports(unittest.TestCase):
    def test_compact(self):
        self.assertEqual(compact_ports([54323, 3000, 54321, 54322, 8080]), "3000,8080,54321-54323")
        self.assertEqual(compact_ports([]), "")

    def test_docker_ports(self):
        text = ("1110/tcp, 0.0.0.0:54325->1025/tcp, [::]:54325->1025/tcp, "
                "0.0.0.0:54324->8025/tcp, 127.0.0.1:9000-9001->9000-9001/tcp")
        self.assertEqual(parse_docker_ports(text), [9000, 9001, 54324, 54325])
        self.assertEqual(parse_docker_ports("8080/tcp"), [])


class FriendlyCmd(unittest.TestCase):
    def test_drops_interpreter_and_flags(self):
        argv = ["/x/node", "--require", "/x/preflight.cjs", "--import", "file:///x/loader.mjs",
                "packages/server/src/index.ts"]
        self.assertEqual(friendly_cmd(argv), "packages/server/src/index.ts")
        self.assertEqual(friendly_cmd(["/x/node", "/p/node_modules/.bin/vite", "--port", "5173"], cwd="/p"),
                         "vite --port 5173")
        self.assertEqual(friendly_cmd(["python3", "-m", "uvicorn", "app:main"]), "uvicorn app:main")

    def test_keeps_package_managers(self):
        self.assertEqual(friendly_cmd(["node", "/home/x/.local/pnpm/bin/pnpm.cjs", "dev"]), "pnpm.cjs dev")
        self.assertEqual(friendly_cmd(["pnpm", "dev"]), "pnpm dev")

    def test_rewritten_titles(self):
        self.assertEqual(friendly_cmd(["next-server (v16.3.8)"]), "next-server (v16.3.8)")
        self.assertEqual(friendly_cmd(["/usr/lib/chromium/chromium --type=renderer"]), "chromium --type=renderer")

    def test_empty(self):
        self.assertEqual(friendly_cmd([], "kworker"), "kworker")


class TreeRoot(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        os.mkdir(os.path.join(self.proj, ".git"))
        # terminal(10) > zsh(11) > pnpm(12) > sh -c(13) > node(14) listening
        self.procs = {
            10: ProcStat("weirdterm", 1, 0, 0),
            11: ProcStat("zsh", 10, 0, 0),
            12: ProcStat("node-MainThread", 11, 0, 0),
            13: ProcStat("sh", 12, 0, 0),
            14: ProcStat("next-server (v1", 13, 0, 0),
        }
        self.argv = {11: ["-zsh"], 13: ["sh", "-c", "next dev"]}
        self.cwds = {10: HOME, 11: self.proj, 12: self.proj, 13: self.proj, 14: self.proj}

    def root(self, pid):
        return tree_root(pid, self.procs, self.argv.get, self.cwds.get)

    def test_climbs_wrappers_stops_at_interactive_shell(self):
        self.assertEqual(self.root(14), 12)

    def test_never_climbs_out_of_project(self):
        # An unrecognised shell/terminal working elsewhere is not part of the server.
        self.procs[11] = ProcStat("xonsh", 10, 0, 0)
        self.cwds[11] = HOME
        self.assertEqual(self.root(14), 12)

    def test_stops_at_editor_running_a_task(self):
        # nvim (in the project) > sh -c > node: stopping the server must not take the editor down.
        self.procs[12] = ProcStat("nvim", 11, 0, 0)
        self.argv[11] = ["-zsh"]
        self.assertEqual(self.root(14), 13)

    def test_outside_project_only_same_cwd(self):
        self.cwds = {10: "/", 11: "/", 12: "/", 13: "/srv", 14: "/srv"}
        self.assertEqual(self.root(14), 13)


def proc(pid, ports=(), mem=10.0, argv=None):
    return ProcInfo(pid, pid * 7, "node", argv or ["node", "server.js"], 1.0, mem, 1000.0, set(ports))


def container(cid, name, project="", workdir="", ports=(), supabase=False, service=""):
    c = Container(cid, name, "img", "running", "Up 1 hour", list(ports), "", project, service, workdir, supabase)
    return ContainerInfo(c, 2.0, 50.0, 500.0)


class BuildTree(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp()
        os.mkdir(os.path.join(self.proj, ".git"))

    def test_groups_servers_and_stacks_by_project(self):
        server = ServerInfo(proc(5), self.proj, self.proj, {3000}, [proc(5), proc(6, [3000], mem=200)])
        stack = [container(f"c{i}", f"supabase_{s}_crm", "crm", self.proj, ports, True)
                 for i, (s, ports) in enumerate([("db", [54322]), ("kong", [54321]), ("rest", [])])]
        stray = container("z", "redis", ports=[6379])
        nodes = build_tree([server], stack + [stray])

        self.assertEqual([n.kind for n in nodes], ["project", "container"])
        project = nodes[0]
        self.assertEqual(sorted(c.kind for c in project.children), ["server", "stack"])
        self.assertEqual(project.ports, [3000, 54321, 54322])
        self.assertAlmostEqual(project.mem, 10 + 200 + 150)
        stack_node = next(c for c in project.children if c.kind == "stack")
        self.assertEqual(stack_node.name, "supabase (crm)")
        self.assertEqual(sorted(c.name for c in stack_node.children), ["db", "kong", "rest"])
        self.assertEqual(stack_node.started, 500.0)

        procs, conts = actions.targets(project)
        self.assertEqual(sorted(procs), [(5, 35), (6, 42)])
        self.assertEqual(conts, ["c0", "c1", "c2"])
        self.assertEqual(actions.describe(project), "2 processes + 3 containers")

    def test_single_process_server_is_a_leaf(self):
        server = ServerInfo(proc(5), self.proj, self.proj, {8000}, [proc(5, [8000])])
        node = build_tree([server], [])[0].children[0]
        self.assertEqual(node.children, [])
        self.assertEqual(node.procs, [(5, 35)])

    def test_server_outside_roots_is_top_level(self):
        server = ServerInfo(proc(5), "/", None, {11434}, [proc(5, [11434], argv=["/usr/bin/ollama", "serve"])])
        nodes = build_tree([server], [])
        self.assertEqual((nodes[0].kind, nodes[0].name), ("server", "ollama serve"))

    def test_server_in_subdirectory_is_labelled(self):
        sub = os.path.join(self.proj, "packages", "web")
        server = ServerInfo(proc(5), sub, self.proj, {5173}, [proc(5, [5173], argv=["vite"])])
        self.assertEqual(build_tree([server], [])[0].children[0].name, "vite  [packages/web]")


class Roots(unittest.TestCase):
    def test_env_wins_and_keeps_missing_dirs(self):
        self.assertEqual(default_roots("/nonexistent/a::/nonexistent/b:/nonexistent/a"),
                         ["/nonexistent/a", "/nonexistent/b"])

    def test_defaults_are_existing_common_folders(self):
        home = tempfile.mkdtemp()
        os.mkdir(os.path.join(home, "code"))
        os.mkdir(os.path.join(home, "src"))
        old = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            self.assertEqual(default_roots(""), [os.path.join(home, "code"), os.path.join(home, "src")])
        finally:
            os.environ["HOME"] = old

    def test_falls_back_to_home(self):
        home = tempfile.mkdtemp()
        old = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            self.assertEqual(default_roots(""), [os.path.realpath(home)])
        finally:
            os.environ["HOME"] = old


class Docker(unittest.TestCase):
    def test_problem(self):
        self.assertEqual(docker_problem("permission denied while trying to connect to the Docker daemon socket"),
                         "denied")
        self.assertEqual(docker_problem("Cannot connect to the Docker daemon. Is the docker daemon running?"),
                         "down")

    def test_parse_ps_and_labels(self):
        line = "\t".join(["abc123", "supabase_studio_crm", "studio:1", "running", "Up 2 hours (healthy)",
                          "0.0.0.0:54323->3000/tcp", "crm", "", "", "/home/x/crm", "crm"])
        [c] = parse_docker_ps(line + "\nshort\tline\n")
        self.assertEqual((c.compose_project, c.workdir, c.supabase, c.ports), ("crm", "/home/x/crm", True, [54323]))
        self.assertEqual(container_label(c), "studio")

    def test_scope_follows_project_roots(self):
        inside = container("a", "web-db-1", "web", workdir="/r/web").c
        outside = container("b", "x-db-1", "x", workdir="/elsewhere/x").c
        loose = container("c", "redis").c
        self.assertEqual([container_in_scope(c, ["/r"], False) for c in (inside, outside, loose)],
                         [True, False, True])
        self.assertTrue(container_in_scope(outside, ["/r"], True))

    def test_label_prefers_compose_service(self):
        c = container("a", "myapp-web-1", "myapp", service="web").c
        self.assertEqual(container_label(c), "web")


class SignalSafety(unittest.TestCase):
    def test_skips_reused_pids_and_self(self):
        import subprocess
        import sys
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        try:
            from devports.collect import proc_stat
            start = proc_stat(child.pid).start
            sent, _ = actions.signal_procs([(child.pid, start + 1), (os.getpid(), proc_stat(os.getpid()).start)],
                                           force=True)
            self.assertEqual(sent, 0)
            self.assertIsNone(child.poll())
            sent, _ = actions.signal_procs([(child.pid, start)], force=False)
            self.assertEqual(sent, 1)
            self.assertEqual(child.wait(5), -15)
        finally:
            child.kill()


if __name__ == "__main__":
    unittest.main()
