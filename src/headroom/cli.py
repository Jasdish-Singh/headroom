"""Command line: status, top, scan, clean, watch, history, and auto-start."""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import date
from pathlib import Path

from headroom import __version__
from headroom import probe as probe_mod
from headroom import processes as processes_mod
from headroom.autostart import AutostartError, install as install_autostart
from headroom.autostart import uninstall as uninstall_autostart
from headroom.cleanup import (
    apply,
    build_preview,
    default_recycle_backend,
    discover_groups,
)
from headroom.analyze import (
    default_roots,
    format_analysis,
    load_analysis,
    save_analysis,
    scan as scan_files,
)
from headroom.config import Config, ConfigError, load_config
from headroom.firstrun import maybe_offer_first_run, parse_profile_list
from headroom.history import HistoryStore, build_changes, build_report, default_history_path
from headroom.instance import InstanceLock, default_lock_path
from headroom.logsetup import configure_logging, default_log_path
from headroom.review import (
    ReviewError,
    archive_directory,
    confirm_move,
    format_move_stats,
    format_review,
    group_suggestions,
    move_records,
    parse_group_selection,
    recycle_file,
    suggestions_for,
    collect_suggestions,
)
from headroom.dashboard import DashboardError, new_token, serve_dashboard
from headroom.downloads import (
    classify_downloads,
    default_downloads,
    format_downloads,
    installed_program_names,
)
from headroom.dupes import find_duplicates, format_dupes
from headroom.plugins import profile_plugins, report_sections, scanner_records
from headroom.rules import (
    PROFILE_NAMES,
    RuleError,
    append_user_text,
    load_merged_rules,
    load_profile_text,
    read_active_profiles,
    read_user_text,
    state_dir,
    write_active_profiles,
)
from headroom.render import (
    format_changes,
    format_history,
    format_preview,
    format_processes,
    format_report,
    format_scan,
    format_self,
    format_stats,
    format_status,
    status_is_attention,
)
from headroom.safety import KNOWN_CLEANUP_IDS, SafetyError
from headroom.startup import (
    StartupError,
    append_backup,
    default_catalog,
    format_startup,
    load_backup,
    remove_backup,
)
from headroom.scan import scan_path
from headroom.schedule import lower_priority
from headroom.selfcheck import read_agent, read_pet, read_self
from headroom.service import Session, supervise
from headroom.tracker import DeepScanner, Tracker, tracking_roots
from headroom.pet import (
    cleanup_should_sparkle,
    clear_scan,
    launch_pet_if_needed,
    load_skins,
    mark_scan,
    mark_sparkle,
    review_should_sparkle,
    run_pet,
    send_control,
)
from headroom.watch import run_tray, run_watch_loop

