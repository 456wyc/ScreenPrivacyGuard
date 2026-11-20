# PrivacyGuard AI

Cross-platform (Windows 10/11 + macOS) local-AI desktop privacy screen prototype.

## Features
- Local webcam face detection (OpenCV YuNet) and owner recognition (OpenCV SFace).
- Owner enrollment from 20 face samples.
- Fail-closed protection: once the owner is enrolled, the screen stays protected whenever the owner is not in view (e.g. you step away), even if no stranger is present. A short grace period avoids false triggers when you briefly look down.
- Approximate distance estimation from face width and configurable camera FOV.
- Distance-based dimming and blur strength.
- Simple overlay mode (no screen-recording permission required).
- True desktop blur mode using MSS screen capture + OpenCV Gaussian blur.
- Settings saved locally in `~/.privacyguard-ai/config.json`.
- Two independent protection methods that can be enabled/configured separately: (1) full-screen blur/dim overlay, and (2) auto-raise of chosen applications (Windows only). Each has its own on/off switch and its own trigger rules (stranger detected and/or owner absent).

## Requirements
- Python 3.11 recommended (3.10+ may work).
- A webcam.
- Windows: Windows 10/11.
- macOS: recent macOS release. For true blur, grant Screen & System Audio Recording / Screen Recording permission to the terminal or packaged app. Camera permission is also required.

## Install

### Windows (PowerShell)
```powershell
py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
```

### macOS
```bash
python3.11 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
python main.py
```

The first launch downloads the two OpenCV Zoo ONNX models into `~/.privacyguard-ai/models/`.
An internet connection is needed only for this first model download.

## First run
1. Click **Register / Update owner face**.
2. Sit in front of the camera, look toward it, and keep your face unobstructed for a few seconds.
3. Wait for 20 samples to be collected.
4. Select **Simple overlay** first and test with another person approaching.
5. Select **True desktop blur** to test screen capture. Grant OS permissions if prompted.

## Anti-flicker / smoothness tuning
Detected stranger distance and face hits naturally fluctuate frame to frame. The overlay smooths the visual risk with asymmetric easing (fast to protect, slow to release) plus show/hide hysteresis, and the vision worker applies a short miss-grace so a single dropped detection does not drop protection. The overlay is shown only while protecting (fading in from fully transparent, fading out before hiding), so when idle it is not on screen at all and cannot intercept mouse clicks; the window is also click-through via a Win32 extended style. You can tune these in `~/.privacyguard-ai/config.json`:

- `risk_rise_alpha` / `risk_fall_alpha` (0..1): easing speed. Smaller = smoother but slower to react.
- `show_threshold` / `hide_threshold`: hysteresis window for entering/leaving protection (hide should stay below show).
- `stranger_miss_grace` (seconds): how long protection is held after the stranger is momentarily lost.
- `capture_interval_ms` (true blur): screen-capture cadence; smaller is smoother but uses more CPU.
- `blur_kernel_size` (true blur): Gaussian kernel size; strength is controlled by a continuous sigma so there is no visible stepping.
- `exclude_from_capture` (Windows, true blur): exclude the overlay from screen captures so it never needs to hide. Disable only if your capture tool shows a residual self-capture.
- `protect_when_owner_absent`: when enabled (and an owner is enrolled), protect the screen whenever the owner is not in view. Disable to go back to stranger-only triggering.
- `owner_absent_risk`: protection strength (0..1) used when the owner is absent and no stranger is visible.
- `owner_absent_grace` (seconds): how long the owner may be out of view before fail-closed protection kicks in (avoids false triggers when looking down).
- `overlay_enabled` / `auto_raise_enabled`: independent on/off switches for the two protection methods. Turning the blur overlay off does **not** disable auto-raise (and vice versa).
- `overlay_triggers` / `auto_raise_triggers`: lists of reasons that activate each method. Allowed values: `"stranger"` (a visitor is detected) and `"owner_absent"` (enrolled owner is not in view). Both can be selected; an empty list falls back to both.

## Important limitations
- A normal RGB webcam does not measure depth. Distance is an estimate based on apparent face width, assumed average face width (14.5 cm), and camera horizontal FOV. Set the FOV to match your camera; a known-distance calibration is recommended.
- SFace matching is not a security-grade biometric authentication system. Lighting, pose, camera quality and face appearance can affect recognition. Keep a manual pause/disable hotkey available.
- The true blur overlay stays on screen and does not blink: on Windows it uses `WDA_EXCLUDEFROMCAPTURE` so the overlay is excluded from screen captures (it never has to hide itself), and the captured frame is blurred and swapped in continuously. On other platforms it falls back to a very brief hide. The first frame fades in smoothly rather than appearing at once, and blur strength changes continuously (fixed Gaussian kernel + continuous sigma). If your capture tool does not honour `WDA_EXCLUDEFROMCAPTURE`, set `exclude_from_capture` to `false` in the config to use the fallback. It captures the primary display only in this prototype.
- Protected content, DRM video, secure desktops, some fullscreen games, and some macOS capture contexts may not be capturable.
- This prototype does not upload frames or face embeddings. The owner embedding is stored locally in the app config. Protect your OS account.
- Do not use this as a substitute for physical privacy controls in high-risk environments.

## Two protection methods
Both methods are configured side by side, each with its own **enable switch** and **trigger reasons** (checkboxes for 有陌生人 / 主人离开, multi-select):
- **保护手段一 · 窗口模糊 / 遮挡覆盖** — the full-screen dim/blur overlay. Enable it and choose which reasons turn it on, plus tune effect mode, distances, dim and blur.
- **保护手段二 · 自动置顶指定程序** (Windows only) — bring chosen apps to the front. Enable it and choose which reasons turn it on, then add program rules:
  - **点选当前窗口…** — pick from a list of currently open windows; the process name is captured automatically.
  - **手动添加…** — type a process name (e.g. `chrome.exe`) and/or a window-title keyword.

A program rule matches when its process name and title keyword (whichever are set) both match; both fields are case-insensitive and at least one must be filled. When an enabled method's trigger reasons match the current situation, it activates; when the situation clears or the app quits, windows are restored to their original top-most state. The two methods are fully independent — disabling the blur overlay does not disable auto-raise. Auto-raise is driven directly by the live detection reason, so it works even while the overlay is off. Non-Windows platforms show a notice and disable the auto-raise section.

## Controls
- **Pause protection**: temporarily hides the privacy overlay.
- **Quit**: exits the app.
- The app window itself stays available for settings; the overlay is only shown when an unknown face is detected inside the configured trigger distance.
- The settings window is freely resizable (minimum 620×420). If the content is taller than the window, a vertical scroll bar appears so you can scroll; the camera preview can also be collapsed with **▾ 收起摄像头画面** (detection keeps running in the background). Related fields are grouped onto shared rows to keep the layout compact.

## Packaging

### Windows
```powershell
pip install pyinstaller
pyinstaller --noconfirm --windowed --name PrivacyGuardAI main.py
```

### macOS
```bash
pip install pyinstaller
pyinstaller --noconfirm --windowed --name PrivacyGuardAI main.py
```
For distribution, test camera and screen-capture permissions from the signed `.app`. macOS distribution generally requires code signing/notarization.

## Project structure
```text
PrivacyGuardAI/
├── main.py
├── requirements.txt
├── privacyguard/
│   ├── __init__.py
│   ├── app.py
│   ├── config.py
│   ├── overlay.py
│   ├── vision.py
│   └── windowctl.py
└── README.md
```
