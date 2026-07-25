#!/usr/bin/env bash
# Install the collector + dashboard as macOS launchd services so they run
# INDEPENDENTLY of any terminal/Claude session — auto-start at login, auto-restart
# on crash, survive reboot. Run once:  bash ops/install_services.sh
set -euo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
PY="$REPO/venv/bin/python"
LA="$HOME/Library/LaunchAgents"
mkdir -p "$LA" "$REPO/runtime/logs"

gen() {                     # gen <label> <module-args...>
  local label="$1"; shift
  local args=""
  for a in "$PY" "$@"; do args+="    <string>$a</string>"$'\n'; done
  cat > "$LA/$label.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$label</string>
  <key>ProgramArguments</key><array>
$args  </array>
  <key>EnvironmentVariables</key><dict><key>PYTHONPATH</key><string>$REPO/src</string></dict>
  <key>WorkingDirectory</key><string>$REPO</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>$REPO/runtime/logs/$label.out</string>
  <key>StandardErrorPath</key><string>$REPO/runtime/logs/$label.err</string>
</dict></plist>
PLIST
  launchctl unload "$LA/$label.plist" 2>/dev/null || true
  launchctl load -w "$LA/$label.plist"
  echo "loaded $label"
}

# stop any session-scoped instances so launchd owns the single lock/port
pkill -f "collector.py run" 2>/dev/null || true
pkill -f "collector_dashboard.py serve" 2>/dev/null || true
sleep 1; rm -f "$REPO/runtime/collector.lock"

gen com.stockpredictor.collector -m collector run
gen com.stockpredictor.dashboard -m collector_dashboard serve 8899

echo
echo "Services installed. They now run independently of any terminal/Claude session."
echo "  dashboard : http://localhost:8899/data-collection"
echo "  status    : launchctl list | grep stockpredictor"
echo "  stop      : launchctl unload ~/Library/LaunchAgents/com.stockpredictor.*.plist"
echo "  logs      : runtime/logs/com.stockpredictor.*.{out,err}"
