# Changelog

## Unreleased

## 0.1.0 (2026-10-22)

First public release.

- Terminal UI listing dev servers and Docker containers grouped by git checkout, with CPU, PSS
  memory, uptime and ports. Stop, kill, restart containers, open in the browser.
- `devports list [--json]` and `devports kill PORT` for scripts and coding agents.
- Omarchy bar widget: a count of projects with something running, a hover summary, and a click to
  open the TUI floating.
- `install.sh` links the CLI, adds a launcher entry, and offers to add an Omarchy menu entry.
  `--uninstall` removes them.
- Project folders are auto-detected from common locations (`~/Projects`, `~/code`, `~/src`, …), with
  `DEVPORTS_ROOTS`, `--root` and the widget's **Project folders** setting to override. Compose and
  Supabase stacks follow the same folders as servers; `a` shows everything.
