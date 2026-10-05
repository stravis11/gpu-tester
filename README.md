# GPU Tester

A native GTK4 Linux desktop application for testing the installed AMD or NVIDIA GPU.
Developed for Omarchy on x86_64 Linux with Hyprland. The core engine supports
AMD and NVIDIA; screen recording and webcam placement require Hyprland/Omarchy.
AMD has been tested on hardware; NVIDIA telemetry is covered by automated fixtures.

Launch `./launch.sh`, or use the **GPU Tester** application launcher entry.
Click **Test GPU**. The app re-identifies the card each time, tests available
video memory for approximately five minutes, then runs Heaven with the remaining
time. A shared 590-second deadline leaves time to close the recording within
ten minutes. Report generation happens after testing and recording stop.

The dashboard shows core/junction temperatures, GPU load, power, VRAM use,
a temperature graph, elapsed time, and live tool output. Missing sensors show
an em dash. **Stop test** saves partial evidence. Closing during a run stops
the tests and saves results before allowing the window to close.

**Benchmark overlay** is enabled by default. MangoHud draws FPS,
frame time/graph, GPU load, core/junction/memory temperatures, VRAM usage,
GPU clocks and power directly over Heaven, where those sensors are available.
It works with AMD and NVIDIA OpenGL drivers and is visible in screen recordings.
The overlay is injected only into Heaven. Disable it to compare scores without
overlay overhead. No system-wide MangoHud configuration is changed.

Enable **Webcam PiP** to include a live view of the physical card and monitor.
This also enables **Record screen**. Select the camera and Small/Medium/Large
inset size, then use **Preview camera** to aim it before clicking **Test GPU**.
The preview preserves the full camera image, including its aspect ratio.
It appears in the bottom-right corner during both tests and is captured in the
same MP4 as the desktop. Closing the preview or changing its selection releases
the camera. Tests close the inset automatically when finished or stopped.

The app uses mpv with Omarchy's webcam window styling. It opens only the selected
capture device and stops only its own camera process. A missing, busy or failed
camera is reported explicitly; losing the camera during a requested recording
stops testing and saves partial evidence. Camera mode/settings and logs are saved
with the run. The video may help connect a physical card to the tests; the app
does not claim to automatically verify the physical label or manufacturer serial.
The recording and test time cap continues to apply with the webcam enabled.

Turn on **Record screen** for an MP4 of the focused Hyprland monitor (30 FPS,
up to 1080p, no audio). It captures everything visible on that monitor. The
recorder belongs to this app, so stopping it does not stop another recording.
Recording adds encoding load and can affect the benchmark score.

**Export reports** saves HTML and PDF. Every run saves raw logs, telemetry CSV,
identity before/after, JSON results, SHA256 checksums and an evidence ZIP. Use
**Save to…** to choose a folder; otherwise results go in `results/`. Previous
runs are preserved. **Open report** and **Open results** open the saved output.

## Installation

Clone the repository and install the system dependencies using your distribution's
package manager. On Omarchy, use `omarchy pkg add <package>` for missing packages.

- Python 3.10+, `python-gobject`, `python-cairo`, `gtk4`, `libadwaita`, and `pciutils`.
- A working GPU driver and Vulkan loader (`vulkan-icd-loader` on Arch).
- NVIDIA: `nvidia-smi` and the NVIDIA Vulkan ICD; AMD: amdgpu sensors and `vulkan-radeon`.
- Optional overlay: `mangohud` with its 64-bit OpenGL shim.
- Optional PDF: `chromium`.
- Optional recording: `gpu-screen-recorder` and `ffmpeg` (provides `ffprobe`).
- Optional webcam: `mpv`, `v4l-utils`, and Omarchy's webcam window styling.

Third-party benchmark binaries and drivers are **not redistributed** in this
repository. Obtain them from their upstream projects and follow their licenses:

1. Download the Linux x86_64 archive from
   [memtest_vulkan releases](https://github.com/GpuZelenograd/memtest_vulkan/releases).
   Extract it and put its executable at `tools/memtest_vulkan`.
2. Download Heaven 4.0 for Linux from
   [UNIGINE](https://benchmark.unigine.com/heaven). From the checkout root, extract
   its official installer with `sh /path/to/Unigine_Heaven-4.0.run --noexec --target tools/heaven`.
   Keep the complete directory, including `bin/` and `data/`.
3. Make the executables runnable and launch the app:

   ```sh
   chmod +x tools/memtest_vulkan tools/heaven/bin/heaven_x64
   ./launch.sh
   ```

The expected layout is:

```text
gpu-tester/
  app.py
  engine.py
  launch.sh
  tools/
    memtest_vulkan
    heaven/
      bin/heaven_x64
      data/
```

To use an existing tools directory, set `GPU_TESTER_TOOLS=/absolute/path/to/tools`.
Existing sibling `gpu-sale-evidence/tools` installations are also recognized.
MangoHud is discovered under standard Arch/Debian library paths, or in a local
`vendor/mangohud` extraction. Override its shim path with
`GPU_TESTER_MANGOHUD_LIBRARY=/absolute/path/to/libMangoHud_shim.so` if necessary;
the matching `libMangoHud_opengl.so` must be in the same directory.

Install a desktop menu entry with `python3 install-launcher.py`. It points to
this checkout, so reinstall it if you move the directory. The app does not
install packages, overclock, or modify drivers or power limits.

## Privacy and saved evidence

Recordings include everything visible on the selected monitor and, when enabled,
the webcam. Reports include available hardware identifiers and system logs.
Review a result bundle before sharing it. Generated results, recordings, local
benchmark binaries, and validation captures are excluded from Git by default.

## Interpreting evidence

- **PASS**: both the five-minute memory run and graphics benchmark completed
  with explicit memory PASS, valid GPU rendering, and no recorded framebuffer
  errors. Heaven settings: 1920×1080 windowed, Ultra, 8× AA (option value 3),
  requested normal tessellation. Any rejected tessellation command is disclosed.
- **FAIL**: memory errors detected.
- **INCOMPLETE**: stopped, deadline reached, safety threshold crossed, or a stage
  did not finish. An incomplete benchmark receives no FPS score.
- **TOOL ERROR**: driver, telemetry, renderer or execution problem. This does
  not establish a hardware failure.

Temperature limits: 85°C core, 100°C junction, 95°C memory. Missing core
temperature stops the test. Only one supported GPU may be attached to avoid
misattributing logs or testing a software renderer. Multiple GPUs are rejected.
Manufacturer label serial is reported only if a hardware interface exposes it;
UUID, unique ID and PCI address are never substituted for it.

The memory tool may reduce its allocation because of driver constraints.
A PASS does not establish coverage of every byte of VRAM. Physical condition,
display ports, audible fan behavior and human visual artifacts are outside the
automated tests. Kernel log access restrictions are disclosed. Checksums are
integrity data, not independent certification. Tests cannot guarantee future
reliability.

## Development validation

Run `python -m unittest discover -s tests -v` for verdict, safety, process
cleanup and export tests. `./launch.sh --auto-start --record` is an internal
integration validation mode that runs the real test and records the desktop;
it saves `last-validation.json` in addition to the usual results.

GTK widgets follow the [official PyGObject GTK4 documentation](https://pygobject.gnome.org/tutorials/gtk4.html).
Capture uses [gpu-screen-recorder](https://git.dec05eba.com/gpu-screen-recorder/about/).
Automated tests do not require the GUI, benchmark binaries, or attached GPU.
