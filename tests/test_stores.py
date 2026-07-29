"""The picker: every onboarded store, and an honest reason when one cannot
place products. Plus the guards around spawning the onboarding pipeline."""
import glob
import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as app_module
from app import app
from router import calibrate
from router.calibrate import catalog_store_ids

client = TestClient(app)


@pytest.fixture(autouse=True)
def no_leftover_run(monkeypatch, tmp_path):
    """Each test starts with nothing being onboarded, whatever the box did."""
    monkeypatch.setattr(app_module, "ONBOARDING_STATE",
                        str(tmp_path / "onboarding.json"))
    monkeypatch.setattr(app.state, "onboarding", None, raising=False)


@pytest.fixture
def no_spawning(monkeypatch):
    """Records commands instead of running them.

    These endpoints launch hour-long agents and browser sessions. A test that
    reaches Popen for real does it on the developer's machine — one already
    did, against a live store.
    """
    commands = []

    class Fake:
        pid = 424242

        def poll(self):
            return None

    def fake_popen(command, **kwargs):
        commands.append(command)
        return Fake()

    monkeypatch.setattr(app_module.subprocess, "Popen", fake_popen)
    return commands


def stores():
    return {store["id"]: store for store in
            client.get("/api/stores").json()["stores"]}


def test_every_onboarded_store_is_listed():
    # Whatever has a built map profile, including a store onboarded five
    # minutes ago — the roster is not a constant in this file.
    built = {Path(path).parent.name for path in glob.glob("data/*/profile.npz")
             if Path(path).parent.name.isdigit()}

    assert set(stores()) == built
    assert {"24", "265", "269", "388", "659", "790"} <= built
    assert client.get("/api/stores").json()["default"] == "659"


def test_only_a_calibrated_store_is_offered_for_routing():
    listed = stores()

    assert listed["659"]["ready"] and listed["659"]["blocked_reason"] is None
    assert listed["659"]["name"] == "Lakeline H-E-B Plus! #659"
    # Ready means exactly one thing: a calibration that passed its gates. A
    # store that has not earned that is listed with what it is missing, never
    # offered with approximate placement.
    for store_id, store in listed.items():
        assert store["ready"] == (calibrate.load_calibration(store_id) is not None)
        assert store["ready"] != bool(store["blocked_reason"])


def test_public_catalog_matches_passing_calibrations():
    enabled = {
        store_id for store_id, store in stores().items()
        if store["catalog_enabled"]
    }

    assert enabled == {str(store) for store in catalog_store_ids()}
    for store_id, store in stores().items():
        assert store["catalog_enabled"] == (
            calibrate.load_calibration(store_id) is not None)


@pytest.mark.parametrize("store_id", sorted(stores()))
def test_a_store_that_cannot_place_products_says_so_and_refuses(store_id):
    listed = stores()[store_id]
    response = client.post(f"/api/products/locate?store={store_id}",
                           json={"products": []})

    if not listed["catalog_enabled"]:
        assert response.status_code == 409
        assert "not catalog-enabled" in response.json()["detail"]
    elif listed["ready"]:
        # Calibrated. The live session may still be on a different store,
        # which is a connection problem and names itself as one.
        assert response.status_code in (200, 503)
        if response.status_code == 503:
            assert f"Connect store #{store_id}" in response.json()["detail"]
    else:
        assert response.status_code == 409
        assert listed["blocked_reason"] in response.json()["detail"]


def test_store_names_fall_back_to_the_guide_the_store_was_built_from():
    assert stores()["265"]["name"] == "H-E-B #265 · Cedar Park"
    assert stores()["790"]["name"] == "H-E-B #790 · Plano"


def test_geometry_is_served_per_store():
    def aisles(store):
        body = client.get(f"/api/geometry?store={store}").json()
        return len([name for name in body["anchors"]
                    if name.startswith("AISLE ")])

    assert aisles("659") == 45
    assert aisles("24") == 43
    assert aisles("265") == 25


def test_an_unknown_store_is_not_a_path():
    assert client.get("/api/geometry?store=999").status_code == 404
    assert client.get("/api/geometry?store=../659").status_code == 404
    assert client.get("/api/geometry?store=659/../24").status_code == 404
    assert client.get("/api/walkability.png?store=..%2f659").status_code == 404


