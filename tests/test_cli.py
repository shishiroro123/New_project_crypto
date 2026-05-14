"""Smoke tests for CLI commands using typer.testing.

These don't make network calls; they just validate that command surfaces are
parseable, defaults plumbed through, and no command crashes on bare --help.
"""

from __future__ import annotations

from typer.testing import CliRunner

from crypto_bot.cli import app


def test_help_lists_all_commands():
    runner = CliRunner()
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for cmd in (
        "fetch",
        "backtest",
        "walkforward",
        "robustness",
        "run",
        "news",
        "status",
        "unhalt",
        "dashboard",
        "seed-demo",
    ):
        assert cmd in result.stdout, f"missing command: {cmd}"


def test_unhalt_on_unhalted_db_is_noop(tmp_path):
    runner = CliRunner()
    state = tmp_path / "state.sqlite"
    # Create the file via a status call first (initialises the schema).
    result = runner.invoke(app, ["status", "--state", str(state)])
    assert result.exit_code == 0
    result = runner.invoke(app, ["unhalt", "--state", str(state)])
    assert result.exit_code == 0
    assert "not halted" in result.stdout.lower()


def test_seed_demo_populates_state(tmp_path):
    runner = CliRunner()
    state = tmp_path / "demo.sqlite"
    result = runner.invoke(
        app,
        ["seed-demo", "--state", str(state), "--bars", "300", "--seed", "1"],
    )
    assert result.exit_code == 0, result.output
    assert state.exists()
    # Now status should show non-empty equity / trades.
    result = runner.invoke(app, ["status", "--state", str(state)])
    assert result.exit_code == 0
    assert "Latest equity" in result.output
