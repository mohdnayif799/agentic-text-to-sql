"""Packaging and entry-point sanity.

These exist because of a bug that survived three rounds of "fresh clone"
testing: `scripts/build_database.py` still pointed at the deleted `src/`
directory, and every test passed anyway because a stale `pip install -e .`
in the system Python was shadowing the repo. It only surfaced in a real
virtualenv.

The lesson encoded here: verify that the code under test is the code in this
repo, and that each entry point can bootstrap its own imports.

A second lesson is encoded in `clean_subprocess_env()` (in conftest.py): the
first version of this file hand-built a POSIX environment dict, which replaced
the environment wholesale and broke on Windows. Sanitise by *removing* the
variables under test, never by constructing an environment from scratch.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest
from conftest import clean_subprocess_env

ROOT = Path(__file__).resolve().parents[1]

ENTRY_POINTS = [
    ROOT / "app.py",
    ROOT / "mcp_server.py",
    ROOT / "scripts" / "build_database.py",
    ROOT / "scripts" / "run_eval.py",
]


def test_imported_package_is_this_repo() -> None:
    """Guard against a stale global install shadowing the working tree."""
    import agentcrew

    resolved = Path(agentcrew.__file__).resolve()
    assert resolved == (ROOT / "agentcrew" / "__init__.py").resolve(), (
        f"agentcrew resolved to {resolved}, not this repo. A stale "
        "'pip install -e .' is shadowing the source tree."
    )


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_entry_point_has_no_stale_src_path(path: Path) -> None:
    """The `src/` layout is gone; nothing may still reference it."""
    text = path.read_text()
    assert '"src"' not in text, f"{path.name} still references the removed src/ dir"
    assert "src/agentcrew" not in text


@pytest.mark.parametrize("path", ENTRY_POINTS, ids=lambda p: p.name)
def test_entry_point_parses_and_bootstraps_path(path: Path) -> None:
    """Every entry point must add the repo root to sys.path itself.

    Entry points are run directly (`python scripts/run_eval.py`), not through
    pytest, so they cannot rely on the pythonpath set in pyproject.toml.
    """
    tree = ast.parse(path.read_text())
    inserts = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "insert"
        and isinstance(n.func.value, ast.Attribute)
        and n.func.value.attr == "path"
    ]
    assert inserts, f"{path.name} does not bootstrap sys.path"


@pytest.mark.parametrize(
    "path", [ROOT / "scripts" / "build_database.py", ROOT / "scripts" / "run_eval.py"],
    ids=["build_database", "run_eval"],
)
def test_script_runs_as_a_subprocess_without_pythonpath(
    path: Path, tmp_path: Path
) -> None:
    """The real check: run it the way a user would, with a clean environment.

    No PYTHONPATH, no reliance on pytest's config. This is what caught the
    stale-src bug.
    """
    args = [sys.executable, str(path)]
    env = clean_subprocess_env()
    probe_db = tmp_path / "packaging_probe.db"

    if path.name == "build_database.py":
        # Build into a temp file, never the shared database. Two reasons:
        # the script deletes its target first, and on Windows an open file
        # cannot be unlinked - the pytest process still holds pooled
        # SQLAlchemy handles on data/northstar.db from earlier tests, so this
        # failed with WinError 32. It is also simply wrong for a test to
        # destroy and rebuild shared state mid-session.
        env["AGENTCREW_DATABASE_PATH"] = str(probe_db)
    if path.name == "run_eval.py":
        args += ["--provider", "fake", "--ids", "q01", "--arm", "agent",
                 "--out", str(tmp_path / "r.json")]

    proc = subprocess.run(
        args,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=180,
        env=env,
    )
    assert proc.returncode == 0, (
        f"{path.name} failed with no PYTHONPATH:\n{proc.stderr[-1500:]}"
    )
    if path.name == "build_database.py":
        assert probe_db.exists(), (
            "build_database.py ignored AGENTCREW_DATABASE_PATH; it must honour "
            "the project's settings or it will overwrite the shared database."
        )


def test_packaging_probe_leaves_the_real_database_untouched(tmp_path: Path) -> None:
    """The subprocess test must never modify data/northstar.db.

    Guards the fix directly: if the seed script stops honouring
    AGENTCREW_DATABASE_PATH, this catches it before the shared database is
    destroyed.
    """
    real = ROOT / "data" / "northstar.db"
    if not real.exists():
        pytest.skip("real database not built")
    before = (real.stat().st_size, real.stat().st_mtime_ns)

    probe = tmp_path / "probe.db"
    env = clean_subprocess_env()
    env["AGENTCREW_DATABASE_PATH"] = str(probe)
    subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "build_database.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=180, env=env, check=True,
    )

    assert probe.exists(), "probe database was not created"
    assert (real.stat().st_size, real.stat().st_mtime_ns) == before, (
        "build_database.py modified the real database despite the override"
    )


class TestRequirements:
    """The dependency files must stay honest."""

    def test_requirements_txt_exists_and_pins_exactly(self) -> None:
        lines = [
            ln.strip()
            for ln in (ROOT / "requirements.txt").read_text().splitlines()
            if ln.strip() and not ln.strip().startswith("#")
        ]
        assert lines, "requirements.txt has no active pins"
        for ln in lines:
            assert "==" in ln, f"unpinned dependency: {ln!r}"

    def test_dev_requirements_include_runtime(self) -> None:
        text = (ROOT / "requirements-dev.txt").read_text()
        assert "-r requirements.txt" in text

    def test_every_pinned_version_matches_pyproject_range(self) -> None:
        """requirements.txt pins must satisfy the ranges in pyproject.toml.

        Two files declare dependencies, so drift is possible. This makes it
        impossible to pin a version pyproject forbids.
        """
        import tomllib

        with (ROOT / "pyproject.toml").open("rb") as fh:
            cfg = tomllib.load(fh)

        ranges: dict[str, str] = {}
        groups = [cfg["project"]["dependencies"]]
        groups += list(cfg["project"].get("optional-dependencies", {}).values())
        for group in groups:
            for spec in group:
                name = (
                    spec.split(">=")[0].split("==")[0].split("[")[0].strip().lower()
                )
                ranges.setdefault(name, spec)

        for ln in (ROOT / "requirements.txt").read_text().splitlines():
            ln = ln.strip()
            if not ln or ln.startswith("#") or "==" not in ln:
                continue
            name, version = ln.split("==")[0].strip().lower(), ln.split("==")[1].strip()
            assert name in ranges, f"{name} pinned in requirements.txt but absent from pyproject.toml"
            spec = ranges[name]
            if ">=" in spec:
                floor = spec.split(">=")[1].split(",")[0].strip()
                assert _ge(version, floor), f"{name}=={version} is below pyproject floor {floor}"
            if "<" in spec.split(">=")[-1]:
                ceiling = spec.split("<")[1].strip().rstrip('"')
                assert not _ge(version, ceiling), (
                    f"{name}=={version} violates pyproject ceiling <{ceiling}"
                )


def _ge(a: str, b: str) -> bool:
    def parts(v: str) -> tuple[int, ...]:
        out = []
        for chunk in v.split("."):
            digits = "".join(c for c in chunk if c.isdigit())
            out.append(int(digits) if digits else 0)
        return tuple(out)

    pa, pb = parts(a), parts(b)
    width = max(len(pa), len(pb))
    return pa + (0,) * (width - len(pa)) >= pb + (0,) * (width - len(pb))
