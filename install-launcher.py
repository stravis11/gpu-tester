#!/usr/bin/env python3
"""Install a user desktop launcher for this checkout (no root required)."""
import os
from pathlib import Path


def desktop_escape(value):
    return str(value).replace('\\', '\\\\').replace('\n', '\\n').replace('\t', '\\t').replace('\r', '\\r')


def main():
    root = Path(__file__).resolve().parent
    executable = desktop_escape(root / 'launch.sh')
    executable = executable.replace('"', '\\"').replace('`', '\\`').replace('$', '\\$').replace('%', '%%')
    data = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share')))
    destination = data / 'applications/gpu-tester.desktop'
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(f'''[Desktop Entry]
Type=Application
Name=GPU Tester
Comment=Test an NVIDIA or AMD GPU with live telemetry and buyer evidence
Exec="{executable}"
Path={desktop_escape(root)}
Icon=video-display
Terminal=false
Categories=Utility;
StartupNotify=true
StartupWMClass=local.omarchy.GpuTester
''')
    print(destination)


if __name__ == '__main__':
    main()
