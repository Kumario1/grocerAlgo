import httpx

from router.calibrate import catalog_store_ids
from soak_heb import (
    accepted,
    prime_recovery_cache,
    probe_store,
    recovered_after_restart,
)


def test_soak_gate_requires_every_store_and_every_restart():
    stores = catalog_store_ids()
    healthy = {
        str(store): {"searches": 20, "search_ok": 19, "located": 5}
        for store in stores
    }

    assert accepted(
        healthy, {"boot-1", "boot-2", "boot-3"}, 2, {"boot-2", "boot-3"})
    healthy[str(stores[0])]["search_ok"] = 18
    assert not accepted(
        healthy, {"boot-1", "boot-2", "boot-3"}, 2, {"boot-2", "boot-3"})
    healthy[str(stores[0])]["search_ok"] = 19
    assert not accepted(
        healthy, {"boot-1", "boot-2"}, 2, {"boot-2"})
    assert not accepted(
        healthy, {"boot-1", "boot-2", "boot-3"}, 2, {"boot-2"})


def test_probe_counts_only_routable_products(monkeypatch):
    class Response:
        is_success = True
        status_code = 200
        headers = {}

        def __init__(self, body):
            self.body = body

        def json(self):
            return self.body

    class Client:
        def request(self, method, path, **kwargs):
            if method == "GET":
                return Response({"products": [
                    {"id": str(i), "name": str(i)} for i in range(5)
                ]})
            return Response({"products": [
                {"routable": i < 3} for i in range(5)
            ]})

    monkeypatch.setattr("soak_heb.QUERIES", ("milk",))
    stats = {
        "659": {
            "searches": 0, "search_ok": 0, "located": 0, "failures": [],
        },
    }

    probe_store(Client(), 659, stats, {})

    assert stats["659"]["located"] == 3
    assert "fewer than five routable placements" in stats["659"]["failures"]


def test_restart_recovery_treats_transient_disconnect_as_not_ready():
    class Client:
        def request(self, *_args, **_kwargs):
            raise httpx.ConnectError("restarting")

    assert not recovered_after_restart(
        Client(), (659,), {"Authorization": "Bearer secret"})


def test_restart_cache_prime_uses_a_fresh_query(monkeypatch):
    queries = []

    class Response:
        is_success = True
        status_code = 200
        headers = {}

    class Client:
        def request(self, _method, _path, **kwargs):
            queries.append(kwargs["params"]["q"])
            return Response()

    monkeypatch.setattr("soak_heb.time.time_ns", lambda: 123)

    assert prime_recovery_cache(Client(), (24, 659))
    assert queries == ["deployment-restart-123"] * 2
