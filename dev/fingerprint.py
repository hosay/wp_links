"""Generate and persist distinct Camoufox fingerprint profiles.

Each username gets a deterministic but unique fingerprint config
(OS, screen resolution, WebGL renderer, locale, timezone).
"""

import hashlib
import json
import os
import random

# Common screen resolutions weighted toward popular sizes
SCREEN_RESOLUTIONS = [
    (1920, 1080), (1366, 768), (1536, 864), (1440, 900),
    (1280, 720), (1600, 900), (1280, 800), (1280, 1024),
    (1680, 1050), (1360, 768), (1920, 1200), (2560, 1440),
]

OS_OPTIONS = ["windows", "macos", "linux"]

# WebGL renderers that look realistic
WEBGL_CONFIGS = [
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 620, OpenGL 4.5)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) HD Graphics 630, OpenGL 4.5)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1650, OpenGL 4.5)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3060, OpenGL 4.5)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 580, OpenGL 4.5)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon(TM) Graphics, OpenGL 4.5)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Xe Graphics, OpenGL 4.5)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 770, OpenGL 4.5)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1060, OpenGL 4.5)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 2060, OpenGL 4.5)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6600, OpenGL 4.5)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) HD Graphics 530, OpenGL 4.5)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1050 Ti, OpenGL 4.5)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 730, OpenGL 4.5)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 5700 XT, OpenGL 4.5)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce GTX 1660, OpenGL 4.5)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) Iris(R) Plus Graphics 655, OpenGL 4.5)"),
    ("Google Inc. (NVIDIA)", "ANGLE (NVIDIA, NVIDIA GeForce RTX 3070, OpenGL 4.5)"),
    ("Google Inc. (AMD)", "ANGLE (AMD, AMD Radeon RX 6700 XT, OpenGL 4.5)"),
    ("Google Inc. (Intel)", "ANGLE (Intel, Intel(R) UHD Graphics 750, OpenGL 4.5)"),
]

# Spanish-speaking locales
LOCALES = ["es-ES", "es-MX", "es-CL", "es-CO", "es-AR", "es-PE"]

# Timezones matching the VPN countries
TIMEZONES = [
    "Europe/Madrid", "America/Mexico_City", "America/Santiago",
    "America/Bogota", "America/Buenos_Aires", "America/Lima",
]


def _seed_from_username(username: str) -> int:
    return int(hashlib.sha256(username.encode()).hexdigest(), 16) % (2**32)


def generate_fingerprint(username: str) -> dict:
    """Generate a deterministic fingerprint config for a given username."""
    rng = random.Random(_seed_from_username(username))

    screen = rng.choice(SCREEN_RESOLUTIONS)
    os_choice = rng.choice(OS_OPTIONS)
    webgl = rng.choice(WEBGL_CONFIGS)
    locale = rng.choice(LOCALES)
    tz = rng.choice(TIMEZONES)

    return {
        "os": os_choice,
        "screen": {"width": screen[0], "height": screen[1]},
        "webgl_config": {"vendor": webgl[0], "renderer": webgl[1]},
        "locale": locale,
        "timezone": tz,
        "firefox_user_prefs": {
            "media.peerconnection.enabled": False,  # block WebRTC leaks
        },
    }


def generate_all_profiles(usernames: list[str], profiles_dir: str) -> None:
    """Generate and persist fingerprint configs for all usernames."""
    for username in usernames:
        user_dir = os.path.join(profiles_dir, username)
        os.makedirs(user_dir, exist_ok=True)
        os.makedirs(os.path.join(user_dir, "browser"), exist_ok=True)

        fp = generate_fingerprint(username)
        fp_path = os.path.join(user_dir, "fingerprint.json")
        with open(fp_path, "w") as f:
            json.dump(fp, f, indent=2)


def load_fingerprint(username: str, profiles_dir: str) -> dict:
    """Load a fingerprint config, auto-generating if missing on disk."""
    fp_path = os.path.join(profiles_dir, username, "fingerprint.json")
    if not os.path.exists(fp_path):
        import logging
        logging.getLogger(__name__).warning(
            "Fingerprint missing for %s — generating on-the-fly", username
        )
        generate_all_profiles([username], profiles_dir)
    with open(fp_path) as f:
        return json.load(f)