def test_walkability_serves_each_store_its_own_overlay():
    for store in ("659", "24"):
        response = client.get(f"/api/walkability.png?store={store}")

        assert response.status_code == 200
        assert response.content == Path(
            f"data/{store}/qa/walkable_overlay.png").read_bytes()


def test_free_text_routing_says_which_stores_have_a_directory():
    assert client.post("/api/route?store=659",
                       json={"items": ["milk"]}).status_code == 200
    refused = client.post("/api/route?store=24", json={"items": ["milk"]})

    assert refused.status_code == 422
    assert "no free-text directory" in refused.json()["detail"]


def test_status_for_another_store_does_not_disturb_the_live_session():
    body = client.get("/api/heb/status?store=24").json()

    # The session carries one store; reporting a different one as connected
    # would let its products be placed through the wrong building's catalog.
    assert body == {"connected": False, "map_ready": False, "store_id": 24}


@pytest.mark.parametrize("payload", [
    {"store": "12; rm -rf /"},
    {"store": "../659"},
    {"store": ""},
    {"store": "99999"},
    {"store": "1234", "city": "austin; ls"},
    {"store": "1234", "city": "../../etc"},
    {"store": "1234", "from": "0"},
    {"store": "1234", "from": "7"},
    {"store": "1234", "from": "3; rm -rf /"},
])
def test_onboarding_refuses_anything_that_is_not_a_store_number(payload):
    assert client.post("/api/stores/onboard", json=payload).status_code == 400


def test_onboarding_refuses_a_store_that_already_exists():
    response = client.post("/api/stores/onboard", json={"store": "659"})

    assert response.status_code == 409
    assert "already onboarded" in response.json()["detail"]


def running_run(monkeypatch, store="452"):
    """Hold the one slot with a process that never finishes."""
    class Running:
        pid = -1

        def poll(self):
            return None

    monkeypatch.setattr(app.state, "onboarding",
                        {"store": store, "process": Running(),
                         "log": f"logs/onboard-{store}.log"}, raising=False)


def test_only_one_run_at_a_time_but_a_second_store_gets_in_line(monkeypatch):
    """The machine runs one at a time; the person no longer has to come back
    in an hour and ask again."""
    running_run(monkeypatch)

    assert client.get("/api/stores/onboard").json()["running"] is True
    second = client.post("/api/stores/onboard",
                         json={"store": "1234", "city": "austin"})

    assert second.status_code == 200
    assert second.json()["store"] == "452"        # the slot is still 452's
    assert second.json()["queue"] == ["1234"]


@pytest.mark.parametrize("store", ["1234", "452"])
def test_a_store_already_spoken_for_does_not_get_a_second_place_in_line(
        monkeypatch, store):
    running_run(monkeypatch)
    client.post("/api/stores/onboard", json={"store": "1234"})

    again = client.post("/api/stores/onboard", json={"store": store})

    assert again.status_code == 409
    assert "already in line" in again.json()["detail"]


def test_a_finished_run_hands_the_slot_to_the_next_store_in_line(
        monkeypatch, tmp_path, no_spawning):
    """Nothing supervises the queue. The poll that notices a run ended is the
    same poll that starts the next one — so there is no worker to die quietly,
    and a server restarted mid-queue drains on the first GET."""
    state = tmp_path / "onboarding.json"
    state.write_text(json.dumps(
        {"current": {"store": "811", "pid": 2 ** 30,
                     "log": str(tmp_path / "gone.log"), "returncode": None},
         "queue": [{"store": "1234", "city": "austin", "from": None}]}))
    monkeypatch.setattr(app_module, "ONBOARDING_STATE", str(state))

    body = client.get("/api/stores/onboard").json()

    assert no_spawning == [["./pipeline.sh", "1234", "austin"]]
    assert body["running"] is True
    assert body["store"] == "1234"
    assert body["queue"] == []


