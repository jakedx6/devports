# Contributing to devports

Thanks for helping. devports is small on purpose, so please open an issue before starting anything
large.

## Reporting a bug

Include:

- what you ran and what you expected
- `omarchy version`
- the output of `devports list --json -a` (or `-d 2` without `--json`) for the case that's wrong,
  with anything private removed

Grouping and naming bugs ("this server shows up under the wrong project", "this name is
unreadable") are the most useful kind. The JSON is usually enough to turn them into a test.

## Layout

| Path | What it is |
| --- | --- |
| `devports/collect.py` | Reads `/proc`, `ss`, `docker ps` and container cgroups. `build_tree` is pure and holds the grouping rules. |
| `devports/actions.py` | Stop, kill, restart, open. All signalling goes through `signal_procs`. |
| `devports/tui.py` | The curses UI. |
| `devports/__main__.py` | CLI: the TUI by default, plus `list` and `kill`. |
| `BarWidget.qml`, `manifest.json` | The Omarchy bar widget. It only renders `devports list --json --quick`. |
| `bin/devports` | Launcher. It pins `/usr/bin/python3` because the Omarchy menu's PATH puts mise shims first. |
| `bin/devports-open` | Opens the TUI in a floating window, or focuses the one that's already open. |
| `install.sh` | Links the CLI, writes the launcher entry, and offers to add the Omarchy menu entry. |

## Rules

- **Standard library only**, Python 3.11+. No third-party packages, so a fresh Omarchy install can
  run it as is.
- **Never widen what a stop can reach without a test.** `tree_root` must not climb past the shell,
  terminal, editor or agent that launched a server, or out of its project directory. If you add a
  process name to `LAUNCHERS`, add a `TreeRoot` test for it.
- **Use the terminal palette** (`use_default_colors`, ANSI pairs) so the UI follows the active theme.
  Don't hard-code colours.
- **No symlinks in the repo.** Omarchy's plugin validator rejects them, so the plugin would fail to
  install.
- Keep the widget thin. Logic belongs in Python, where it's tested.

## Check your change

```sh
python3 -m unittest discover -s tests
./bin/devports list -d 2
omarchy plugin validate .
git diff --check
```

To try the TUI headless:

```sh
tmux -L devports-test new-session -d -x 150 -y 45 ./bin/devports
tmux -L devports-test capture-pane -p
tmux -L devports-test kill-server
```

To try the widget from your checkout, link it in place of an installed copy:

```sh
ln -sfn "$PWD" ~/.config/omarchy/plugins/jakedx6.devports
omarchy-shell shell rescanPlugins
omarchy plugin enable jakedx6.devports right
```

## Pull requests

- One change per PR, with a short imperative title (`Group Turborepo workspaces by package`).
- Say what was wrong, what happens now, and how you checked it.
- Include a screenshot for anything visible in the TUI or the bar.
- Add an entry under **Unreleased** in `CHANGELOG.md`.
