import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

// How many projects have dev servers or containers running. Hover lists them; click opens the TUI.
// All the work is done by the bundled CLI (`devports list --json --quick`); this only renders it.
BarWidget {
  id: root
  moduleName: "jakedx6.devports"

  readonly property string devports: localPath(Qt.resolvedUrl("bin/devports"))
  readonly property string opener: localPath(Qt.resolvedUrl("bin/devports-open"))
  readonly property int refreshMs: Math.max(2, Number(setting("refreshIntervalSec", 10))) * 1000
  readonly property bool hideWhenIdle: setting("hideWhenIdle", false) === true
  readonly property bool tiled: String(setting("windowMode", "Floating")) === "Tiled"
  // The shell doesn't see variables exported in your shell profile, so folders can be set here too.
  readonly property string folders: String(setting("projectFolders", "")).trim()
  readonly property var envPrefix: folders ? ["env", "DEVPORTS_ROOTS=" + folders] : []

  property var nodes: []
  property string dockerStatus: "ok"
  property bool failed: false

  visible: !(hideWhenIdle && nodes.length === 0 && !failed)
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function localPath(url) {
    var value = String(url || "")
    return value.indexOf("file://") === 0 ? decodeURIComponent(value.substring(7)) : value
  }

  function fmtMem(mb) {
    return mb >= 1024 ? (mb / 1024).toFixed(1) + "G" : Math.round(mb) + "M"
  }

  // [3000, 54321, 54322, 54323] -> "3000,54321-54323", same as the TUI.
  function compactPorts(ports) {
    var ps = ports.slice().sort(function(a, b) { return a - b })
    var out = []
    for (var i = 0; i < ps.length; i++) {
      var j = i
      while (j + 1 < ps.length && ps[j + 1] === ps[j] + 1) j++
      out.push(i === j ? String(ps[i]) : ps[i] + "-" + ps[j])
      i = j
    }
    return out.join(",")
  }

  function tooltip() {
    if (failed) return "devports couldn't read your dev servers.\nClick to open the TUI."
    var lines = []
    if (nodes.length === 0) {
      lines.push("No dev servers or containers running")
    } else {
      var sorted = nodes.slice().sort(function(a, b) { return b.mem - a.mem })
      var total = 0
      for (var i = 0; i < sorted.length; i++) {
        var n = sorted[i]
        total += n.mem
        if (i < 10) lines.push(n.name + " · " + fmtMem(n.mem) + (n.ports.length ? " · :" + compactPorts(n.ports) : ""))
      }
      if (sorted.length > 10) lines.push("… and " + (sorted.length - 10) + " more")
      lines.push("", fmtMem(total) + " in use · click to manage")
    }
    if (dockerStatus === "denied") lines.push("Docker: permission denied (join the docker group)")
    else if (dockerStatus === "down") lines.push("Docker isn't running")
    return lines.join("\n")
  }

  function refresh() {
    if (!listProc.running) listProc.running = true
  }

  function open() {
    Quickshell.execDetached(envPrefix.concat(tiled ? [opener, "--tile"] : [opener]))
    refreshAfterOpen.remaining = 20
    refreshAfterOpen.restart()
  }

  IpcHandler {
    target: "jakedx6.devports"

    function refresh(): void {
      root.broadcast("refresh")
    }

    function open(): void {
      root.open()
    }
  }

  Process {
    id: listProc
    command: root.envPrefix.concat([root.devports, "list", "--json", "--quick"])
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: {
        try {
          var snap = JSON.parse(String(text || ""))
          root.nodes = snap.nodes || []
          root.dockerStatus = snap.system ? String(snap.system.docker) : "ok"
          root.failed = false
        } catch (e) {
          root.failed = true
        }
      }
    }
    onExited: function(exitCode) {
      if (exitCode !== 0) root.failed = true
    }
  }

  Timer {
    interval: root.refreshMs
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.refresh()
  }

  // Pick up servers stopped from the TUI without waiting a full interval.
  Timer {
    id: refreshAfterOpen
    interval: 3000
    repeat: true
    property int remaining: 20
    onTriggered: {
      root.refresh()
      if (--remaining <= 0) stop()
    }
  }

  WidgetButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: "" + (root.nodes.length ? " " + root.nodes.length : "")
    dimmed: root.nodes.length === 0
    tooltipText: root.tooltip()
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.RightButton) root.refresh()
      else root.open()
    }
  }
}