_CONFIRM = "Type YES to permanently delete these files: "


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except KeyboardInterrupt:
        print("\nStopped.", file=sys.stderr)
        return 130
    except ConfigError as exc:
        print(f"headroom: {exc}", file=sys.stderr)
        return 2
    except SafetyError as exc:
        print(f"headroom: {exc}", file=sys.stderr)
        return 2
    except RuleError as exc:
        print(f"headroom: {exc}", file=sys.stderr)
        return 2
    except ReviewError as exc:
        print(f"headroom: {exc}", file=sys.stderr)
        return 2
    except StartupError as exc:
        print(f"headroom: {exc}", file=sys.stderr)
        return 2
    except DashboardError as exc:
        print(f"headroom: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"headroom: {exc}", file=sys.stderr)
        return 2


def _main(argv: list[str] | None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "config"):
        args.config = None
    if not hasattr(args, "log"):
        args.log = None
    if not hasattr(args, "no_log"):
        args.no_log = False
    if not hasattr(args, "verbose"):
        args.verbose = False
    config, config_path = load_config(args.config)
    log_path = _resolve_log_path(args, config)
    logger = configure_logging(log_path, args.verbose)
    if args.verbose:
        logger.info("config %s", config_path if config_path is not None else "defaults")
    maybe_offer_first_run(args.command)
    return args.func(args, config, config_path, logger, log_path)


def build_parser() -> argparse.ArgumentParser:
    # SUPPRESS keeps a flag passed before the subcommand from being reset to
    # the default when the subparser does not see it again.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--config",
        type=Path,
        default=argparse.SUPPRESS,
        help="TOML or YAML config file",
    )
    common.add_argument(
        "--log",
        type=Path,
        default=argparse.SUPPRESS,
        help="log file (default: per-user Headroom log)",
    )
    common.add_argument(
        "--no-log",
        action="store_true",
        default=argparse.SUPPRESS,
        help="do not write a log file",
    )
    common.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=argparse.SUPPRESS,
        help="more detail on the console",
    )

    parser = argparse.ArgumentParser(
        prog="headroom",
        description=(
            "Watch RAM and disk on a Windows laptop, analyze files, and clean only known-safe caches."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
        epilog=(
            "examples:\n"
            "  headroom status\n"
            "  headroom top\n"
            "  headroom scan %USERPROFILE%\n"
            "  headroom clean --dry-run\n"
            "  headroom clean\n"
            "  headroom watch\n"
            "  headroom install\n"
            "  headroom self\n"
            "  headroom history\n"
            "  headroom changes --since 7d\n"
            "  headroom report\n"
            "  headroom analyze\n"
            "  headroom profile list\n"
            "  headroom profile use student\n"
            "  headroom rules add keep name: thesis\n"
            "  headroom review\n"
            "  headroom dashboard\n"
            "  headroom dupes %USERPROFILE%\\Downloads\n"
            "  headroom startup\n"
            "  headroom downloads\n"
            "  headroom mcp\n"
            "  headroom pet\n"
            "  headroom widget\n"
        ),
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser(
        "status",
        parents=[common],
        help="show RAM, commit, page file, and free space",
    )
    status.set_defaults(func=cmd_status)

    top = sub.add_parser(
        "top",
        parents=[common],
        help="show the processes using the most RAM",
    )
    top.add_argument("-n", "--count", type=int, help="how many processes to show")
    top.set_defaults(func=cmd_top)

    scan = sub.add_parser(
        "scan",
        parents=[common],
        help="show the largest items under a folder",
    )
    scan.add_argument("path", type=Path)
    scan.add_argument("--top", type=int, help="how many entries to show")
    scan.set_defaults(func=cmd_scan)

    clean = sub.add_parser(
        "clean",
        parents=[common],
        help="preview or delete known-safe caches (always previews first)",
    )
    clean.add_argument(
        "--dry-run",
        action="store_true",
        help="show sizes and delete nothing",
    )
    clean.add_argument(
        "--only",
        help="comma-separated built-in target ids to include (narrows config)",
    )
    clean.set_defaults(func=cmd_clean)

    watch = sub.add_parser(
        "watch",
        parents=[common],
        help="monitor and warn; shows a tray icon when available",
    )
    watch.add_argument("--no-tray", action="store_true", help="console only")
    watch.add_argument("--once", action="store_true", help="sample once and exit")
    watch.set_defaults(func=cmd_watch)

    history = sub.add_parser(
        "history",
        parents=[common],
        help="show recorded RAM, commit, and free space",
    )
    history.add_argument("--hours", type=float, default=24, help="hours of history to show")
    history.add_argument(
        "--since",
        help="window such as 24h, 7d, yesterday, or week (overrides --hours)",
    )
    history.set_defaults(func=cmd_history)

    changes = sub.add_parser(
        "changes",
        parents=[common],
        help="show folders and large files that grew",
    )
    changes.add_argument(
        "--since",
        default="1d",
        help="window such as 1d, 7d, yesterday, or week",
    )
    changes.set_defaults(func=cmd_changes)

    report = sub.add_parser(
        "report",
        parents=[common],
        help="summarize trends, leaks, and growth",
    )
    report.set_defaults(func=cmd_report)

    own = sub.add_parser(
        "self",
        parents=[common],
        help="show Headroom's own CPU and RAM",
    )
    own.set_defaults(func=cmd_self)

    install = sub.add_parser(
        "install",
        parents=[common],
        help="start Headroom at Windows sign-in",
    )
    install.set_defaults(func=cmd_install)

    remove = sub.add_parser(
        "uninstall",
        parents=[common],
        help="remove the Windows sign-in startup entry",
    )
    remove.set_defaults(func=cmd_uninstall)

    analyze = sub.add_parser(
        "analyze",
        parents=[common],
        help="read-only categorization of your files",
    )
    analyze.add_argument(
        "--root",
        action="append",
        type=Path,
        dest="roots",
        help="folder to read (repeatable). Default: Documents, Downloads, and similar",
    )
    analyze.set_defaults(func=cmd_analyze)

    profile = sub.add_parser(
        "profile",
        parents=[common],
        help="list, choose, or show starter profiles",
    )
    profile_sub = profile.add_subparsers(dest="profile_command", required=True)
    profile_list = profile_sub.add_parser("list", parents=[common], help="list starter profiles")
    profile_list.set_defaults(func=cmd_profile_list)
    profile_use = profile_sub.add_parser("use", parents=[common], help="select profiles")
    profile_use.add_argument("names", nargs="+", help="student, developer, gamer, or none")
    profile_use.set_defaults(func=cmd_profile_use)
    profile_show = profile_sub.add_parser("show", parents=[common], help="show a profile or the merged rules")
    profile_show.add_argument("name", nargs="?", help="profile name; omit to show the active rules")
    profile_show.set_defaults(func=cmd_profile_show)

    rules = sub.add_parser(
        "rules",
        parents=[common],
        help="add your own keep and toss lines",
    )
    rules_sub = rules.add_subparsers(dest="rules_command", required=True)
    rules_show = rules_sub.add_parser("show", parents=[common], help="show your rules")
    rules_show.set_defaults(func=cmd_rules_show)
    rules_add = rules_sub.add_parser("add", parents=[common], help="append one keep or toss line")
    rules_add.add_argument("text", nargs="+", help="for example: keep name: thesis")
    rules_add.set_defaults(func=cmd_rules_add)
    rules_paste = rules_sub.add_parser("paste", parents=[common], help="append rules from stdin or a file")
    rules_paste.add_argument("--file", type=Path, help="text file of rules")
    rules_paste.set_defaults(func=cmd_rules_paste)

    review = sub.add_parser(
        "review",
        parents=[common],
        help="dry-run toss suggestions; move only after you type MOVE",
    )
    review.add_argument("--apply", action="store_true", help="ask before moving the files you pick")
    review.add_argument("--each", action="store_true", help="ask MOVE for every file")
    review.add_argument("--group", action="append", help="group number or 'all' to preselect")
    review.set_defaults(func=cmd_review)

    board = sub.add_parser(
        "dashboard",
        parents=[common],
        help="serve a localhost page for history, the report, and review",
    )
    board.add_argument("--port", type=int, default=47321, help="port on 127.0.0.1 (default 47321)")
    board.set_defaults(func=cmd_dashboard)

    dupes = sub.add_parser(
        "dupes",
        parents=[common],
        help="find duplicate files; move extras only after you type MOVE",
    )
    dupes.add_argument("--root", action="append", type=Path, dest="roots", help="folder to read")
    dupes.add_argument("--apply", action="store_true", help="ask before moving the extra copies")
    dupes.set_defaults(func=cmd_dupes)

    startup = sub.add_parser(
        "startup",
        parents=[common],
        help="list startup apps; disable only after you type DISABLE",
    )
    startup_sub = startup.add_subparsers(dest="startup_command")
    startup_list = startup_sub.add_parser("list", parents=[common], help="list startup entries")
    startup_list.set_defaults(func=cmd_startup_list)
    startup_disable = startup_sub.add_parser("disable", parents=[common], help="disable one entry")
    startup_disable.add_argument("entry_id", help="id from headroom startup")
    startup_disable.set_defaults(func=cmd_startup_disable)
    startup_restore = startup_sub.add_parser("restore", parents=[common], help="restore disabled entries")
    startup_restore.add_argument("entry_id", nargs="?", help="one id, or all backed-up entries")
    startup_restore.set_defaults(func=cmd_startup_restore)
    startup.set_defaults(func=cmd_startup_list)

    downloads = sub.add_parser(
        "downloads",
        parents=[common],
        help="suggest old downloads and installers that were already run",
    )
    downloads.add_argument("--root", type=Path, help="folder to read (default: Downloads)")
    downloads.add_argument("--apply", action="store_true", help="ask before moving the suggestions")
    downloads.set_defaults(func=cmd_downloads)

    mcp_cmd = sub.add_parser(
        "mcp",
        parents=[common],
        help="stdio MCP server; action tools are dry-run only",
    )
    mcp_cmd.set_defaults(func=cmd_mcp)

    for pet_name, pet_help in (
        ("widget", "open Roomy, the desktop pet (read-only)"),
        ("pet", "open Roomy, the desktop pet (read-only)"),
    ):
        pet_cmd = sub.add_parser(pet_name, parents=[common], help=pet_help)
        pet_cmd.add_argument("--skin", help="roomy, midnight, tower, or a plugin skin")
        pet_cmd.add_argument(
            "--lite",
            action="store_true",
            help="static pet; the face changes with mood and does not walk",
        )
        pet_cmd.set_defaults(func=cmd_pet)
    return parser


def cmd_status(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del args, logger, log_path
    try:
        current = probe_mod.sample()
    except Exception as exc:
        print(f"headroom: could not read system stats: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(format_status(current, config, config_path))
    return 1 if status_is_attention(current, config) else 0


def cmd_top(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config_path, logger, log_path
    count = config.top_processes if args.count is None else args.count
    if count < 1 or count > 100:
        print("headroom: process count must be from 1 to 100", file=sys.stderr)
        return 2
    try:
        rows = processes_mod.top_processes(count)
        current = probe_mod.sample()
        total = current.memory.ram_total
    except Exception as exc:
        print(f"headroom: could not list processes: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(format_processes(rows, total))
    return 0


def cmd_scan(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config_path, logger, log_path
    top = config.scan_top if args.top is None else args.top
    if top < 1 or top > 500:
        print("headroom: --top must be from 1 to 500", file=sys.stderr)
        return 2
    mark_scan()
    try:
        try:
            result = scan_path(args.path, top)
        except OSError as exc:
            print(f"headroom: {exc}", file=sys.stderr)
            return 2
    finally:
        clear_scan()
    sys.stdout.write(format_scan(result))
    return 0


def cmd_clean(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config_path
    enabled = _parse_only(args.only, config.enabled_cleanup)
    groups, context = discover_groups(enabled, os.environ)
    recycle = default_recycle_backend()
    preview = build_preview(groups, recycle, context)
    sys.stdout.write(format_preview(preview, dry_run=args.dry_run))
    if log_path is not None:
        print(f"Log: {log_path}")
    if args.dry_run:
        return 0
    if not preview.has_work:
        print("Nothing to delete.")
        return 0
    if not confirm_interactive():
        print("Nothing deleted.")
        return 0
    stats = apply(groups, logger=logger, recycle_bin=recycle, context=context)
    if cleanup_should_sparkle(dry_run=bool(args.dry_run), freed_bytes=stats.freed_bytes):
        mark_sparkle()
    sys.stdout.write(format_stats(stats))
    return 0


def cmd_watch(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    where = str(config_path) if config_path is not None else "defaults"
    print(
        f"Headroom watch every {config.interval_seconds}s. Ctrl+C to stop. Config: {where}",
        flush=True,
    )
    print(
        f"RAM warning: above {config.ram_percent:g}% for {config.ram_sustained_seconds}s. "
        f"Disk warning: {', '.join(config.drives)} under "
        f"{config.disk_warn_free_bytes / (1024 ** 3):g} GB "
        f"(critical under {config.disk_critical_free_bytes / (1024 ** 3):g} GB).",
        flush=True,
    )
    if log_path is not None:
        print(f"Log: {log_path}", flush=True)
    lock = InstanceLock(default_lock_path())
    try:
        acquired = lock.acquire()
    except OSError as exc:
        print(f"headroom: could not create the single-instance lock: {exc}", file=sys.stderr)
        return 2
    if not acquired:
        print("Headroom is already running.")
        return 0
    store = _open_history(logger)
    tracker = _make_tracker(config, store, logger) if store is not None else None
    try:
        if args.once:
            run_watch_loop(config, iterations=1, logger=logger, tracker=tracker)
            return 0
        launch_pet_if_needed(config, once=False)
        lower_priority()
        session = Session()
        session.install_signals()
        try:
            if not args.no_tray:
                try:
                    run_tray(
                        config,
                        logger=logger,
                        stop_event=session.stop,
                        tracker=tracker,
                        adaptive=True,
                        track_deep=True,
                        session=session,
                    )
                    return 0
                except ImportError as exc:
                    logger.warning("%s", exc)
                except Exception:
                    logger.exception("tray stopped; continuing in the console")
                    if session.stop.is_set():
                        return 0
            print(
                "Interval slows on battery or when idle, and speeds up while RAM or disk is under pressure.",
                flush=True,
            )
            supervise(
                lambda: run_watch_loop(
                    config,
                    logger=logger,
                    stop_event=session.stop,
                    tracker=tracker,
                    adaptive=True,
                    track_deep=True,
                ),
                session.stop,
                logger,
            )
        finally:
            session.request_stop()
    finally:
        if store is not None:
            store.close()
        lock.release()
    return 0


def cmd_history(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config, config_path, logger, log_path
    store = _open_history_readonly()
    if store is None:
        return 0
    try:
        window = _hours_window(args.hours) if not args.since else parse_window(args.since)
        now = time.time()
        rows = store.samples_between(now - window, now + 1)
        shown = rows[-20:]
        packed = [
            (
                float(row["ts"]),
                float(row["ram_percent"]),
                float(row["commit_percent"]),
                tuple((drive, free) for drive, free, _total in store.disks_at(float(row["ts"]))),
            )
            for row in shown
        ]
        sys.stdout.write(
            format_history(packed, path=store.path, window_seconds=window, total=len(rows))
        )
    finally:
        store.close()
    return 0


def cmd_changes(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config, config_path, logger, log_path
    store = _open_history_readonly()
    if store is None:
        return 0
    try:
        window = parse_window(args.since)
        report = build_changes(store, now=time.time(), window_seconds=window)
        sys.stdout.write(format_changes(report))
    finally:
        store.close()
    return 0


def cmd_report(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del args, config_path, logger, log_path
    store = _open_history_readonly()
    if store is None:
        return 0
    try:
        report = build_report(store, config, now=time.time())
        text = format_report(report)
        extra = report_sections({"report": report})
    finally:
        store.close()
    sys.stdout.write(text)
    if extra:
        sys.stdout.write("\n")
        sys.stdout.write(extra if extra.endswith("\n") else extra + "\n")
    return 0


def cmd_self(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del args, config, config_path, logger, log_path
    try:
        current = read_self(interval=0.2)
        agent = read_agent(default_lock_path(), interval=0.2)
        pet = read_pet(state_dir(), interval=0.2)
    except Exception as exc:
        print(f"headroom: could not read process stats: {exc}", file=sys.stderr)
        return 2
    sys.stdout.write(format_self(current, agent, pet))
    return 0


def cmd_install(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del args, config_path, logger, log_path
    try:
        message = install_autostart(include_pet=bool(config.widget_autostart))
    except AutostartError as exc:
        print(f"headroom: {exc}", file=sys.stderr)
        return 2
    print(message)
    return 0


def cmd_uninstall(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del args, config, config_path, logger, log_path
    try:
        message = uninstall_autostart()
    except AutostartError as exc:
        print(f"headroom: {exc}", file=sys.stderr)
        return 2
    print(message)
    return 0


def cmd_analyze(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config, config_path, log_path
    roots = list(args.roots) if args.roots else default_roots()
    missing = [root for root in roots if not root.exists()]
    if missing:
        print("headroom: folder not found: " + ", ".join(str(item) for item in missing), file=sys.stderr)
        return 2
    now = time.time()
    directory = state_dir()
    mark_scan()
    try:
        analysis = scan_files(roots, now=now, skip_under=directory)
    finally:
        clear_scan()
    extra = scanner_records(roots, now=now)
    if extra:
        analysis.records = tuple(list(analysis.records) + extra)
    save_analysis(analysis, directory)
    sys.stdout.write(format_analysis(analysis))
    rules = load_merged_rules(directory)
    groups = group_suggestions(suggestions_for(analysis, rules, now=now, today=date.today()))
    sys.stdout.write(format_review(groups, archive=archive_directory(directory)))
    logger.info("analysis stored %s items under %s", len(analysis.records), directory)
    return 0


def cmd_profile_list(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del args, config, config_path, logger, log_path
    active = set(read_active_profiles())
    print("Starter profiles")
    names = list(PROFILE_NAMES) + [plugin.name for plugin in profile_plugins()]
    for name in names:
        mark = "active" if name in active else "available"
        print(f"  {name:<12} {mark}")
    if not active:
        print("None selected. headroom profile use student")
    return 0


def cmd_profile_use(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config, config_path, logger, log_path
    names = parse_profile_list(" ".join(args.names))
    saved = write_active_profiles(names)
    if saved:
        print("Using " + ", ".join(saved) + ". Your rules still override these.")
    else:
        print("Cleared starter profiles. Your own rules are unchanged.")
    return 0


def cmd_profile_show(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config, config_path, logger, log_path
    if args.name:
        sys.stdout.write(load_profile_text(args.name))
        if not load_profile_text(args.name).endswith("\n"):
            print()
        return 0
    active = read_active_profiles()
    print("Active profiles: " + (", ".join(active) if active else "none"))
    rules = load_merged_rules()
    if not rules:
        print("No rules yet.")
        return 0
    for rule in rules:
        print(f"[{rule.source}] {rule.text}")
    print("Last matching rule wins. User rules are listed after profiles.")
    return 0


def cmd_rules_show(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del args, config, config_path, logger, log_path
    text = read_user_text()
    if not text.strip():
        print("No personal rules yet. headroom rules add keep name: thesis")
        return 0
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    return 0


def cmd_rules_add(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config, config_path, logger, log_path
    append_user_text(" ".join(args.text))
    print("Added. Your rules override starter profiles.")
    return 0


def cmd_rules_paste(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config, config_path, logger, log_path
    if args.file is not None:
        try:
            text = args.file.read_text(encoding="utf-8-sig")
        except OSError as exc:
            print(f"headroom: could not read {args.file}: {exc}", file=sys.stderr)
            return 2
    else:
        if sys.stdin.isatty():
            print("Paste rules, then a line with only a period.", flush=True)
            lines: list[str] = []
            while True:
                try:
                    line = input()
                except EOFError:
                    break
                if line.strip() == ".":
                    break
                lines.append(line)
            text = "\n".join(lines)
        else:
            text = sys.stdin.read()
    append_user_text(text)
    print("Added. Your rules override starter profiles.")
    return 0


def cmd_review(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config, config_path, log_path
    directory = state_dir()
    analysis = load_analysis(directory)
    now = time.time()
    today = date.today()
    items, roots = collect_suggestions(directory, now=now, today=today)
    if analysis is None and not items:
        print("No analysis yet. Run headroom analyze. It only reads files.")
        return 0
    groups = group_suggestions(items)
    archive = archive_directory(directory)
    sys.stdout.write(format_review(groups, archive=archive))
    if not args.apply:
        return 0
    if not groups:
        return 0
    if not sys.stdin.isatty():
        print(
            "Refusing to move files without an interactive terminal. "
            "The list above is a dry run.",
            file=sys.stderr,
        )
        return 2
    chosen = _review_selection(args, groups)
    if not chosen:
        print("Nothing moved.")
        return 0
    records = [item.record for group in chosen for item in group.items]
    if args.each:
        records = [record for record in records if _approve_one(record.path)]
        if not records:
            print("Nothing moved.")
            return 0
    else:
        print(f"{len(records)} items selected.")
        try:
            answer = input(MOVE_WORD)
        except EOFError:
            print("Nothing moved.")
            return 0
        if not confirm_move(answer):
            print("Nothing moved.")
            return 0
    recycler = recycle_file if os.name == "nt" else (lambda _path: False)
    stats = move_records(records, roots, archive, recycle=recycler, state=directory)
    for record in records:
        logger.info("review move path=%s bytes=%s", record.path, record.size)
    sys.stdout.write(format_move_stats(stats))
    return 0


MOVE_WORD = "Type MOVE to move the selected files: "


def _review_selection(args, groups):
    if args.group:
        indexes: list[int] = []
        for value in args.group:
            indexes.extend(parse_group_selection(value, len(groups)))
        return [groups[index] for index in indexes]
    if args.each:
        return groups
    try:
        answer = input("Groups to move (numbers, all, or empty to cancel): ")
    except EOFError:
        return []
    indexes = parse_group_selection(answer, len(groups))
    return [groups[index] for index in indexes]


def _approve_one(path: str) -> bool:
    try:
        answer = input(f"Move {path}? Type MOVE or press Enter to skip: ")
    except EOFError:
        return False
    return confirm_move(answer)


def cmd_dashboard(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config_path, logger, log_path
    if args.port < 0 or args.port > 65535:
        print("headroom: port must be from 0 to 65535", file=sys.stderr)
        return 2
    token = new_token()
    serve_dashboard(
        "127.0.0.1",
        args.port,
        token,
        directory=state_dir(),
        interval_seconds=config.interval_seconds,
    )
    return 0


def cmd_dupes(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config, config_path
    roots = list(args.roots) if args.roots else default_roots()
    missing = [root for root in roots if not root.exists()]
    if missing:
        print("headroom: folder not found: " + ", ".join(str(item) for item in missing), file=sys.stderr)
        return 2
    mark_scan()
    try:
        groups, skipped = find_duplicates(roots)
    finally:
        clear_scan()
    sys.stdout.write(format_dupes(groups, skipped=skipped))
    from headroom.analyze import importance
    from headroom.plans import PlanItem, replace_plan

    now = time.time()
    plan_items = []
    for group in groups:
        for extra in group.extras:
            plan_items.append(
                PlanItem(
                    path=extra.path,
                    size=extra.size,
                    accessed=extra.accessed,
                    category="duplicates",
                    reason=f"duplicate of {group.keeper.path}",
                    source="dupes",
                )
            )
    directory = state_dir()
    replace_plan("dupes", roots, plan_items, directory)
    if not args.apply or not plan_items:
        return 0
    records = []
    for item in plan_items:
        from headroom.analyze import FileRecord

        records.append(
            FileRecord(
                path=item.path,
                size=item.size,
                accessed=item.accessed,
                category="duplicates",
                importance=importance("duplicates", item.accessed, now),
                base_category="duplicates",
            )
        )
    return _apply_move(records, roots, directory, logger)


def cmd_startup_list(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del args, config, config_path, logger, log_path
    sys.stdout.write(format_startup(default_catalog().list_entries()))
    return 0


def cmd_startup_disable(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config, config_path, logger
    catalog = default_catalog()
    if not sys.stdin.isatty():
        print(
            "Refusing to disable a startup entry without an interactive terminal. "
            "The list from headroom startup is only a preview.",
            file=sys.stderr,
        )
        return 2
    print(f"Disable {args.entry_id}. This can be restored with headroom startup restore.")
    try:
        answer = input("Type DISABLE to turn off this startup entry: ")
    except EOFError:
        print("Nothing changed.")
        return 0
    if answer.strip() != "DISABLE":
        print("Nothing changed.")
        return 0
    entry = catalog.disable(args.entry_id)
    append_backup(entry)
    print(f"Disabled {entry.name}. Backup saved. Restore with headroom startup restore {entry.id}")
    return 0


def cmd_startup_restore(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config, config_path, logger
    backed_up = load_backup()
    if args.entry_id:
        backed_up = [entry for entry in backed_up if entry.id == args.entry_id]
    if not backed_up:
        print("No startup backup matches that request.")
        return 0
    if not sys.stdin.isatty():
        print("Refusing to restore startup entries without an interactive terminal.", file=sys.stderr)
        return 2
    print(f"{len(backed_up)} startup entries will be turned back on.")
    try:
        answer = input("Type RESTORE to put the backed-up startup entries back: ")
    except EOFError:
        print("Nothing changed.")
        return 0
    if answer.strip() != "RESTORE":
        print("Nothing changed.")
        return 0
    catalog = default_catalog()
    restored: list[str] = []
    for entry in backed_up:
        catalog.restore(entry)
        restored.append(entry.id)
    remove_backup(set(restored))
    print("Restored " + ", ".join(restored) + ".")
    return 0


def cmd_downloads(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del config, config_path
    root = args.root if args.root is not None else default_downloads()
    if not root.exists():
        print(f"headroom: folder not found: {root}", file=sys.stderr)
        return 2
    now = time.time()
    items, skipped = classify_downloads(root, installed_program_names(), now=now)
    sys.stdout.write(format_downloads(items, skipped=skipped))
    from headroom.analyze import FileRecord, importance
    from headroom.plans import PlanItem, replace_plan

    suggested = [item for item in items if item.suggest]
    plan_items = []
    for item in suggested:
        if item.installed_match:
            reason = f"installer already run; matches {item.installed_match}"
            category = "installers"
        else:
            reason = f"download last opened {item.age_days} days ago ({item.kind}, {item.bucket})"
            category = "downloads" if item.kind != "installer" else "installers"
        plan_items.append(
            PlanItem(
                path=item.path,
                size=item.size,
                accessed=item.accessed,
                category=category,
                reason=reason,
                source="downloads",
            )
        )
    directory = state_dir()
    replace_plan("downloads", [root], plan_items, directory)
    if not args.apply or not plan_items:
        return 0
    records = [
        FileRecord(
            path=item.path,
            size=item.size,
            accessed=item.accessed,
            category=item.category,
            importance=importance(item.category, item.accessed, now),
            base_category=item.category,
        )
        for item in plan_items
    ]
    return _apply_move(records, [root], directory, logger)


def cmd_pet(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    """Open Roomy. A second launch shows the pet that is already running."""
    del config_path, logger, log_path
    chosen = getattr(args, "skin", None)
    if chosen and chosen not in load_skins():
        known = ", ".join(sorted(load_skins()))
        print(f"headroom: unknown skin {chosen}. Known skins: {known}", file=sys.stderr)
        return 2
    if send_control("show"):
        print("Roomy is already out.")
        return 0
    lock = InstanceLock(state_dir() / "pet.lock")
    try:
        acquired = lock.acquire()
    except OSError as exc:
        print(f"headroom: could not create the pet lock: {exc}", file=sys.stderr)
        return 2
    if not acquired:
        if send_control("show"):
            print("Roomy is already out.")
        else:
            print("Roomy is already running.")
        return 0
    try:
        run_pet(config, skin=chosen, lite=bool(getattr(args, "lite", False)))
    except ModuleNotFoundError:
        print("headroom: tkinter is required for the pet.", file=sys.stderr)
        return 2
    except Exception as exc:
        if exc.__class__.__name__ != "TclError":
            raise
        print(f"headroom: could not open the pet window: {exc}", file=sys.stderr)
        return 2
    finally:
        lock.release()
    return 0


def cmd_mcp(args, config: Config, config_path: Path | None, logger, log_path: Path | None) -> int:
    del args, config, config_path, logger, log_path
    try:
        from headroom.mcp_server import serve
    except ModuleNotFoundError:
        print(
            "headroom: the MCP package is missing. Reinstall with: pip install --upgrade headroom-pc",
            file=sys.stderr,
        )
        return 2
    serve()
    return 0


def _apply_move(records, roots, directory, logger) -> int:
    if not sys.stdin.isatty():
        print(
            "Refusing to move files without an interactive terminal. The list above is a dry run.",
            file=sys.stderr,
        )
        return 2
    print(f"{len(records)} items selected.")
    try:
        answer = input(MOVE_WORD)
    except EOFError:
        print("Nothing moved.")
        return 0
    if not confirm_move(answer):
        print("Nothing moved.")
        return 0
    recycler = recycle_file if os.name == "nt" else (lambda _path: False)
    stats = move_records(records, roots, archive_directory(directory), recycle=recycler, state=directory)
    if review_should_sparkle(archived=stats.archived, recycled=stats.recycled):
        mark_sparkle(directory)
    for record in records:
        logger.info("review move path=%s bytes=%s", record.path, record.size)
    sys.stdout.write(format_move_stats(stats))
    return 0


def parse_window(text: str) -> float:
    """Return a window length in seconds. A bare number is hours."""
    raw = text.strip().lower()
    aliases = {
        "yesterday": 86400.0,
        "day": 86400.0,
        "1d": 86400.0,
        "week": 7 * 86400.0,
        "lastweek": 7 * 86400.0,
        "7d": 7 * 86400.0,
    }
    if raw in aliases:
        return aliases[raw]
    unit = raw[-1:] if raw else ""
    number = raw
    scale = 3600.0
    if unit in {"h", "d"}:
        number = raw[:-1]
        scale = 3600.0 if unit == "h" else 86400.0
    try:
        value = float(number)
    except ValueError as exc:
        raise ConfigError(
            "time window must look like 24h, 7d, yesterday, or week"
        ) from exc
    if value <= 0:
        raise ConfigError("time window must be positive")
    return value * scale


def _hours_window(hours: float) -> float:
    if hours <= 0:
        raise ConfigError("--hours must be positive")
    return hours * 3600.0


def confirm_interactive(input_fn=None, isatty=None) -> bool:
    """Require the user to type YES on an interactive terminal."""
    tty = sys.stdin.isatty if isatty is None else isatty
    if not tty():
        print(
            "Refusing to delete without an interactive terminal. "
            "Preview with: headroom clean --dry-run",
            file=sys.stderr,
        )
        return False
    read = input if input_fn is None else input_fn
    try:
        answer = read(_CONFIRM)
    except EOFError:
        return False
    return answer.strip() == "YES"


def _parse_only(text: str | None, enabled: tuple[str, ...]) -> tuple[str, ...]:
    if text is None or text.strip() == "":
        return enabled
    parts = tuple(part.strip() for part in text.split(",") if part.strip())
    if not parts:
        raise ConfigError("--only did not name any targets")
    if len(set(parts)) != len(parts):
        raise ConfigError("--only lists a target more than once")
    unknown = [part for part in parts if part not in KNOWN_CLEANUP_IDS]
    if unknown:
        known = ", ".join(KNOWN_CLEANUP_IDS)
        raise ConfigError(
            "unknown cleanup target "
            + ", ".join(unknown)
            + f". Known targets: {known}"
        )
    disabled = [part for part in parts if part not in enabled]
    if disabled:
        raise ConfigError(
            "--only includes targets disabled in config: " + ", ".join(disabled)
        )
    return parts


def _open_history(logger) -> HistoryStore | None:
    path = default_history_path()
    try:
        return HistoryStore(path)
    except Exception:
        logger.exception("could not open history database %s", path)
        return None


def _open_history_readonly() -> HistoryStore | None:
    path = default_history_path()
    if not path.is_file():
        print("No history yet. Leave `headroom watch` running to record samples.")
        return None
    try:
        return HistoryStore(path)
    except Exception as exc:
        raise ConfigError(f"could not read history: {exc}") from exc


def _make_tracker(config: Config, store: HistoryStore, logger) -> Tracker:
    scanner = DeepScanner(
        tracking_roots(config),
        large_file_bytes=config.large_file_bytes,
        top_folders=config.scan_top,
    )
    return Tracker(store, config, logger=logger, scanner=scanner)


def _resolve_log_path(args, config: Config) -> Path | None:
    if args.no_log:
        return None
    if args.log is not None:
        return args.log
    if config.log_path is not None:
        return config.log_path
    return default_log_path()
