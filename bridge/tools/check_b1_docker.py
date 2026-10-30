"""Desktop check of the B1 path (container disappears mid-trajectory) with real Docker, no API, no download.
The agent loop, LoggedLitellmModel, run_trajectory, the real DockerEnvironment (frozen public config), the real
DockerSnapshotter and the real docker_alive are used; only litellm.completion is replaced by the offline FakeAPI of
tests/test_end_to_end.py (scripted `echo` commands). Three trajectories on one local dev image:
  control  container stays up for all 50 calls             => not ENG_FAIL, container_running_at_stop True
  killed   `docker kill` from the host after call 3 (--rm)  => ENG_FAIL, CONTAINER_NOT_RUNNING, early abort
  expired  container_timeout shortened so the keepalive exits mid-run (the review's 2-hour case, compressed)
                                                            => ENG_FAIL, CONTAINER_NOT_RUNNING, early abort
plus docker_alive on a container id that does not exist => False. Run with the mini venv:
  .venv-mini/bin/python bridge/tools/check_b1_docker.py --instance pytest-dev__pytest-5631 --dataset-base SHA --out DIR"""
import argparse, json, os, subprocess, sys, time
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, "tests"))
import test_end_to_end as TE  # noqa: E402  (sets the offline cost map; FakeAPI, trajectory)
import identity as ID  # noqa: E402
import run_one as RO  # noqa: E402


def one(name, a, *, container_timeout=None, after_call=None):
    from minisweagent.environments.docker import DockerEnvironment
    from snapshot import DockerSnapshotter
    env_cfg = dict(TE.CFG["environment"], image=ID.image_name(a.instance), run_args=["--rm", "--pull", "never"])
    if container_timeout:
        env_cfg["container_timeout"] = container_timeout
    tdir = os.path.join(os.path.abspath(a.out), name); os.makedirs(tdir, exist_ok=True)
    snap = DockerSnapshotter(env_cfg["cwd"], os.path.join(tdir, "snap"), a.dataset_base,
                             keepalive=f"sleep {env_cfg.get('container_timeout', '2h')}")
    env = DockerEnvironment(**env_cfg)
    cid = env.container_id

    def hook(model):                                  # host-side action after the n-th API response
        orig = model._query
        def q(*x, **k):
            r = orig(*x, **k)
            if after_call and len(model.calls) == after_call[0]:
                after_call[1](cid)
            return r
        model._query = q
    t0 = time.time()
    rec, *_ = TE.trajectory("silent", ["echo hi"] * 60, tdir=tdir, env=env, snapshotter=snap,
                            alive_fn=lambda e: RO.docker_alive(e.container_id), model_hook=hook)
    for _ in range(30):                               # mini's cleanup stops/removes the container in the background
        still = subprocess.run(["docker", "ps", "-a", "-q", "--no-trunc", "--filter", f"id={cid}"], capture_output=True, text=True).stdout.strip()
        if not still:
            break
        time.sleep(2)
    return {"code": rec.get("code"), "why": rec.get("why"), "n_calls": rec.get("n_calls"),
            "container_running_at_stop": rec.get("container_running_at_stop"), "rule": rec.get("rule"),
            "seconds": round(time.time() - t0, 1), "container_left_behind": bool(still)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", required=True); ap.add_argument("--dataset-base", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    kill = lambda cid: subprocess.run(["docker", "kill", cid], capture_output=True)   # noqa: E731
    wait = lambda cid: time.sleep(25)                                                # noqa: E731  (keepalive is 15s)
    res = {"control": one("control", a),
           "killed": one("killed", a, after_call=(3, kill)),
           "expired": one("expired", a, container_timeout="15s", after_call=(3, wait)),
           "alive_nonexistent": RO.docker_alive("0" * 64)}
    res["pass"] = (res["control"]["code"] != "ENG_FAIL" and res["control"]["container_running_at_stop"] is True
                   and res["control"]["n_calls"] == ID.K
                   and all(res[k]["code"] == "ENG_FAIL" and "CONTAINER_NOT_RUNNING" in (res[k]["why"] or [])
                           and res[k]["container_running_at_stop"] is False and res[k]["n_calls"] < ID.K
                           for k in ("killed", "expired"))
                   and res["alive_nonexistent"] is False
                   and not any(res[k]["container_left_behind"] for k in ("control", "killed", "expired")))
    print(json.dumps(res, indent=1, default=str))


if __name__ == "__main__":
    main()
