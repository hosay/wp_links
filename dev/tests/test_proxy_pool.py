"""Tests for dev.data.proxy_pool — proxy rotation pool logic."""

import pytest

from dev.data.proxy_pool import PROXY_POOL, get_rotation_pool


class TestProxyPool:
    def test_pool_has_50_entries(self):
        assert len(PROXY_POOL) == 50

    def test_pool_entries_have_country(self):
        for i, proxy in enumerate(PROXY_POOL):
            assert "country" in proxy, f"Entry {i} missing 'country' key"

    def test_pool_entries_are_dicts(self):
        for proxy in PROXY_POOL:
            assert isinstance(proxy, dict)


class TestGetRotationPool:
    def test_current_proxy_is_first(self):
        current = {"country": "MX", "region": "jalisco", "city": "guadalajara"}
        pool = get_rotation_pool(current)
        assert pool[0] == current

    def test_default_max_attempts_is_10(self):
        current = {"country": "MX", "region": "jalisco", "city": "guadalajara"}
        pool = get_rotation_pool(current)
        assert len(pool) == 10

    def test_custom_max_attempts(self):
        current = {"country": "MX"}
        pool = get_rotation_pool(current, max_attempts=5)
        assert len(pool) == 5

    def test_current_proxy_not_duplicated(self):
        # Use a proxy that IS in the pool
        current = PROXY_POOL[0]
        pool = get_rotation_pool(current, max_attempts=50)
        count = sum(1 for p in pool if p == current)
        assert count == 1, f"Current proxy appears {count} times, expected 1"

    def test_empty_current_proxy_uses_pool_only(self):
        pool = get_rotation_pool({}, max_attempts=5)
        assert len(pool) == 5
        assert pool[0] != {}  # Should not start with empty dict

    def test_max_attempts_capped_at_pool_size_plus_one(self):
        current = {"country": "ZZ"}  # Not in pool
        pool = get_rotation_pool(current, max_attempts=100)
        assert len(pool) == 51  # current + all 50 pool entries

    def test_results_vary_across_calls(self):
        """Pool should be shuffled, so two calls should differ (probabilistically)."""
        current = {"country": "MX"}
        pool1 = get_rotation_pool(current, max_attempts=20)
        pool2 = get_rotation_pool(current, max_attempts=20)
        # With 50 entries and 19 random picks, collision is astronomically unlikely
        assert pool1[1:] != pool2[1:], "Two calls returned identical order — shuffle may be broken"
