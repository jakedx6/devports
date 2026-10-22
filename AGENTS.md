# devports

Curses TUI and CLI listing local dev servers and Docker containers by project, with stop/kill.

> [!NOTE]
> `AGENTS.md` is canonical; `CLAUDE.md` imports it. It isn't a symlink because Omarchy's plugin
> validator rejects symlinks inside a plugin folder. Edit this file only.

## Layout

- `devports/collect.py`: reads `/proc`, `ss`, `docker ps` and container cgroups. `build_tree` is pure
  and holds the grouping rules; `Sampler` keeps the previous reading for CPU deltas.
- `devports/actions.py`: stop / kill / restart / open. All signalling goes through `signal_procs`,
  which checks pid start times and skips devports' own ancestry.
- `devports/tui.py`: curses UI. Only the main thread touches curses; sampling and actions run in
  threads and hand results back through `App.snap` / `App.flash`.
- `bin/devports`: launcher, symlinked into `~/.local/bin` by `install.sh`. It pins `/usr/bin/python3`
  because the Omarchy menu's PATH puts mise shims first.
- `bin/devports-open`: opens the TUI floating via Hyprland's Lua `hl.dsp.exec_cmd` window rules (no
  user config needed), or focuses the open window.
- `manifest.json` + `BarWidget.qml`: the Omarchy shell plugin (id `jakedx6.devports`). The widget only
  renders `devports list --json --quick`; keep logic in Python.
- `install.sh`: links the CLI, writes the `.desktop` entry, asks before editing the Omarchy menu;
  `--uninstall` reverses it.

## Rules

- Standard library only, Python 3.11+. No third-party packages.
- Never widen what a stop can reach without a test: `tree_root` must not climb past the launching
  shell/terminal or out of the project directory.
- Use the terminal palette (`use_default_colors`, ANSI pairs) so the UI follows the Omarchy theme.
- No symlinks in the repo: `omarchy plugin validate` rejects them.
- Contributor-facing docs live in `CONTRIBUTING.md`; keep the two consistent.

## Check

```sh
python3 -m unittest discover -s tests
./bin/devports list -d 2
omarchy plugin validate .
git diff --check
```

To exercise the TUI headless: `tmux -L devports-test new-session -d -x 150 -y 45 ./bin/devports`
then `tmux -L devports-test capture-pane -p`.
