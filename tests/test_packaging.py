"""Check what the wheel and sdist actually ship."""

import subprocess
import tarfile
import tomllib
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

pytestmark = pytest.mark.packaging


@pytest.fixture(scope="session")
def built(tmp_path_factory):
    """Build the wheel and sdist once, return their paths."""
    out = tmp_path_factory.mktemp("dist")
    result = subprocess.run(
        ["uv", "build", "--out-dir", str(out)],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"uv build failed:\n{result.stderr}")
    wheels = list(out.glob("*.whl"))
    sdists = list(out.glob("*.tar.gz"))
    assert len(wheels) == 1, wheels
    assert len(sdists) == 1, sdists
    return wheels[0], sdists[0]


@pytest.fixture(scope="session")
def wheel_names(built):
    with zipfile.ZipFile(built[0]) as zf:
        return zf.namelist()


@pytest.fixture(scope="session")
def sdist_names(built):
    with tarfile.open(built[1]) as tf:
        # Drop the "getflightplan-0.9.0/" prefix so paths are repo-relative.
        return [n.split("/", 1)[1] for n in tf.getnames() if "/" in n]


@pytest.fixture(scope="session")
def version():
    with open(ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def read_wheel(built, suffix):
    with zipfile.ZipFile(built[0]) as zf:
        name = next(n for n in zf.namelist() if n.endswith(suffix))
        return zf.read(name).decode()


def test_wheel_ships_install_assets(wheel_names):
    for asset in ("snippet.md", "registry-digest.md", "stop_hook.py"):
        assert f"flightplan/install_assets/{asset}" in wheel_names


def test_wheel_ships_every_module(wheel_names):
    for module in sorted(p.name for p in (ROOT / "src" / "flightplan").glob("*.py")):
        assert f"flightplan/{module}" in wheel_names


def test_wheel_excludes_repo_only_files(wheel_names):
    for name in wheel_names:
        assert not name.startswith("tests/"), name
        assert "CLAUDE.md" not in name, name
        assert ".claude" not in name, name


def test_wheel_metadata(built, version):
    metadata = read_wheel(built, "METADATA")
    assert "Name: getflightplan" in metadata
    assert f"Version: {version}" in metadata
    assert "License-Expression: Apache-2.0" in metadata


def test_wheel_entry_point(built):
    entry_points = read_wheel(built, "entry_points.txt")
    assert "getflightplan = flightplan.cli:main" in entry_points


def test_sdist_ships_source_and_tests(sdist_names):
    assert "src/flightplan/install_assets/snippet.md" in sdist_names
    assert "LICENSE" in sdist_names
    assert "CHANGELOG.md" in sdist_names
    assert any(n.startswith("tests/") for n in sdist_names)


def test_sdist_excludes_repo_only_files(sdist_names):
    for name in sdist_names:
        assert name != "CLAUDE.md", name
        assert name != ".flightplan.toml", name
        assert name != "uv.lock", name
        assert not name.startswith(".claude/"), name
        assert not name.startswith("scripts/"), name
