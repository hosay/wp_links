"""WireGuard VPN manager.

Provides a context manager that activates/deactivates a WireGuard
VPN interface for the duration of a block, ensuring teardown on errors.
"""

import logging
import os
import subprocess
from contextlib import contextmanager

log = logging.getLogger(__name__)


def _interface_name(conf_path: str) -> str:
    """Extract interface name from config file path (filename without .conf)."""
    return os.path.splitext(os.path.basename(conf_path))[0]


def activate_vpn(conf_path: str) -> None:
    """Bring up a WireGuard interface from a .conf file."""
    log.info("Activating VPN: %s", conf_path)
    result = subprocess.run(
        ["wg-quick", "up", conf_path],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(f"wg-quick up failed: {result.stderr}")
    log.info("VPN activated: %s", _interface_name(conf_path))


def deactivate_vpn(conf_path: str) -> None:
    """Tear down a WireGuard interface."""
    log.info("Deactivating VPN: %s", conf_path)
    result = subprocess.run(
        ["wg-quick", "down", conf_path],
        capture_output=True, text=True, timeout=30,
    )
    if result.returncode != 0:
        log.warning("wg-quick down failed: %s", result.stderr)
    else:
        log.info("VPN deactivated: %s", _interface_name(conf_path))


@contextmanager
def vpn_session(conf_path: str):
    """Context manager that activates VPN on entry and deactivates on exit."""
    activate_vpn(conf_path)
    try:
        yield
    finally:
        deactivate_vpn(conf_path)
