import threading
import time

import pytest

from cache import CacheManager


@pytest.fixture
def cache():
    cm = CacheManager(":memory:")
    yield cm
    cm.close()


def test_set_and_get(cache):
    cache.set("key", {"value": 42}, ttl=60)
    assert cache.get("key") == {"value": 42}


def test_get_missing_key(cache):
    assert cache.get("nonexistent") is None


def test_ttl_expiry(cache):
    cache.set("key", "value", ttl=-1)
    assert cache.get("key") is None


def test_overwrite(cache):
    cache.set("key", "first", ttl=60)
    cache.set("key", "second", ttl=60)
    assert cache.get("key") == "second"


def test_delete(cache):
    cache.set("key", "value")
    cache.delete("key")
    assert cache.get("key") is None


def test_cleanup_removes_only_expired(cache):
    cache.set("expired", "gone", ttl=-1)
    cache.set("valid", "kept", ttl=60)
    deleted = cache.cleanup()
    assert deleted == 1
    assert cache.get("valid") == "kept"
    assert cache.get("expired") is None


def test_stats(cache):
    cache.set("valid", "v", ttl=60)
    cache.set("expired", "v", ttl=-1)
    s = cache.stats()
    assert s["valid_entries"] == 1
    assert s["expired"] == 1
    assert s["total_entries"] == 2


def test_user_profile_roundtrip(cache):
    cache.ensure_user_profiles_table()
    assert cache.load_user_profile("123") is None
    cache.save_user_profile("123", "Faker#KR1")
    assert cache.load_user_profile("123") == "Faker#KR1"


def test_user_profile_overwrite(cache):
    cache.ensure_user_profiles_table()
    cache.save_user_profile("123", "Faker#KR1")
    cache.save_user_profile("123", "Caps#EUW")
    assert cache.load_user_profile("123") == "Caps#EUW"


def test_user_profiles_isolated_per_user(cache):
    cache.ensure_user_profiles_table()
    cache.save_user_profile("1", "PlayerOne#NA1")
    cache.save_user_profile("2", "PlayerTwo#EUW")
    assert cache.load_user_profile("1") == "PlayerOne#NA1"
    assert cache.load_user_profile("2") == "PlayerTwo#EUW"


def test_thread_safety_concurrent_writes(cache):
    errors = []

    def writer(i):
        try:
            cache.set(f"key{i}", i, ttl=60)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(30)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
    for i in range(30):
        assert cache.get(f"key{i}") == i


def test_thread_safety_mixed_reads_and_writes(cache):
    cache.set("shared", 0, ttl=60)
    errors = []

    def reader():
        try:
            for _ in range(50):
                cache.get("shared")
        except Exception as exc:
            errors.append(exc)

    def writer():
        try:
            for i in range(50):
                cache.set("shared", i, ttl=60)
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(5)]
    threads += [threading.Thread(target=writer) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
