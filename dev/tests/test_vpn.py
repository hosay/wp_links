"""Tests for dev.vpn — WireGuard VPN context manager."""

import subprocess
from unittest.mock import patch, MagicMock

import pytest

from dev.vpn import (
    _interface_name,
    activate_vpn,
    deactivate_vpn,
    vpn_session,
)


def test_interface_name_from_conf_path():
    assert _interface_name("/path/to/Mexico-1-MX-8.conf") == "Mexico-1-MX-8"
    assert _interface_name("wireguard_confs/Spain-2-ES-46.conf") == "Spain-2-ES-46"


@patch("dev.vpn.subprocess.run")
def test_activate_vpn_calls_wg_quick_up(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    activate_vpn("/path/to/Mexico-1-MX-8.conf")
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "wg-quick"
    assert args[1] == "up"
    assert "/path/to/Mexico-1-MX-8.conf" in args[2]


@patch("dev.vpn.subprocess.run")
def test_deactivate_vpn_calls_wg_quick_down(mock_run):
    mock_run.return_value = MagicMock(returncode=0)
    deactivate_vpn("/path/to/Mexico-1-MX-8.conf")
    mock_run.assert_called_once()
    args = mock_run.call_args[0][0]
    assert args[0] == "wg-quick"
    assert args[1] == "down"


@patch("dev.vpn.deactivate_vpn")
@patch("dev.vpn.activate_vpn")
def test_vpn_session_context_manager(mock_activate, mock_deactivate):
    conf = "/path/to/conf.conf"
    with vpn_session(conf):
        mock_activate.assert_called_once_with(conf)
        mock_deactivate.assert_not_called()
    mock_deactivate.assert_called_once_with(conf)


@patch("dev.vpn.deactivate_vpn")
@patch("dev.vpn.activate_vpn")
def test_vpn_session_deactivates_on_exception(mock_activate, mock_deactivate):
    conf = "/path/to/conf.conf"
    with pytest.raises(RuntimeError):
        with vpn_session(conf):
            raise RuntimeError("simulated failure")
    mock_deactivate.assert_called_once_with(conf)
