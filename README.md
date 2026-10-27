# Precision Touchpad Zones

See and edit the palm-rejection zones of a Windows Precision Touchpad. These are the supercurtain, curtain and right-click zones.

![zones](https://img.shields.io/badge/Windows-10%201903%2B%20%7C%2011-blue)

- Draws your touchpad to scale, using the real size the device reports, with every zone on top.
- Shows your fingers live. Each contact is coloured by the zone it **started** in, which is all the zones check. Touches the pad reports as a palm show as a dashed outline.
- Edit the zones with the spin boxes, or drag a zone's edge on the picture.
- Reads the touchpad's own registry key and the global key, and shows which one each value comes from.
- Backs up the affected registry keys before every write, and can restore a backup.
- Restarts the touchpad so changes take effect without a reboot.

## Run it

Requires Python 3.9+ (Tkinter is included). Nothing else to install.

**Run it as administrator.** Windows only allows these values to be written with admin rights. Without them, the tool opens read-only and offers a *Restart as admin* button.

```
python touchpad_zones.py
```

You can also double-click `Touchpad Zones.bat`. `python touchpad_zones.py --probe` prints the detected touchpad and current values without opening the window.

## How the zones work

| Zone | Effect |
| --- | --- |
| **Supercurtain** | Taps that start here are almost always rejected. |
| **Curtain** | Input that starts here is ignored for a short time after typing. How long depends on *Settings → Touchpad → Touchpad sensitivity*: lower sensitivity means longer. |
| **Right-click zone** | Pressing here gives a right-click (percent of the pad's width and height). |

- Values are stored in himetric (0.01 mm). The tool shows millimetres.
- Curtains only apply when `CurtainLeft`, `CurtainRight` and `CurtainTop` are all set. Otherwise Windows uses its built-in curtain.
- Windows checks the touchpad's own key (`Enum\HID\…\Device Parameters`) first, then the global `HKLM\SOFTWARE\Microsoft\Windows\CurrentVersion\PrecisionTouchPad`. The default **Auto** mode writes each value where it will take effect.
- The zones reject touches that *start* inside them. A finger that slides in from the middle is not affected.
- A driver update can overwrite values in the touchpad's own key.

Reference: [Microsoft – Precision touchpad tuning](https://learn.microsoft.com/windows-hardware/design/component-guidelines/touchpad-tuning-guidelines)
