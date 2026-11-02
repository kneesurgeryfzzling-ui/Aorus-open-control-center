# AORUS Open Control Center

An unofficial, open replacement for the AORUS Control Center, built for the **GIGABYTE AORUS 15G YC** laptop.
It controls the fans, power presets, per-key RGB keyboard and battery from a liquid-glass interface with a
live thunderstorm background, and keeps working from the system tray when the window is closed.

> ⚠️ **Disclaimer:** this is a personal, unofficial project. It is not made, endorsed or verified by GIGABYTE or AORUS.
> It reuses elements of the original AORUS Control Center (firmware commands and the keyboard protocol were
> taken from its DLLs). AORUS and GIGABYTE are trademarks of GIGABYTE Technology. Built with a little help from
> Claude (Anthropic). Use at your own risk; if in doubt, set the fans back to **Normal**.

## Features

### Fans
- Firmware modes: Normal, Quiet, Gaming, Turbo, fixed speed and capped auto (30–100 %)
- **Custom fan curve**: drag points on a temperature → speed graph; the app follows the hottest of CPU/GPU,
  ramps up instantly and slows down after ~8 s to avoid oscillation
- **Thermal guard**: fans go to 100 % above a threshold (93 °C by default) until things cool down by 8 °C
- **Cooling test**: loads every CPU core for 30 s, 1 min or 3 min, then measures the cool-down
  (peak temperature, clock speed, fan RPM and a verdict)

### Performance
- One-click power presets (Silent, Balanced, Performance, Turbo, Battery saver) combining the Windows power plan,
  CPU limits, GPU power limit and fan mode
- Custom presets
- **Auto profiles**: switch preset when a game starts or when plugging/unplugging the charger

### Keyboard (Fusion USB, per-key RGB)
- 3-zone colors, gradients, brightness, colour calibration for the LED's strong red channel
- **Per-key painting** with brush, eyedropper and eraser, plus templates
- 15+ software effects: Ripple, Tank, Fireworks, Matrix, Storm, Rain, Heat map, Snake, Starlight, Plasma,
  System HUD (F-keys = temperature, number row = CPU, etc.) and more
- Reactive effects light up the keys you press (key positions only, nothing is recorded)
- Lightning in the app flashes the keyboard, and keyboard lightning lights up the app

### Battery
- Charge limit (60–100 %), kept by the firmware even when the app is closed
- Dynamic Boost and AI Boost toggles

### Interface
- Live temperature chart, CPU/GPU/RAM/battery/network/disk meters, most demanding apps
- Liquid-glass panels and buttons with spring animations
- WebGL thunderstorm background: clouds, dense rain, refractive drops on the glass that you can wipe with the mouse.
  It follows your PC: rain = CPU load, wind = fan speed, storms = temperature
- Alternative "light lines" background
- Command palette (`Ctrl + K`) and page shortcuts (`Ctrl + 1…7`)
- Settings export/import

### Runs in the background
- Closing the window keeps the fan curve, thermal guard, auto profiles and keyboard effects running
- Tray icon to reopen the app, switch preset or fan mode, toggle the keyboard, or quit
- Windows notifications (thermal guard, low battery, auto profile, test finished)
- Optional start with Windows, directly in the background

## Requirements
- Windows 10 / 11 on an AORUS 15G YC (other GIGABYTE/AORUS laptops may partly work)
- Python 3.9+ and `psutil` (or the prebuilt `.exe`)
- Microsoft Edge (used as the app window, already installed on Windows)
- Optional: [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) running, for fan RPM
  and temperatures when the firmware doesn't expose them

## Getting started
```bat
lancer.bat          :: run from source (asks for admin rights)
build_exe.bat       :: build dist\AORUS-Open-CC.exe with PyInstaller
```
Administrator rights are required to talk to the firmware (WMI) and change power settings.

## How it works
- `app.py`: local web server on `127.0.0.1` (token-protected), tray icon, single instance, background mode
- `backends.py`: fans, presets, battery, monitoring, auto profiles, cooling test
- `gbwmi.ps1`: persistent PowerShell service calling the GIGABYTE WMI interfaces (`GB_WMIACPI_Set/Get`)
- `fusionhid.py`: Fusion USB keyboard protocol (HID feature reports + 512-byte per-key matrix)
- `kbfx.py`: per-key effect engine
- `tray.py`: notification-area icon written with plain `ctypes`
- `ui/index.html`: the whole interface (HTML/CSS/JS, WebGL background)
- `fake_firmware.py`: firmware simulator for testing without the laptop

## Troubleshooting
Something doesn't respond? Go to **System → Create a report** and open an issue with `rapport_aorus.txt`
(it lists the WMI classes found, exact errors and the log).

## Credits
- Intro: Aramas Studio
- Liquid-glass button and light-lines background inspired by components by designali-in on 21st.dev
- Made by Aramas Studio, with a little help from Claude (Anthropic)

<details>
<summary>🥚</summary>

<br>

**Made by Eldam / addabeat.**
Fuck big companies' software. DM me on Discord and I'll make a version for your specific laptop, for free :)

</details>
