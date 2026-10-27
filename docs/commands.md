# Commands

Global flags: `--config`, `--log`, `--no-log`, `-v`, `--version`.

| Command | What it does |
| --- | --- |
| `headroom status` | RAM, commit, page file, and free space. Exit 1 when a threshold is crossed. |
| `headroom top` | Largest processes by RAM. `-n` sets the count (1 to 100). |
| `headroom scan PATH` | Largest entries in a folder. `--top` is 1 to 500. Links are not followed. |
| `headroom clean --dry-run` | Allowlist preview. Deletes nothing. |
| `headroom clean` | Deletes allowlisted caches after you Type YES. |
| `headroom watch` | Stay running. `--once` takes one sample. `--no-tray` stays in the console. |
| `headroom install` | Start at sign-in. `headroom uninstall` removes the task and the Startup-folder script. |
| `headroom history` | Recorded samples. `--since 7d` or `--hours 48`. |
| `headroom changes` | Folder growth and new files over 500 MB. `--since 1d` or `--since 7d`. |
| `headroom report` | Trend, likely leaks, and growth. |
| `headroom self` | This process, the watch process, and Roomy against their budgets. |
| `headroom analyze` | Read files and write a report. `--root` may be repeated. Does not move or delete. |
| `headroom profile list` | Student, Developer, and Gamer starter profiles. |
| `headroom profile use student` | Turn profiles on. `none` clears them. Your rules still override them. |
| `headroom profile show` | Show one profile, or the merged rules when the name is omitted. |
| `headroom rules show` | Your keep and toss lines. |
| `headroom rules add keep name: thesis` | Append one line. |
| `headroom rules paste` | Append from stdin, or `--file`. |
| `headroom review` | Toss suggestions. `--apply` waits for `MOVE`. `--group` and `--each` narrow the prompt. |
| `headroom dashboard` | Page on `127.0.0.1`. Default port 47321. Moves still require `MOVE`. |
| `headroom dupes` | Duplicate groups. `--root` may be repeated. `--apply` waits for `MOVE`. |
| `headroom startup` | Same as `headroom startup list`. Run keys, Startup folders, and logon tasks. |
| `headroom startup disable ID` | Waits for `DISABLE`. `headroom startup restore` waits for `RESTORE`. |
| `headroom downloads` | Old downloads and already-run installers. `--root` overrides Downloads. `--apply` waits for `MOVE`. |
| `headroom mcp` | stdio MCP server. |
| `headroom pet` | Roomy. `headroom widget` opens the same pet. `--skin`, `--lite`. |

`headroom clean --only user_temp,chrome_cache` narrows the allowlist. It cannot name a path.

## MCP tools

`clean`, `move`, and `startup_disable` only return plans. Confirm on the PC with `headroom clean`, `headroom review --apply`, or `headroom startup disable`.

| Tool | Returns |
| --- | --- |
| `status` | RAM, commit, and free space. |
| `top` | Processes using the most RAM. |
| `scan` | Largest entries under a folder. |
| `analyze` | File categories. Nothing is saved or moved. |
| `report` | Latest history report. |
| `changes` | Folders that grew, and new large files. |
| `dupes` | Duplicate groups. |
| `startup_list` | Startup entries. |
| `review_list` | Toss suggestions. |
| `clean` | Dry-run plan of the allowlisted caches. |
| `move` | The review list a move would offer. |
| `startup_disable` | What disabling one startup id would do. |

The prompt `health_check` is the text in `docs/ai-starter-prompt.md`.

## Defaults

Warn when RAM stays above 85% for 120 seconds, or when `C:` has under 30 GB free (critical under 15 GB). The watch interval is 30 seconds, 120 on battery, 90 when idle, and 10 under pressure.

History is `%LOCALAPPDATA%\Headroom\history.db`, or `~/.local/share/headroom/history.db`. `HEADROOM_DB` overrides the path. The cap is 32 MB. Each sample is committed in WAL mode before the call returns, so another process can read it. The first sample is written before the first sleep.

Exit 0 is success or a declined cleanup. Exit 1 is `status` over a threshold. Exit 2 is a bad argument, a refused path, or a move without a terminal.
