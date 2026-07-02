"""Tests for the performance metrics and caching helpers in metrics.py."""

from unittest.mock import MagicMock, patch

from custom_components.alexa_media.const import DOMAIN
from custom_components.alexa_media.metrics import (
    AlexaMetrics,
    BootMetrics,
    DataCache,
    get_metrics,
)

_MOD = "custom_components.alexa_media.metrics"


def _hass(data=None):
    """Build a MagicMock hass exposing a real dict ``.data``."""
    hass = MagicMock()
    hass.data = {} if data is None else data
    return hass


# --------------------------------------------------------------------------- #
# BootMetrics
# --------------------------------------------------------------------------- #
def test_boot_metrics_record_stage_uses_elapsed_monotonic():
    # start_time is set explicitly: its default_factory captured the real
    # time.monotonic at import, so patching would not affect it.
    boot = BootMetrics(start_time=100.0)
    with patch(f"{_MOD}.time.monotonic", return_value=103.5):
        boot.record_stage("login")
    assert boot.stages == {"login": 3.5}


def test_boot_metrics_record_stage_multiple_stages():
    boot = BootMetrics(start_time=10.0)
    with patch(f"{_MOD}.time.monotonic", side_effect=[12.0, 15.5]):
        boot.record_stage("first")
        boot.record_stage("second")
    assert boot.stages == {"first": 2.0, "second": 5.5}


def test_boot_metrics_get_summary_rounds_total_and_stages():
    boot = BootMetrics(start_time=100.0)
    boot.stages = {"login": 1.5, "setup": 2.25}
    with patch(f"{_MOD}.time.monotonic", return_value=105.5):
        summary = boot.get_summary()
    assert summary == {
        "total_time_seconds": 5.5,
        "stages": {"login": 1.5, "setup": 2.25},
    }


def test_data_cache_get_missing_key_returns_none():
    cache = DataCache()
    assert cache.get("nope") is None


def test_data_cache_set_then_get_within_ttl_returns_value():
    cache = DataCache(ttl_seconds=30.0)
    with patch(f"{_MOD}.time.monotonic", return_value=100.0):
        cache.cache_set("k", "v")
        assert cache.get("k") == "v"


def test_data_cache_expired_entry_is_evicted():
    cache = DataCache(ttl_seconds=30.0)
    with patch(f"{_MOD}.time.monotonic", side_effect=[100.0, 200.0]):
        cache.cache_set("k", "v")  # stored at t=100
        assert cache.get("k") is None  # t=200, 100s > ttl -> expired
    assert "k" not in cache._cache


def test_data_cache_set_evicts_oldest_when_full():
    cache = DataCache(max_entries=2)
    with patch(f"{_MOD}.time.monotonic", side_effect=[1.0, 2.0, 3.0]):
        cache.cache_set("k1", "v1")  # ts 1.0 (oldest)
        cache.cache_set("k2", "v2")  # ts 2.0 -> cache now full
        cache.cache_set("k3", "v3")  # full + new key -> evict oldest (k1)
    assert "k1" not in cache._cache
    assert set(cache._cache) == {"k2", "k3"}


def test_data_cache_set_existing_key_does_not_evict():
    cache = DataCache(max_entries=2)
    with patch(f"{_MOD}.time.monotonic", side_effect=[1.0, 2.0, 3.0]):
        cache.cache_set("k1", "v1")
        cache.cache_set("k2", "v2")  # full
        cache.cache_set("k1", "v1b")  # key already present -> overwrite, no evict
    assert set(cache._cache) == {"k1", "k2"}
    assert cache._cache["k1"][0] == "v1b"


def test_alexa_metrics_initial_state():
    hass = _hass()
    metrics = AlexaMetrics(hass)
    assert metrics.hass is hass
    assert metrics.boot_metrics is None
    assert isinstance(metrics.api_cache, DataCache)
    assert metrics.api_cache._ttl == 30.0
    assert metrics._api_calls == {}


def test_start_boot_tracking_creates_boot_metrics():
    metrics = AlexaMetrics(_hass())
    metrics.start_boot_tracking()
    assert isinstance(metrics.boot_metrics, BootMetrics)


def test_record_boot_stage_without_tracking_is_noop():
    metrics = AlexaMetrics(_hass())
    metrics.record_boot_stage("login")  # boot_metrics is None -> no-op
    assert metrics.boot_metrics is None


def test_record_boot_stage_delegates_to_boot_metrics():
    metrics = AlexaMetrics(_hass())
    metrics.start_boot_tracking()
    metrics.boot_metrics.start_time = 100.0
    with patch(f"{_MOD}.time.monotonic", return_value=104.0):
        metrics.record_boot_stage("login")
    assert metrics.boot_metrics.stages == {"login": 4.0}


def test_record_api_call_accumulates_count_and_duration():
    metrics = AlexaMetrics(_hass())
    metrics.record_api_call("ep", 1.5)
    metrics.record_api_call("ep", 0.5)
    assert metrics._api_calls["ep"] == (2, 2.0)


def test_get_metrics_returns_stored_instance():
    sentinel = object()
    hass = _hass({DOMAIN: {"metrics": sentinel}})
    assert get_metrics(hass) is sentinel


def test_get_metrics_none_when_domain_absent():
    assert get_metrics(_hass({})) is None


def test_get_metrics_none_when_metrics_key_absent():
    assert get_metrics(_hass({DOMAIN: {}})) is None
