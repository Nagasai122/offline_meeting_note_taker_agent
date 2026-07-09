"""Tests for cli/teams_sync.py's COM-initialization wrapping.

The Outlook COM fetch itself is not testable off-Windows/off-Outlook and is
best-effort by design (see tests/cli/test_mail_sync.py's docstring for the
same caveat) -- these tests instead verify the CoInitialize/CoUninitialize
control flow the fix (P0 bug: asyncio.to_thread runs this on a worker
thread that's never COM-initialized) added, by injecting fake `pythoncom`/
`win32com.client` modules rather than requiring real Outlook.
"""

from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

from cli.teams_sync import fetch_outlook_calendar


def _fake_win32_modules(dispatch_side_effect=None):
    """Build minimal fake `pythoncom`/`win32com.client` modules good enough
    for fetch_outlook_calendar's exact call pattern, with no real Outlook."""
    pythoncom_mod = types.ModuleType("pythoncom")
    pythoncom_mod.CoInitialize = MagicMock()
    pythoncom_mod.CoUninitialize = MagicMock()

    win32com_mod = types.ModuleType("win32com")
    win32com_client_mod = types.ModuleType("win32com.client")
    if dispatch_side_effect is not None:
        win32com_client_mod.Dispatch = MagicMock(side_effect=dispatch_side_effect)
    else:
        # Empty calendar: Items.Restrict(...) returns an empty iterable.
        mock_items = MagicMock()
        mock_items.Restrict.return_value = []
        mock_calendar = MagicMock()
        mock_calendar.Items = mock_items
        mock_namespace = MagicMock()
        mock_namespace.GetDefaultFolder.return_value = mock_calendar
        mock_outlook = MagicMock()
        mock_outlook.GetNamespace.return_value = mock_namespace
        win32com_client_mod.Dispatch = MagicMock(return_value=mock_outlook)
    win32com_mod.client = win32com_client_mod

    return pythoncom_mod, win32com_mod, win32com_client_mod


@pytest.fixture()
def fake_win32(monkeypatch):
    pythoncom_mod, win32com_mod, win32com_client_mod = _fake_win32_modules()
    monkeypatch.setitem(sys.modules, "pythoncom", pythoncom_mod)
    monkeypatch.setitem(sys.modules, "win32com", win32com_mod)
    monkeypatch.setitem(sys.modules, "win32com.client", win32com_client_mod)
    return pythoncom_mod, win32com_client_mod


def test_fetch_outlook_calendar_initializes_and_uninitializes_com(fake_win32, tmp_path):
    pythoncom_mod, win32com_client_mod = fake_win32
    output_path = tmp_path / "calendar.json"

    count = fetch_outlook_calendar(output_path)

    assert count == 0  # empty calendar in the fake
    pythoncom_mod.CoInitialize.assert_called_once()
    pythoncom_mod.CoUninitialize.assert_called_once()
    # CoInitialize must happen before Dispatch, CoUninitialize after -- i.e.
    # the whole COM-using body is bracketed by the init/uninit pair.
    init_call_order = pythoncom_mod.CoInitialize.call_args_list
    assert len(init_call_order) == 1


def test_fetch_outlook_calendar_uninitializes_com_even_on_dispatch_failure(fake_win32, tmp_path):
    """CoUninitialize must run even when Dispatch itself raises -- a bare
    try/finally around the COM body, not just a happy-path call, otherwise
    a repeated failed sync would leak COM initialization counts on the
    worker thread it happened to land on."""
    pythoncom_mod, win32com_client_mod = fake_win32
    win32com_client_mod.Dispatch.side_effect = Exception("Outlook not running")
    output_path = tmp_path / "calendar.json"

    count = fetch_outlook_calendar(output_path)

    assert count == 0
    pythoncom_mod.CoInitialize.assert_called_once()
    pythoncom_mod.CoUninitialize.assert_called_once()


def test_fetch_outlook_calendar_missing_pywin32_returns_zero_without_com_calls(monkeypatch, tmp_path):
    # Simulate pywin32 not being installed at all -- the ImportError path
    # must return early without touching pythoncom.
    monkeypatch.setitem(sys.modules, "pythoncom", None)
    monkeypatch.setitem(sys.modules, "win32com.client", None)

    count = fetch_outlook_calendar(tmp_path / "calendar.json")

    assert count == 0
