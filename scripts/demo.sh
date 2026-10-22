#!/usr/bin/env bash
# Start a set of fake dev servers and a small Compose stack for screenshots, without anyone's real projects.
#
#   scripts/demo.sh up      # start everything under $DEMO (default /tmp/devports-demo)
#   scripts/demo.sh tui     # open devports on just the demo, as if it lived in ~/code
#   scripts/demo.sh down    # stop and remove it all
#
# Servers are tiny node/python stand-ins named like the real tools (vite, astro, next-server,
# uvicorn), started through pnpm/npm wrappers so the process trees look the way they do in real life.
# Needs node, npm, pnpm and docker.
set -euo pipefail

DEMO="${DEVPORTS_DEMO:-/tmp/devports-demo}"
HOME_DIR="$DEMO/home"
CODE="$HOME_DIR/code"
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# A node HTTP server that holds some memory and, optionally, burns a little CPU like a dev server
# rebuilding. Usage: <title> <port> <MiB> <cpu%>
fake_node() {
  cat <<'EOF'
#!/usr/bin/env node
const [title, port, mib, cpu] = [process.env.FAKE_TITLE, +process.env.FAKE_PORT, +process.env.FAKE_MIB, +process.env.FAKE_CPU];
if (title) process.title = title;
const hold = Buffer.alloc(mib * 1024 * 1024, 1);
if (cpu) setInterval(() => { const end = Date.now() + cpu; while (Date.now() < end) {} }, 100);
require("http").createServer((_, res) => res.end("ok " + hold.length)).listen(port, "127.0.0.1");
EOF
}

project() {
  local dir="$CODE/$1"
  mkdir -p "$dir/node_modules/.bin"
  git -C "$dir" init -q 2>/dev/null || true
  echo "$dir"
}

node_app() { # name bin title port mib cpu runner
  local dir
  dir=$(project "$1")
  fake_node >"$dir/node_modules/.bin/$2"
  chmod +x "$dir/node_modules/.bin/$2"
  printf '{"name":"%s","private":true,"scripts":{"dev":"%s --port %s"}}\n' "$1" "$2" "$4" >"$dir/package.json"
  (cd "$dir" && FAKE_TITLE="$3" FAKE_PORT="$4" FAKE_MIB="$5" FAKE_CPU="$6" setsid -f "$7" run dev </dev/null >/dev/null 2>&1)
}

up() {
  mkdir -p "$CODE"
  node_app acme-web vite "" 5173 140 0 pnpm
  node_app storefront next "next-server (v15.5.2)" 3001 420 18 pnpm
  node_app docs-site astro "" 4321 110 0 npm

  local api
  api=$(project billing-api)
  mkdir -p "$api/uvicorn"
  cat >"$api/uvicorn/__main__.py" <<'EOF'
import http.server, sys
port = int(sys.argv[sys.argv.index("--port") + 1])
hold = bytearray(90 * 2**20)
http.server.ThreadingHTTPServer(("127.0.0.1", port), http.server.SimpleHTTPRequestHandler).serve_forever()
EOF
  (cd "$api" && setsid -f python3 -m uvicorn app.main:app --port 8000 --reload </dev/null >/dev/null 2>&1)

  cat >"$CODE/acme-web/compose.yaml" <<'EOF'
services:
  db:
    image: postgres:17-alpine
    environment: { POSTGRES_PASSWORD: demo }
    ports: ["55432:5432"]
  cache:
    image: redis:7-alpine
    ports: ["56379:6379"]
EOF
  docker compose -f "$CODE/acme-web/compose.yaml" -p acme-web up -d --quiet-pull
  echo "demo running under $CODE"
}

down() {
  [[ -f $CODE/acme-web/compose.yaml ]] && docker compose -f "$CODE/acme-web/compose.yaml" -p acme-web down -v
  DEVPORTS_ROOTS="$CODE" "$repo/bin/devports" list --json --quick |
    python3 -c 'import json,sys; [print(p) for n in json.load(sys.stdin)["nodes"] for x in [n, *n["children"]] for p, _ in x["procs"]]' |
    sort -u | xargs -r kill 2>/dev/null || true
  rm -rf "$DEMO"
  echo "demo removed"
}

tui() {
  # HOME is only changed for devports, so paths read ~/code/... and the terminal keeps its theme.
  exec env HOME="$HOME_DIR" DEVPORTS_ROOTS="$CODE" "$repo/bin/devports" "$@"
}

case "${1:-}" in
  up) up ;;
  down) down ;;
  tui) shift && tui "$@" ;;
  *) echo "usage: $0 up|tui|down" >&2 && exit 2 ;;
esac