def test_a_queued_store_that_cannot_start_does_not_blind_the_dialog(
        monkeypatch, tmp_path):
    """The drain runs inside a status poll, so a spawn that cannot happen has
    to move the line on — otherwise every poll from then on raises and the
    dialog that would have said so goes dark instead."""
    state = tmp_path / "onboarding.json"
    state.write_text(json.dumps(
        {"current": None, "queue": [{"store": "1234"}, {"store": "4321"}]}))
    monkeypatch.setattr(app_module, "ONBOARDING_STATE", str(state))

    def no_such_command(command, **kwargs):
        raise FileNotFoundError(2, "No such file or directory", command[0])

    monkeypatch.setattr(app_module.subprocess, "Popen", no_such_command)

    body = client.get("/api/stores/onboard").json()

    assert body["running"] is False
    assert body["store"] == "4321"        # the line drained through to the end
    assert body["queue"] == []
    assert "could not start the pipeline" in body["log"]


def test_a_queued_store_can_leave_the_line_without_killing_the_live_run(
        monkeypatch, tmp_path):
    state = tmp_path / "onboarding.json"
    state.write_text(json.dumps(
        {"current": {"store": "811", "pid": os.getpid(),
                     "log": str(tmp_path / "gone.log"), "returncode": None},
         "queue": [{"store": "1234"}, {"store": "24"}]}))
    monkeypatch.setattr(app_module, "ONBOARDING_STATE", str(state))

    body = client.delete("/api/stores/onboard?store=1234").json()

    assert body["queue"] == ["24"]
    assert body["running"] is True and body["store"] == "811"   # untouched
    # Naming a store only ever takes it out of the line, so a stray ?store=
    # cannot end an hour of agent time by looking like a cancel.
    assert client.delete("/api/stores/onboard?store=1234").status_code == 404
    assert client.delete("/api/stores/onboard?store=811").status_code == 404


def test_a_run_started_before_this_server_is_still_visible(monkeypatch, tmp_path):
    """An hour-long agent must not go invisible because uvicorn restarted."""
    log = tmp_path / "onboard-811.log"
    log.write_text("==> [3/5] onboarding agent\n")
    state = tmp_path / "onboarding.json"
    # A flat record, the shape written before there was a queue to write, and
    # os.getpid() stands in for the pipeline: a pid that is genuinely alive.
    state.write_text(json.dumps(
        {"store": "811", "pid": os.getpid(), "log": str(log)}))
    monkeypatch.setattr(app_module, "ONBOARDING_STATE", str(state))

    body = client.get("/api/stores/onboard").json()

    assert body["running"] is True
    assert body["store"] == "811"
    assert body["queue"] == []
    assert "onboarding agent" in body["log"]
    # ...and a second run does not start on top of it, it waits behind it.
    second = client.post("/api/stores/onboard", json={"store": "1234"})

    assert second.status_code == 200
    assert second.json() == {**body, "queue": ["1234"]}


def test_a_finished_run_from_a_previous_server_reads_as_finished(
        monkeypatch, tmp_path):
    state = tmp_path / "onboarding.json"
    state.write_text(json.dumps(
        {"store": "811", "pid": 2 ** 30, "log": str(tmp_path / "gone.log")}))
    monkeypatch.setattr(app_module, "ONBOARDING_STATE", str(state))

    assert client.get("/api/stores/onboard").json()["running"] is False
    assert client.delete("/api/stores/onboard").status_code == 409


def test_a_blocked_store_can_be_put_through_the_pipeline_again(no_spawning):
    """388 and 811 are onboarded but unroutable — rerunning is the fix."""
    assert client.post("/api/stores/onboard",
                       json={"store": "388", "city": "austin"}).status_code == 200
    assert no_spawning[0] == ["./pipeline.sh", "388", "austin"]


def test_a_run_can_resume_at_a_stage_instead_of_repeating_an_hour(no_spawning):
    """Stages 3 and 4 are ~50 min of agent time; a stage-5 failure must not
    charge for them twice."""
    response = client.post("/api/stores/onboard",
                           json={"store": "388", "city": "austin", "from": "6"})

    assert response.status_code == 200
    assert no_spawning[0] == ["./pipeline.sh", "388", "austin", "--from", "6"]


