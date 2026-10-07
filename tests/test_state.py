from web_mcp.state import DomainMemory, TTLCache, normalize_url


class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_cache_expires_after_ttl():
    clock = Clock()
    c = TTLCache(3600, clock=clock)
    c.set("a", 1)
    clock.t += 3599
    assert c.get("a") == 1
    clock.t += 2
    assert c.get("a") is None


def test_cache_evicts_least_recently_used():
    c = TTLCache(3600, max_items=2)
    c.set("a", 1)
    c.set("b", 2)
    assert c.get("a") == 1  # a is now most recent
    c.set("c", 3)
    assert c.get("b") is None
    assert c.get("a") == 1 and c.get("c") == 3


def test_normalize_url():
    assert normalize_url("HTTPS://Example.COM/a?b=1#frag") == "https://example.com/a?b=1"
    assert normalize_url("https://example.com") == "https://example.com/"


def test_domain_memory_24h():
    clock = Clock()
    m = DomainMemory(24 * 3600, clock=clock)
    m.mark_blocked("www.Shop.example")
    assert m.is_blocked("shop.example")
    assert m.is_blocked("www.shop.example")
    assert not m.is_blocked("other.example")
    clock.t += 24 * 3600 - 1
    assert m.is_blocked("shop.example")
    clock.t += 2
    assert not m.is_blocked("shop.example")
