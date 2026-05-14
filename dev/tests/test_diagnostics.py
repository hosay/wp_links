"""Tests for dev.diagnostics — claude -p failure analysis."""

from unittest.mock import patch, MagicMock

import pytest

from dev.diagnostics import (
    build_diagnostic_prompt,
    run_diagnostic,
)


def test_build_diagnostic_prompt():
    prompt = build_diagnostic_prompt(
        account="editor1",
        edit_type="link_fix",
        error="TimeoutError on save",
        page_title="Ejemplo",
    )
    assert "editor1" in prompt
    assert "link_fix" in prompt
    assert "TimeoutError" in prompt
    assert "Ejemplo" in prompt


@patch("dev.diagnostics.subprocess.run")
def test_run_diagnostic_success(mock_run):
    mock_run.return_value = MagicMock(
        returncode=0,
        stdout="The issue is that the page is protected.",
        stderr="",
    )
    result = run_diagnostic("editor1", "link_fix", "Timeout", "Ejemplo")
    assert "protected" in result
    mock_run.assert_called_once()


@patch("dev.diagnostics.subprocess.run")
def test_run_diagnostic_timeout(mock_run):
    import subprocess
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=180)
    result = run_diagnostic("editor1", "link_fix", "Error", "Ejemplo")
    assert "timed out" in result.lower()


@patch("dev.diagnostics.subprocess.run")
def test_run_diagnostic_failure(mock_run):
    mock_run.side_effect = Exception("Process failed")
    result = run_diagnostic("editor1", "link_fix", "Error", "Ejemplo")
    assert "failed" in result.lower() or "error" in result.lower()