def test_how_a_run_ended_survives_the_server_that_started_it(
        monkeypatch, tmp_path):
    """A failed pipeline that reports nothing is indistinguishable from a hang
    — which is how store 811 read for a day."""
    state = tmp_path / "onboarding.json"
    state.write_text(json.dumps({"store": "811", "pid": 2 ** 30,
                                 "log": str(tmp_path / "gone.log"),
                                 "returncode": 1}))
    monkeypatch.setattr(app_module, "ONBOARDING_STATE", str(state))
    body = client.get("/api/stores/onboard").json()

    assert body["running"] is False
    assert body["returncode"] == 1


def test_a_finished_process_records_its_returncode_for_the_next_server(
        monkeypatch, tmp_path):
    state = tmp_path / "onboarding.json"
    monkeypatch.setattr(app_module, "ONBOARDING_STATE", str(state))

    class Finished:
        pid = 424242

        def poll(self):
            return 1

    monkeypatch.setattr(app.state, "onboarding",
                        {"store": "811", "process": Finished(),
                         "log": str(tmp_path / "onboard-811.log")},
                        raising=False)

    assert client.get("/api/stores/onboard").json()["returncode"] == 1
    assert json.loads(state.read_text())["current"]["returncode"] == 1


def test_live_label_verification_is_reachable_without_a_terminal(no_spawning):
    """The one step that can clear the margin gate, driven from the webapp."""
    response = client.post("/api/stores/811/verify")

    assert response.status_code == 200
    assert response.json()["store"] == "811"
    assert no_spawning[0][1:] == ["calibrate.py", "811", "--verify"]
    # it occupies the one long-job slot, so the log tail and Stop still apply
    assert client.get("/api/stores/onboard").json()["running"] is True


@pytest.mark.parametrize("store", ["999", "9999", "../659", "12; ls", ""])
def test_verification_refuses_anything_that_is_not_an_onboarded_store(
        store, no_spawning):
    assert client.post(f"/api/stores/{store}/verify").status_code in (404, 405)
    assert no_spawning == []


def test_stopping_nothing_is_not_an_error_worth_killing_a_process_over():
    assert client.delete("/api/stores/onboard").status_code == 409
    assert client.get("/api/stores/onboard").json() == {
        "running": False, "store": None, "returncode": None, "log": "",
        "queue": []}


# --- who may start one ----------------------------------------------------

def test_the_admin_token_is_the_whole_credential_once_it_is_set(
        monkeypatch, no_spawning):
    """A public deploy is one env var away from safe."""
    monkeypatch.setenv("GROCER_ADMIN_TOKEN", "s3cret")
    start = {"json": {"store": "1234"}}

    assert client.post("/api/stores/onboard", **start).status_code == 401
    assert client.post("/api/stores/onboard", **start,
                       headers={"Authorization": "Bearer wrong"}
                       ).status_code == 401
    assert client.post("/api/stores/onboard", **start,
                       headers={"Authorization": "s3cret"}).status_code == 401
    assert client.post("/api/stores/811/verify").status_code == 401
    assert client.delete("/api/stores/onboard").status_code == 401
    assert no_spawning == []
    # Reading stays public: the picker and a log tail are not secrets, and the
    # browser polls the tail every 2 s without a credential to hand it.
    assert client.get("/api/stores/onboard").status_code == 200
    assert client.get("/api/stores").status_code == 200

    started = client.post("/api/stores/onboard", **start,
                          headers={"Authorization": "Bearer s3cret"})

    assert started.status_code == 200
    assert no_spawning == [["./pipeline.sh", "1234"]]


def test_with_no_token_configured_only_this_machine_may_start_a_run(
        monkeypatch, no_spawning):
    """The zero-config trust model: on the box is the credential, which is why
    the whole suite above needs no header — and why a server put on a public
    address refuses instead of onboarding for strangers."""
    monkeypatch.delenv("GROCER_ADMIN_TOKEN", raising=False)
    remote = TestClient(app, client=("203.0.113.9", 5000))

    refused = remote.post("/api/stores/onboard", json={"store": "1234"})

    assert refused.status_code == 403
    assert "GROCER_ADMIN_TOKEN" in refused.json()["detail"]
    assert remote.post("/api/stores/811/verify").status_code == 403
    assert remote.delete("/api/stores/onboard").status_code == 403
    assert remote.get("/api/stores/onboard").status_code == 200
    assert no_spawning == []
