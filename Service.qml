import QtQuick
import Quickshell
import Quickshell.Io

// Headless collector for the stock omarchy.agents panel.
// Writes ~/.local/state/omarchy/agents/usage/grok.json; the built-in
// AI button already watches that directory and draws whatever appears.
Item {
  id: root

  property var manifest: null
  property var shell: null

  // Omarchy strips __sourceDir from third-party manifests, so a collector
  // path built from that property never runs. Resolve collect.py from this
  // QML file instead.
  readonly property string collector: Qt.resolvedUrl("collect.py").toString().replace(/^file:\/\//, "")
  readonly property string home: Quickshell.env("HOME") || ""
  readonly property string stateHome: Quickshell.env("XDG_STATE_HOME") || (home + "/.local/state")
  readonly property string fireworksRecord: stateHome + "/omarchy/agents/usage/fireworks.json"

  function collect(force) {
    if (collector === "" || collectProcess.running) return
    var cmd = ["python3", collector, "--write"]
    if (force === true) cmd.push("--force")
    collectProcess.command = cmd
    collectProcess.running = true
  }

  function clearRecord() {
    if (collector === "") return
    // A shell restart destroys this service while shell.json still enables
    // it. --clear would delete grok.json before the new shell's only startup
    // scan, and the agents icon then stays hidden. Delete only when the
    // plugin was actually turned off.
    clearProcess.command = ["python3", collector, "--clear-if-disabled"]
    clearProcess.running = true
  }

  Timer {
    interval: 300000
    running: true
    repeat: true
    triggeredOnStart: true
    onTriggered: root.collect(false)
  }

  // Stock panel refresh rewrites bundled collectors' records, not grok.json.
  // Watch fireworks.json so Grok updates when the user hits r in the panel.
  FileView {
    path: root.fireworksRecord
    watchChanges: true
    printErrors: false
    onFileChanged: root.collect(false)
  }

  Process {
    id: collectProcess
    running: false
    stderr: StdioCollector {
      waitForEnd: true
      onStreamFinished: if (text.trim() !== "") console.warn("grok-usage", text.trim())
    }
  }

  Process {
    id: clearProcess
    running: false
  }

  Component.onDestruction: root.clearRecord()
}
