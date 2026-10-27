from __future__ import annotations

from pathlib import PurePosixPath, PureWindowsPath

import pytest

from headroom.safety import (
    SafetyError,
    assert_contained,
    ensure_known_cleanup_id,
    validate_chrome_cache,
    validate_edge_cache,
    validate_firefox_cache,
    validate_recycle_root,
    validate_thumbnail,
    validate_user_temp,
    validate_windows_temp,
)

LOCAL = PureWindowsPath(r"C:\Users\me\AppData\Local")


def test_user_temp_accepts_windows_temp() -> None:
    validate_user_temp(PureWindowsPath(r"C:\Users\me\AppData\Local\Temp"))
    validate_user_temp(PureWindowsPath(r"D:\Temp"))
    validate_user_temp(PurePosixPath("/var/scratch/tmp"))


@pytest.mark.parametrize(
    "path",
    [
        PureWindowsPath(r"C:\\"),
        PureWindowsPath(r"C:\Users\me"),
        PureWindowsPath(r"C:\Users\me\Documents"),
        PureWindowsPath(r"C:\Users\me\Documents\Temp"),
        PureWindowsPath(r"C:\Users\me\Desktop\tmp"),
        PureWindowsPath(r"C:\Users\me\OneDrive - Personal\Temp"),
        PureWindowsPath(r"C:\Users\me\Downloads\Temp"),
        PurePosixPath("/"),
        PurePosixPath("/tmp"),
        PurePosixPath("/var/tmp"),
        PurePosixPath("/home/me/Documents/tmp"),
        PurePosixPath("/home/me"),
    ],
)
def test_user_temp_rejects_broad_and_protected_paths(path) -> None:
    with pytest.raises(SafetyError):
        validate_user_temp(path)


def test_windows_temp_is_exact() -> None:
    validate_windows_temp(
        PureWindowsPath(r"C:\Windows\Temp"),
        PureWindowsPath(r"C:\Windows"),
    )
    with pytest.raises(SafetyError):
        validate_windows_temp(
            PureWindowsPath(r"C:\Users\me\Temp"),
            PureWindowsPath(r"C:\Windows"),
        )
    with pytest.raises(SafetyError, match="protected"):
        validate_windows_temp(
            PureWindowsPath(r"D:\Documents\Windows\Temp"),
            PureWindowsPath(r"D:\Documents\Windows"),
        )
    with pytest.raises(SafetyError, match="Windows directory"):
        validate_windows_temp(
            PureWindowsPath(r"C:\Win\Temp"),
            PureWindowsPath(r"C:\Win"),
        )


def test_browser_caches_are_only_cache_directories() -> None:
    validate_chrome_cache(LOCAL / "Google" / "Chrome" / "User Data" / "Default" / "Cache", LOCAL)
    validate_chrome_cache(
        LOCAL / "Google" / "Chrome" / "User Data" / "Profile 1" / "Code Cache",
        LOCAL,
    )
    validate_edge_cache(LOCAL / "Microsoft" / "Edge" / "User Data" / "Default" / "GPUCache", LOCAL)
    validate_firefox_cache(
        LOCAL / "Mozilla" / "Firefox" / "Profiles" / "abc.default-release" / "cache2",
        LOCAL,
    )
    rejected = [
        LOCAL / "Google" / "Chrome" / "User Data" / "Default",
        LOCAL / "Google" / "Chrome" / "User Data" / "Default" / "Cookies",
        LOCAL / "Google" / "Chrome" / "User Data" / "Default" / "Login Data",
        LOCAL / "Google" / "Chrome" / "User Data" / "Default" / "Bookmarks",
        LOCAL / "Google" / "Chrome" / "User Data" / "Default" / "History",
        LOCAL / "Google" / "Chrome" / "User Data" / "Default" / "Cache" / "Cache_Data",
        PureWindowsPath(r"D:\Cache"),
    ]
    for path in rejected:
        with pytest.raises(SafetyError):
            validate_chrome_cache(path, LOCAL)
    with pytest.raises(SafetyError):
        validate_firefox_cache(
            LOCAL / "Mozilla" / "Firefox" / "Profiles" / "abc.default-release",
            LOCAL,
        )


def test_thumbnail_name_is_narrow() -> None:
    explorer = LOCAL / "Microsoft" / "Windows" / "Explorer"
    validate_thumbnail(explorer / "thumbcache_96.db", LOCAL)
    validate_thumbnail(explorer / "Thumbcache_idx.db", LOCAL)
    for name in ("iconcache_16.db", "desktop.ini", "thumbcache_96.db.exe", "notes.db"):
        with pytest.raises(SafetyError):
            validate_thumbnail(explorer / name, LOCAL)
    with pytest.raises(SafetyError):
        validate_thumbnail(LOCAL / "Microsoft" / "Windows" / "thumbcache_96.db", LOCAL)


def test_recycle_root_is_only_the_bin() -> None:
    validate_recycle_root(PureWindowsPath(r"C:\$Recycle.Bin"))
    validate_recycle_root(PureWindowsPath("D:/$Recycle.Bin"))
    with pytest.raises(SafetyError):
        validate_recycle_root(PureWindowsPath(r"C:\$Recycle.Bin\S-1-5-21"))
    with pytest.raises(SafetyError):
        validate_recycle_root(PureWindowsPath(r"C:\Users\me\$Recycle.Bin"))


def test_containment_is_lexical() -> None:
    root = PureWindowsPath(r"C:\Temp")
    assert_contained(root, PureWindowsPath(r"C:\Temp\a\b.txt"))
    assert_contained(root, PureWindowsPath(r"c:\temp\A.TXT"))
    with pytest.raises(SafetyError):
        assert_contained(root, root)
    with pytest.raises(SafetyError):
        assert_contained(root, PureWindowsPath(r"C:\Tempest\a"))
    with pytest.raises(SafetyError):
        assert_contained(root, PureWindowsPath(r"D:\Temp\a"))
    with pytest.raises(SafetyError, match=r"\.\."):
        assert_contained(root, PureWindowsPath(r"C:\Temp\..\Windows\notepad.exe"))


def test_unknown_cleanup_id() -> None:
    with pytest.raises(SafetyError, match="allowlist"):
        ensure_known_cleanup_id(r"C:\Users\me\Documents")
