#!/usr/bin/env bash
# Put devports on your PATH and in the app launcher, and offer to add it to the Omarchy menu.
# Works from a git clone or from the plugin folder (~/.config/omarchy/plugins/jakedx6.devports).
# Safe to re-run. `./install.sh --uninstall` removes everything it added.
set -euo pipefail

repo="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bin="$HOME/.local/bin"
desktop="$HOME/.local/share/applications/devports.desktop"
menu="$HOME/.config/omarchy/extensions/omarchy-menu.jsonc"
menu_id='"trigger.devports"'
menu_entry='  "trigger.devports": {"icon":"","label":"Dev Servers","aliases":["devports","servers","ports"],"description":"Running dev servers and containers","action":"\"$HOME/.local/bin/devports-open\""},'

yes=0
for arg in "$@"; do
  case $arg in
    -y | --yes) yes=1 ;;
    --uninstall)
      for f in devports devports-open; do
        if [[ -L $bin/$f && $(readlink "$bin/$f") == "$repo/bin/$f" ]]; then rm "$bin/$f" && echo "removed $bin/$f"; fi
      done
      [[ -f $desktop ]] && rm "$desktop" && echo "removed $desktop"
      if [[ -f $menu ]] && grep -q "$menu_id" "$menu"; then
        cp "$menu" "$menu.bak.$(date +%s)"
        grep -v "$menu_id" "$menu" >"$menu.tmp" && mv "$menu.tmp" "$menu"
        echo "removed the Dev Servers entry from $menu (backup saved next to it)"
      fi
      exit 0
      ;;
    *) echo "usage: $0 [--yes] [--uninstall]" >&2 && exit 2 ;;
  esac
done

mkdir -p "$bin" "$(dirname "$desktop")"
for f in devports devports-open; do
  ln -sfn "$repo/bin/$f" "$bin/$f"
  echo "linked $bin/$f"
done
case ":$PATH:" in *":$bin:"*) ;; *) echo "note: $bin isn't on your PATH, so type the full path or add it" ;; esac

# Desktop entry so the app launcher (Super+Space) lists it.
cat >"$desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Dev Servers
Comment=Running dev servers and containers by project
Exec=$bin/devports-open
Icon=network-server
Terminal=false
Categories=Development;
Keywords=devports;servers;ports;docker;containers;
EOF
echo "wrote $desktop"

# Omarchy menu entry (Trigger → Dev Servers). Only with consent: it edits your config.
if [[ -f $menu ]] && ! grep -q "$menu_id" "$menu"; then
  if ((!yes)) && [[ -t 0 ]]; then
    read -r -p "Add Dev Servers to the Omarchy menu (Trigger → Dev Servers)? [y/N] " answer
    [[ $answer == [yY]* ]] && yes=1
  fi
  if ((yes)); then
    cp "$menu" "$menu.bak.$(date +%s)"
    # Insert before the closing brace; JSONC allows the trailing comma.
    last=$(grep -n '^}' "$menu" | tail -n1 | cut -d: -f1)
    { head -n $((last - 1)) "$menu"; printf '%s\n' "$menu_entry"; tail -n +"$last" "$menu"; } >"$menu.tmp" && mv "$menu.tmp" "$menu"
    echo "added Dev Servers to $menu (backup saved next to it)"
  else
    echo "skipped the Omarchy menu entry; re-run with --yes to add it"
  fi
fi
