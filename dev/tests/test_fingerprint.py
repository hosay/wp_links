"""Tests for dev.fingerprint — Camoufox fingerprint profile generation."""

import json
import os
import tempfile

import pytest

from dev.fingerprint import generate_fingerprint, generate_all_profiles, load_fingerprint


SAMPLE_USERNAMES = ["editor1", "editor2", "editor3"]


@pytest.fixture
def profiles_dir():
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


def test_generate_fingerprint_returns_valid_config():
    fp = generate_fingerprint("editor1")
    assert isinstance(fp, dict)
    assert "os" in fp
    assert fp["os"] in ("windows", "macos", "linux")
    assert "screen" in fp
    assert "width" in fp["screen"]
    assert "height" in fp["screen"]


def test_generate_fingerprint_deterministic_per_username():
    fp1 = generate_fingerprint("editor1")
    fp2 = generate_fingerprint("editor1")
    assert fp1 == fp2


def test_generate_fingerprint_different_per_username():
    fp1 = generate_fingerprint("editor1")
    fp2 = generate_fingerprint("editor2")
    # At least one field should differ (overwhelmingly likely with different seeds)
    assert fp1 != fp2


def test_generate_all_profiles_creates_files(profiles_dir):
    generate_all_profiles(SAMPLE_USERNAMES, profiles_dir)
    for username in SAMPLE_USERNAMES:
        fp_path = os.path.join(profiles_dir, username, "fingerprint.json")
        assert os.path.exists(fp_path)
        with open(fp_path) as f:
            data = json.load(f)
        assert "os" in data
        # Browser profile dir should also be created
        browser_dir = os.path.join(profiles_dir, username, "browser")
        assert os.path.isdir(browser_dir)


def test_load_fingerprint(profiles_dir):
    generate_all_profiles(["editor1"], profiles_dir)
    fp = load_fingerprint("editor1", profiles_dir)
    assert isinstance(fp, dict)
    assert "os" in fp


def test_load_fingerprint_auto_generates_missing(profiles_dir):
    """load_fingerprint should auto-generate the profile if missing on disk."""
    # No profile generated yet — should auto-create it
    fp = load_fingerprint("editor_missing", profiles_dir)
    assert isinstance(fp, dict)
    assert "os" in fp
    # File should now exist on disk
    fp_path = os.path.join(profiles_dir, "editor_missing", "fingerprint.json")
    assert os.path.exists(fp_path)
    # Browser dir should also exist
    browser_dir = os.path.join(profiles_dir, "editor_missing", "browser")
    assert os.path.isdir(browser_dir)
    # Result should match deterministic generation
    assert fp == generate_fingerprint("editor_missing")


def test_all_20_fingerprints_are_unique(profiles_dir):
    usernames = [f"editor{i}" for i in range(20)]
    generate_all_profiles(usernames, profiles_dir)
    fingerprints = []
    for u in usernames:
        fp = load_fingerprint(u, profiles_dir)
        fingerprints.append(json.dumps(fp, sort_keys=True))
    assert len(set(fingerprints)) == 20
