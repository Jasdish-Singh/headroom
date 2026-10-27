# Roomy

Roomy is a read-only desktop pet. `headroom pet` and `headroom widget` open it. `--skin midnight` is the dark CRT. `--skin tower` is a desktop-case body with the same faces. `--lite` keeps it still.

A speech bubble shows the current RAM usage, like "RAM 62% used". The face reacts to RAM and idle only: it looks tired above the RAM line, falls asleep after a long idle, and scans while Headroom analyzes files or finds duplicates. Mute hides the bubble. Sleep, power-on, scanning, and sparkle stay quiet.

Click opens the local dashboard. Drag remembers the spot. Right-click: dashboard, report, mute, hide, quit. There is no cleanup item. The dashboard has an on/off toggle that starts or stops Roomy and remembers the choice across restarts. The watch tray menu has **Show Roomy**. Set `widget.autostart` in config if sign-in should start the pet next to watch; the dashboard toggle overrides it.

Walking is about 7 frames per second. Idle is about 1.5. Hidden, asleep, a fullscreen app, and lite mode do not animate. The sprite sheet loads once. Health comes from the watch snapshot (`watch-stats.json`) when that file is fresh.

`headroom self` prints Roomy against a budget of 30 MB extra RAM and 2% CPU. The watch process is budgeted at 40 MB and 1%.

Sparkle follows a cleanup that freed space, or a review move you approved. A dry run does not sparkle.

## Sheet

The sheet is 4 columns by 11 rows, each frame 112×88, in this order: power, happy, walk, tired, stuffed, sleep, scan, flicker, dusty, slimy, sparkle. Roomy no longer shows the disk-related stuffed, dusty, and slimy faces; those rows stay in the sheet so plugin skins keep the same layout. See [plugins.md](plugins.md).
