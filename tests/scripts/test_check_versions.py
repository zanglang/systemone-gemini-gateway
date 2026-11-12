"""Tests for scripts/check_versions.py (stdlib only; independent of the s1gw package)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "check_versions.py"
_spec = importlib.util.spec_from_file_location("check_versions", _SCRIPT)
assert _spec is not None and _spec.loader is not None
check_versions = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(check_versions)


def make_repo(root: Path, py: str = "0.1.0", app: str = "0.1.0", quote: str = '"') -> Path:
    (root / "pyproject.toml").write_text(
        f'[project]\nname = "systemone-gemini-gateway"\nversion = "{py}"\n', encoding="utf-8"
    )
    chart = root / "charts" / "systemone-gemini-gateway"
    chart.mkdir(parents=True)
    (chart / "Chart.yaml").write_text(
        "apiVersion: v2\nname: systemone-gemini-gateway\ntype: application\n"
        "version: 0.9.9\n"
        f"appVersion: {quote}{app}{quote}\n",
        encoding="utf-8",
    )
    return root


def run(tag: str, root: Path) -> int:
    return int(check_versions.main([tag, "--root", str(root)]))


def test_match_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(tmp_path)
    assert run("v0.1.0", tmp_path) == 0
    assert "OK" in capsys.readouterr().out


def test_tag_without_v_prefix_passes(tmp_path: Path) -> None:
    make_repo(tmp_path)
    assert run("0.1.0", tmp_path) == 0


def test_unquoted_app_version_passes(tmp_path: Path) -> None:
    make_repo(tmp_path, quote="")
    assert run("v0.1.0", tmp_path) == 0


def test_chart_version_is_not_compared(tmp_path: Path) -> None:
    # Chart `version:` (0.9.9 in the fixture) is independent of appVersion.
    make_repo(tmp_path)
    assert run("v0.1.0", tmp_path) == 0


def test_tag_mismatch_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(tmp_path)
    assert run("v0.2.0", tmp_path) == 1
    err = capsys.readouterr().err
    assert "pyproject.toml" in err and "Chart.yaml appVersion" in err


def test_pyproject_mismatch_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    make_repo(tmp_path, py="0.1.1")
    assert run("v0.1.0", tmp_path) == 1
    err = capsys.readouterr().err
    assert "pyproject.toml [project].version '0.1.1'" in err
    assert "Chart.yaml appVersion '0.1.0'" not in err.split("pyproject.toml version")[0]


def test_chart_appversion_mismatch_fails(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path, app="0.1.1")
    assert run("v0.1.0", tmp_path) == 1
    assert "Chart.yaml appVersion '0.1.1'" in capsys.readouterr().err


def test_pyproject_and_chart_agree_but_tag_differs(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    make_repo(tmp_path, py="0.3.0", app="0.3.0")
    assert run("v0.1.0", tmp_path) == 1
    assert "pyproject.toml version" not in capsys.readouterr().err


def test_missing_chart_is_an_error(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.0"\n', encoding="utf-8")
    assert run("v0.1.0", tmp_path) == 2
    assert "error:" in capsys.readouterr().err


def test_chart_without_appversion_is_an_error(tmp_path: Path) -> None:
    make_repo(tmp_path)
    chart = tmp_path / "charts" / "systemone-gemini-gateway" / "Chart.yaml"
    chart.write_text("apiVersion: v2\nversion: 0.1.0\n", encoding="utf-8")
    assert run("v0.1.0", tmp_path) == 2
