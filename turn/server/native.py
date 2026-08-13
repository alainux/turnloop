"""Small platform boundary for desktop-native affordances.

The web UI asks this adapter for a directory; native bundles can replace the
implementation without changing the authoring API. No path is accepted from
the shell command, so there is no command-injection surface.
"""
from __future__ import annotations

import os
from pathlib import Path
import shutil
import subprocess
import sys


def choose_directory() -> str | None:
    if sys.platform == "darwin":
        result = subprocess.run(
            ["osascript", "-e", 'POSIX path of (choose folder with prompt "Choose a Turn project directory")'],
            capture_output=True,
            text=True,
        )
    elif sys.platform.startswith("linux") and shutil.which("zenity"):
        result = subprocess.run(["zenity", "--file-selection", "--directory"], capture_output=True, text=True)
    elif os.name == "nt":  # pragma: no cover - exercised by platform CI
        script = "Add-Type -AssemblyName System.Windows.Forms; $d=New-Object System.Windows.Forms.FolderBrowserDialog; if($d.ShowDialog() -eq 'OK'){$d.SelectedPath}"
        result = subprocess.run(["powershell", "-NoProfile", "-Command", script], capture_output=True, text=True)
    else:
        return None
    if result.returncode != 0 or not result.stdout.strip():
        return None
    path = Path(result.stdout.strip()).expanduser().resolve()
    return str(path) if path.is_dir() else None
