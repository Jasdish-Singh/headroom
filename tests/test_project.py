from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_readme_explains_usage_and_safety() -> None:
    text = (ROOT / "README.md").read_text(encoding="utf-8")
    commands = (ROOT / "docs" / "commands.md").read_text(encoding="utf-8")
    for snippet in (
        "headroom status",
        "headroom top",
        "headroom clean --dry-run",
        "headroom watch",
        "headroom dashboard",
        "headroom mcp",
        "headroom pet",
        "headroom widget",
        "Roomy",
        "headroom.exe",
        "127.0.0.1",
        "allowlist",
        "Type YES",
        "Type MOVE",
        "MIT",
        "headroom-pc[tray]",
        "Jasdish-Singh/headroom",
    ):
        assert snippet in text
    for snippet in (
        "headroom scan",
        "headroom clean",
        "headroom install",
        "headroom history",
        "headroom changes",
        "headroom report",
        "headroom self",
        "headroom analyze",
        "headroom profile",
        "headroom review",
        "headroom rules",
        "headroom dupes",
        "headroom startup",
        "headroom downloads",
    ):
        assert snippet in commands
    assert "0.1.0" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert "security/advisories" in (ROOT / "SECURITY.md").read_text(encoding="utf-8")


def test_license_is_mit_for_jasdish_singh() -> None:
    text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    assert "MIT License" in text
    assert "Jasdish Singh" in text
    assert "2026" in text


def test_publishing_metadata_matches_the_package() -> None:
    import json

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    glama = json.loads((ROOT / "glama.json").read_text(encoding="utf-8"))
    smithery = (ROOT / "smithery.yaml").read_text(encoding="utf-8")
    project = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert server["name"] == "io.github.Jasdish-Singh/headroom"
    assert f"mcp-name: {server['name']}" in readme
    assert server["packages"][0]["registryType"] == "pypi"
    assert server["packages"][0]["identifier"] == "headroom-pc"
    assert server["packages"][0]["transport"]["type"] == "stdio"
    assert server["packages"][0]["packageArguments"][0]["value"] == "mcp"
    assert len(server["description"]) <= 100
    assert 'name = "headroom-pc"' in project
    assert 'version = "0.1.0"' in project
    assert server["version"] == "0.1.0"
    assert "type: stdio" in smithery
    assert "command: 'headroom'" in smithery
    assert "args: ['mcp']" in smithery
    assert glama["maintainers"] == ["Jasdish-Singh"]
    images = ROOT / "docs" / "images"
    for name in (
        "roomy-desktop.png",
        "roomy-zoom.png",
        "mood-happy.png",
        "mood-scan.png",
        "mood-dusty.png",
        "mood-slimy.png",
        "mood-sparkle.png",
        "mood-midnight-happy.png",
        "mood-tower-happy.png",
    ):
        blob = (images / name).read_bytes()
        assert blob.startswith(b"\x89PNG\r\n\x1a\n")
    release = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "workflow_dispatch" in release
    assert "AZURE_CLIENT_ID" in release
    assert "SIGNPATH_API_TOKEN" in release
    assert "mcp-publisher publish" not in release
    assert "twine" not in release


def test_workflow_runs_on_windows_and_ubuntu() -> None:
    text = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
    assert "ubuntu-latest" in text
    assert "windows-latest" in text
    assert "pytest" in text
    assert "3.11" in text
    assert "upload-artifact" in text
    assert "headroom.exe" in text
