"""WireGuard VPN manager.

Provides a context manager that activates/deactivates a WireGuard
VPN interface for the duration of a block, ensuring teardown on errors.

IMPORTANT: The VPN configs use AllowedIPs = 0.0.0.0/0 which routes ALL
traffic through the tunnel. To protect the SSH/LAN connection, we:
1. Detect the current default gateway before VPN activation
2. Add a static route for the SSH peer (or LAN subnet) via the original gateway
3. Bring up the VPN
4. On teardown, remove the static route
"""

import logging
import os
import re
import subprocess
from contextlib import contextmanager

log = logging.getLogger(__name__)


def _interface_name(conf_path: str) -> str:
    """Extract interface name from config file path (filename without .conf)."""
    return os.path.splitext(os.path.basename(conf_path))[0]


def _get_default_gateway() -> tuple[str, str] | None:
    """Get the current default gateway IP and interface.

    Returns (gateway_ip, interface) or None.
    """
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True, text=True, timeout=5,
        )
        # Parse: "default via 10.0.0.1 dev eth0 ..."
        match = re.search(r'default via (\S+) dev (\S+)', result.stdout)
        if match:
            return match.group(1), match.group(2)
    except Exception as e:
        log.warning("Could not detect default gateway: %s", e)
    return None


def _get_lan_subnet(interface: str) -> str | None:
    """Get the LAN subnet for a given interface (e.g. '10.0.0.0/24')."""
    try:
        result = subprocess.run(
            ["ip", "-o", "addr", "show", interface],
            capture_output=True, text=True, timeout=5,
        )
        # Parse: "2: eth0  inet 10.0.0.5/24 ..."
        match = re.search(r'inet (\d+\.\d+\.\d+)\.\d+/(\d+)', result.stdout)
        if match:
            return f"{match.group(1)}.0/{match.group(2)}"
    except Exception as e:
        log.warning("Could not detect LAN subnet: %s", e)
    return None


def _get_vpn_endpoint(conf_path: str) -> str | None:
    """Extract the VPN endpoint IP from a WireGuard config file."""
    try:
        with open(conf_path) as f:
            for line in f:
                match = re.match(r'Endpoint\s*=\s*(\d+\.\d+\.\d+\.\d+)', line.strip())
                if match:
                    return match.group(1)
    except Exception as e:
        log.warning("Could not read VPN endpoint from %s: %s", conf_path, e)
    return None


def _add_route(destination: str, gateway: str, interface: str) -> bool:
    """Add a static route to preserve connectivity."""
    result = subprocess.run(
        ["ip", "route", "add", destination, "via", gateway, "dev", interface],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode != 0:
        if "File exists" in result.stderr:
            log.debug("Route already exists: %s via %s", destination, gateway)
            return True
        log.warning("Failed to add route %s via %s: %s", destination, gateway, result.stderr)
        return False
    log.info("Added route: %s via %s dev %s", destination, gateway, interface)
    return True


def _del_route(destination: str, gateway: str, interface: str):
    """Remove a previously added static route."""
    result = subprocess.run(
        ["ip", "route", "del", destination, "via", gateway, "dev", interface],
        capture_output=True, text=True, timeout=5,
    )
    if result.returncode == 0:
        log.info("Removed route: %s via %s dev %s", destination, gateway, interface)
    else:
        log.debug("Could not remove route %s: %s", destination, result.stderr)


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
    """Context manager that activates VPN on entry and deactivates on exit.

    Preserves LAN/SSH connectivity by adding static routes before the VPN
    takes over the default route.
    """
    # Step 1: Capture current network state BEFORE VPN
    gw_info = _get_default_gateway()
    routes_added = []

    if gw_info:
        gw_ip, gw_iface = gw_info
        log.info("Current gateway: %s via %s", gw_ip, gw_iface)

        # Add route for the VPN endpoint itself (so VPN packets reach the server)
        vpn_endpoint = _get_vpn_endpoint(conf_path)
        if vpn_endpoint:
            if _add_route(f"{vpn_endpoint}/32", gw_ip, gw_iface):
                routes_added.append((f"{vpn_endpoint}/32", gw_ip, gw_iface))

        # Preserve the LAN subnet route
        lan_subnet = _get_lan_subnet(gw_iface)
        if lan_subnet:
            if _add_route(lan_subnet, gw_ip, gw_iface):
                routes_added.append((lan_subnet, gw_ip, gw_iface))
            log.info("LAN subnet preserved: %s via %s", lan_subnet, gw_ip)
    else:
        log.warning("Could not detect default gateway — VPN may break SSH!")

    # Step 2: Activate VPN
    try:
        activate_vpn(conf_path)
    except Exception:
        # Clean up routes if VPN failed to activate
        for dest, gw, iface in routes_added:
            _del_route(dest, gw, iface)
        raise

    try:
        yield
    finally:
        # Step 3: Deactivate VPN
        deactivate_vpn(conf_path)
        # Step 4: Clean up static routes (wg-quick down should restore routing,
        # but we clean up our explicit routes to be safe)
        for dest, gw, iface in routes_added:
            _del_route(dest, gw, iface)
