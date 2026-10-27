# Headroom plugins

Plugins are Python entry points. Headroom loads three groups. A plugin that fails to import is skipped. Built-in profile names (`student`, `developer`, `gamer`) are never replaced.

| Group | Entry point | Callable |
| --- | --- | --- |
| `headroom.scanners` | any name | `scan(root) -> list[{"path", "size", "note"}]` |
| `headroom.profiles` | profile name | `profile() -> str` of keep/toss lines |
| `headroom.reports` | any name | `report_section(context) -> str` |
| `headroom.skins` | skin name | `skin() -> {"name", "sheet"}` pointing at a PNG sprite sheet |

Scanners run at the end of `headroom analyze` and only add records. Profiles show up in `headroom profile list` and apply after the built-in profiles when you select them. Report sections are appended to `headroom report`. None of the hooks delete or move files. A skin is a sprite sheet for Roomy. Built-in names `roomy`, `midnight`, and `tower` stay in place. The sheet is 4 columns by 11 rows of 112×88 frames, in this row order: power, happy, walk, tired, stuffed, sleep, scan, flicker, dusty, slimy, sparkle. The background is magenta `#ff00ff`. A plugin can pass a `rows` map when its sheet uses a shorter list. The scan row is a green phosphor grid with a sweep and blips. Dusty and slimy are the needs-cleanup faces. Sparkle is the shake-off after a real cleanup or an approved review.

## Example

`examples/plugin` flags `*.scratch` files, adds a toss rule for those names once they are 30 days old, and adds one report paragraph.

```powershell
py -3.12 -m pip install -e examples\plugin
headroom profile list
headroom profile use scratch
headroom analyze --root D:\notes
headroom report
```

The example is also plain functions in `examples/plugin/headroom_demo_plugin.py` if you want to read the shape without installing it.

## Writing one

```python
def scan(root):
    return [{"path": r"D:\notes\draft.scratch", "size": 12, "note": "scratch file"}]

def profile() -> str:
    return "toss name: *.scratch older-than: 30d\n"

def report_section(context) -> str:
    return "Scratch files older than 30 days are suggestions.\n"
```

Register them in your package:

```toml
[project.entry-points."headroom.scanners"]
scratch = "my_plugin:scan"

[project.entry-points."headroom.profiles"]
scratch = "my_plugin:profile"

[project.entry-points."headroom.reports"]
scratch = "my_plugin:report_section"
```

Profile text uses the same keep/toss lines as `headroom rules add`. Your own rules in `user.rules` still win, because they are loaded last.
