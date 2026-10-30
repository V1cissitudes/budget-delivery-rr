"""Offline tests for bridge/tools/pull_bridge.py and delete_images.py (fakes only; no Docker, no network, no PowerShell).
Run: python3 -I bridge/tests/test_tools.py"""
import json, os, sys
sys.dont_write_bytecode = True
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE); sys.path.insert(0, os.path.join(HERE, "..", "tools"))
import fakes as FK  # noqa: E402
import pull_bridge as PB  # noqa: E402
import delete_images as DI  # noqa: E402
GB = 1e9
LAYERS = {PB.image_name("a__a-1"): {"base": 10 * GB, "a": 2 * GB}, PB.image_name("b__b-2"): {"base": 10 * GB, "b": 3 * GB},
          PB.image_name("c__c-3"): {"base": 10 * GB, "c": 4 * GB}}


def roomy():
    return (300 * GB, 312 * GB, 300 * GB, 700 * GB)        # D free, vhdx, WSL used, WSL free


def test_disk_rule_counts_vhdx_reusable_space():
    ok, ds = PB.disk_ok(28 * GB, (144 * GB, 312.6 * GB, 310 * GB, 700 * GB))
    assert not ok and ds["vhdx_reusable_gb"] == 2.6                                  # today: no slack in the vhdx
    ok, ds = PB.disk_ok(28 * GB, (144 * GB, 312.6 * GB, 180 * GB, 700 * GB))          # after deleting ~130 GB inside WSL
    assert ok and ds["reserve_gb"] == 117.0
    assert not PB.disk_ok(1 * GB, (300 * GB, 0, 0, 80 * GB))[0]                      # WSL floor 80 GB still applies


def test_plan_dedups_layers_in_order():
    p = PB.plan(["a__a-1", "b__b-2"], {PB.image_name("c__c-3")}, LAYERS.get)
    assert [(x[0], x[2] / GB, x[3]) for x in p] == [("a__a-1", 2.0, False), ("b__b-2", 3.0, False)]


def _run(cap=100 * GB, state=roomy, pull=None, out=None, waits=None):
    out = out or os.path.join(FK.tmpdir(), "img.json"); log = []
    pulls = []
    def pf(img):
        pulls.append(img)
        return pull(img) if pull else (0, "")
    st = PB.run(["a__a-1", "b__b-2"], {PB.image_name("c__c-3")}, LAYERS.get, state, pf, lambda img: "sha256:" + img[-12:],
                out, cap, sleep=lambda s: (waits.append(s) if waits is not None else None), log=lambda *a: log.append(a))
    return st, json.load(open(out)), pulls


def test_pulls_and_pins_in_batch_format():
    st, res, pulls = _run()
    assert st == "END" and len(pulls) == 2 and abs(res["net_unique_bytes"] - 5 * GB) < 1
    assert all(v["pinned_id"].startswith("sha256:") for v in res["images"].values())


def test_net_cap_and_disk_stops_before_pulling():
    st, res, pulls = _run(cap=4 * GB)
    assert st == "STOP" and pulls == [PB.image_name("a__a-1")] and res["images"]["b__b-2"]["status"] == "STOP_NET_CAP"
    st, res, pulls = _run(state=lambda: (110 * GB, 0, 0, 700 * GB))                   # 110 - (4*2+5) < 100
    assert st == "STOP" and pulls == [] and res["images"]["a__a-1"]["status"] == "STOP_DISK"


def test_rate_limit_waits_then_resumes():
    seq = iter([(1, "toomanyrequests: rate limit"), (0, ""), (0, "")]); waits = []
    st, res, pulls = _run(pull=lambda img: next(seq), waits=waits)
    assert st == "END" and waits == [1800] and len(pulls) == 3
    out = os.path.join(FK.tmpdir(), "img.json")
    _run(pull=lambda img: (1, "connection reset"), out=out)
    st, res, pulls = _run(out=out)                                                    # resume after a failure
    assert st == "END" and all(v.get("pinned_id") for v in res["images"].values())


def test_delete_refuses_keep_and_running():
    keep = {PB.image_name("a__a-1")}
    for names, running in (([PB.image_name("a__a-1")], False), ([PB.image_name("z__z-9")], True)):
        try:
            DI.delete(names, keep, lambda: running, lambda n: (0, ""), log=lambda *a: None)
        except SystemExit:
            continue
        raise AssertionError("unsafe delete accepted")
    res = DI.delete([PB.image_name("z__z-9")], keep, lambda: False, lambda n: (1, "image is being used"), log=lambda *a: None)
    assert res[PB.image_name("z__z-9")]["rc"] == 1


if __name__ == "__main__":
    FK.run_tests(dict(globals()))
