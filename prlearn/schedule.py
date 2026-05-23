from __future__ import annotations

import os
import platform
import shutil
from pathlib import Path


PLIST_NAME = "com.prlearn.daily.plist"


def runner_script(home: Path) -> Path:
    path = home / "bin" / "prlearn-daily"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!/bin/sh\nexec prlearn daily --home {home}\n")
    path.chmod(0o755)
    return path


def schedule_text(home: Path) -> dict[str, str]:
    runner = runner_script(home)
    system = platform.system().lower()
    if system == "darwin":
        plist = Path.home() / "Library" / "LaunchAgents" / PLIST_NAME
        content = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.prlearn.daily</string>
  <key>ProgramArguments</key>
  <array><string>{runner}</string></array>
  <key>StartCalendarInterval</key><dict><key>Hour</key><integer>9</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>{home / "logs" / "daily.log"}</string>
  <key>StandardErrorPath</key><string>{home / "logs" / "daily.log"}</string>
</dict>
</plist>
"""
        return {"kind": "launchd", "path": str(plist), "content": content, "install": f"launchctl load {plist}"}
    if system == "linux" and shutil.which("systemctl"):
        unit = f"""[Unit]
Description=Run prlearn daily

[Service]
Type=oneshot
ExecStart={runner}
"""
        timer = """[Unit]
Description=Run prlearn daily every morning

[Timer]
OnCalendar=*-*-* 09:00:00
Persistent=true

[Install]
WantedBy=timers.target
"""
        return {"kind": "systemd", "content": unit + "\n--- prlearn-daily.timer ---\n" + timer, "install": "systemctl --user enable --now prlearn-daily.timer"}
    if system == "windows":
        return {"kind": "windows", "content": f"PowerShell: New-ScheduledTaskAction -Execute '{runner}'", "install": "Register-ScheduledTask ..."}
    return {"kind": "cron", "content": f"0 9 * * * {runner} >> {home / 'logs' / 'daily.log'} 2>&1", "install": "crontab -e"}


def install_schedule(home: Path) -> dict[str, str]:
    spec = schedule_text(home)
    if spec["kind"] == "launchd":
        path = Path(spec["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(spec["content"])
    return spec


def uninstall_schedule(home: Path) -> bool:
    path = Path.home() / "Library" / "LaunchAgents" / PLIST_NAME
    if path.exists():
        path.unlink()
        return True
    return False


def status(home: Path) -> dict[str, object]:
    plist = Path.home() / "Library" / "LaunchAgents" / PLIST_NAME
    runner = home / "bin" / "prlearn-daily"
    return {"runner_exists": runner.exists(), "launch_agent_exists": plist.exists(), "platform": platform.system()}
