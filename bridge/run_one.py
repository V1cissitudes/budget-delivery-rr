"""Run ONE bridge trajectory (DRAFT: not sealed; nothing may run on sampled tasks before the seal).
台式机用法（需要 Docker 与 API 密钥；由 batch.py 以子进程调用，也可单独调用）：
  python run_one.py --instance ID --arm ARM --dataset-base SHA --problem-file P.txt --image-id sha256:...
         --out DIR --spend spend.json --stop-usd 200 [--attempt ID] [--dev]
写出 DIR/record.json（运行记录，原子写入）、traj.json、calls.json（逐次调用的模型版本与用量）、
submission.patch（仅在主动提交时）、F_end.patch（截止时工作区的 F，仅在快照有效时）。
判分是另一步（evaluate.py，调用已封存的 d1/judge.py）。

核心逻辑在 run_trajectory()，与 Docker 无关；离线测试用 LocalEnvironment + LocalSnapshotter 调它。"""
import argparse, hashlib, json, os, sys, uuid
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")   # 成本表钉在已装的 litellm 版本上，运行时不联网拉取
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import identity as ID  # noqa: E402
from agent import BridgeAgent  # noqa: E402
from outcome import blind_submit, classify, commands_from_messages  # noqa: E402

RECORD_VERSION = "bridge-run-v4.2"


def f_bytes(snap):
    """F bytes from a snapshot dict, or None when extraction/snapshot failed or the snapshot is not provably
    consistent (DockerSnapshotter 'valid' = no stray process in the container before and after the copy)."""
    if not snap or snap.get("error") or not snap.get("valid"):
        return None
    if "patch" in snap:
        return snap["patch"]
    if snap.get("path") and os.path.exists(snap["path"]):
        return open(snap["path"], "rb").read()
    return None


def fb1_bytes(snap):
    """F-b1 bytes (secondary product, prereg v4 §9) or None. Independent of F-v6 validity by construction."""
    b1 = (snap or {}).get("b1") or {}
    if snap is None or snap.get("error") or not b1.get("valid"):
        return None
    if b1.get("patch_bytes") is not None:
        return b1["patch_bytes"]
    if b1.get("path") and os.path.exists(b1["path"]):
        return open(b1["path"], "rb").read()
    return None


def _fb1_part(snap, out):
    """F-b1 bytes, record metadata and the snapshot's b1 summary, fully isolated (addendum C1-1; panoramic ②): any
    error while reading, saving or summarising F-b1 only marks F-b1 unavailable and never stops record.json."""
    keys = ("version", "killed", "dropped_new_non_utf8", "same_as_fv6", "procs_before_kill", "procs_after_kill", "reason")
    try:
        b1 = (snap or {}).get("b1") or {}
        b1_summary = {k: v for k, v in b1.items() if k != "patch_bytes"}
        Fb1 = fb1_bytes(snap)
        if Fb1 is not None:
            ID.atomic_bytes(os.path.join(out, "F_b1.patch"), Fb1)
        meta = {"available": Fb1 is not None, "sha256": hashlib.sha256(Fb1).hexdigest() if Fb1 is not None else None,
                "bytes": len(Fb1) if Fb1 is not None else None, **{k: b1.get(k) for k in keys}}
        return meta, b1_summary
    except Exception as e:
        for f in ("F_b1.patch", "F_b1.patch.tmp"):
            try:
                os.remove(os.path.join(out, f))
            except Exception:
                pass
        return ({"available": False, "sha256": None, "bytes": None, "error": f"{type(e).__name__}: {e}"},
                {"error": f"{type(e).__name__}: {e}"})


def image_status(image_id_fn, expected, tries=3, wait_s=5, sleep=None):
    """'OK' / 'DRIFT' / 'CHECK_FAILED' (review m2): retry a failing check; only a different, successfully read Id is
    drift. image_id_fn may return None or raise when docker is briefly unavailable."""
    import time
    sleep = sleep or time.sleep
    seen = None
    for k in range(tries):
        try:
            seen = image_id_fn()
        except Exception:
            seen = None
        if seen is not None:
            return "OK" if seen == expected else "DRIFT"
        if k < tries - 1:
            sleep(wait_s)
    return "CHECK_FAILED"


def check_env(pins=None, version_fn=None):
    """Mismatches between the running agent environment and the frozen lock (review I3); [] = consistent.
    Every pinned distribution must be installed at exactly the pinned version."""
    from importlib.metadata import version as _v, PackageNotFoundError
    pins = pins if pins is not None else ID.env_lock_pins()
    version_fn = version_fn or _v
    bad = []
    for name, want in sorted(pins.items()):
        try:
            got = version_fn(name)
        except PackageNotFoundError:
            got = None
        except Exception:
            got = None
        if got != want:
            bad.append(f"{name}: lock {want}, installed {got}")
    return bad


def docker_alive(container_id):
    """True / False / None (check failed) for `docker inspect -f {{.State.Running}}` (review B1)."""
    import subprocess
    try:
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container_id], capture_output=True, text=True, timeout=60)
    except Exception:
        return None
    if r.returncode != 0:
        # Docker 29 prints "error: no such object: <id>" (lowercase); older clients "Error: No such object/container"
        return False if "no such" in (r.stderr or "").lower() else None
    return {"true": True, "false": False}.get(r.stdout.strip())


def run_trajectory(*, rec, out, model, env, snapshotter, task_text, agent_kwargs, spend=None, image_ok_after=None,
                   cleanup=True, alive_fn=None):
    """Run the agent, take F inside step() (BridgeAgent), clean up, classify, write record.json atomically.
    rec must carry instance_id, arm, attempt, k, cost_limit (and any identity fields)."""
    os.makedirs(out, exist_ok=True)
    agent, info, err, cleanup_err = None, {}, None, None
    try:
        if hasattr(snapshotter, "base_snapshot"):
            rec["base"] = snapshotter.base_snapshot(env)
        agent = BridgeAgent(model, env, arm=rec["arm"], snapshot_fn=snapshotter, spend=spend, alive_fn=alive_fn,
                            step_limit=rec["k"], cost_limit=rec["cost_limit"], **agent_kwargs)
        info = agent.run(task_text)
    except Exception as e:
        err = f"{type(e).__name__}: {e}"
    finally:
        if agent is not None:
            ID.atomic_json(os.path.join(out, "traj.json"), agent.serialize())
        if cleanup and env is not None and hasattr(env, "cleanup"):
            try:
                env.cleanup()
            except Exception as e:                       # recorded only; the snapshot was already taken
                cleanup_err = f"{type(e).__name__}: {e}"
    return finalize(rec, out, agent, info, err, model, image_ok_after, cleanup_err)


def finalize(rec, out, agent, info, err, model, image_ok_after=None, cleanup_err=None):
    stop = (agent.stop if agent else None) or {}
    snap = stop.get("snapshot") or {}
    F = f_bytes(snap)
    if F is not None:
        ID.atomic_bytes(os.path.join(out, "F_end.patch"), F)
    fb1_meta, b1_summary = _fb1_part(snap, out)
    exit_status = info.get("exit_status")
    submission = info.get("submission")
    if exit_status == "Submitted":
        ID.atomic_bytes(os.path.join(out, "submission.patch"), (submission or "").encode("utf-8"))
    why = []
    if err:
        why.append("EXCEPTION")
    if agent is not None and agent.eng_fail:
        why.append("STEP_EXCEPTION")
    if agent is not None and getattr(agent, "container_problem", None):
        why.append(agent.container_problem)        # review B1: container gone / state unknown at the stop => ENG_FAIL
    if exit_status is None:
        why.append("NO_EXIT_STATUS")
    if exit_status == "SpendStop":
        why.append("SPEND_STOP")
    if agent is not None and not all(r["consistent"] for r in agent.budget_log):
        why.append("BUDGET_DISPLAY_MISMATCH")
    if image_ok_after is not None:
        st = image_ok_after()
        st = {True: "OK", False: "DRIFT"}.get(st, st)          # bool (older callers) or "OK" / "DRIFT" / "CHECK_FAILED"
        if st == "DRIFT":
            why.append("IMAGE_DRIFT_DURING_RUN")
        elif st != "OK":
            why.append("IMAGE_CHECK_FAILED")                     # review m2: a failed check is reported separately
    pricing = getattr(model, "pricing_failure", None)
    if pricing:
        why.append("PRICING_UNAVAILABLE")
    code, rule = classify(exit_status=exit_status, submission=submission, limit_kind=stop.get("limit_kind"),
                          workspace_patch=F, eng_fail=bool(why))
    calls = list(getattr(model, "calls", None) or [])
    try:
        import model_log as ML
        summ = ML.summarize(calls)
    except ImportError:                                  # model_log needs mini; summaries are optional here
        summ = {"model_versions": [c.get("model_version") for c in calls], "n_api_responses": len(calls)}
    if calls:
        ID.atomic_json(os.path.join(out, "calls.json"), calls)
    rec.update({
        "record_version": RECORD_VERSION, "exit_status": exit_status, "error": err or (agent.eng_fail if agent else None),
        "cleanup_error": cleanup_err, "eng_fail": bool(why), "why": why, "resource_stop": exit_status == "SpendStop",
        "code": code, "rule": rule, "limit_kind": stop.get("limit_kind"),
        "n_calls": agent.n_calls if agent else None, "n_calls_at_stop": stop.get("n_calls"),
        "agent_cost_usd": agent.cost if agent else None,
        "container_running_at_stop": stop.get("container_running"),
        "stop": {"kind": stop.get("kind"), "limit_kind": stop.get("limit_kind"), "n_calls": stop.get("n_calls"),
                 "snapshot": {k: (v if k != "b1" else b1_summary) for k, v in snap.items() if k != "patch"}},
        "F": {"available": F is not None, "sha256": hashlib.sha256(F).hexdigest() if F is not None else None,
              "bytes": len(F) if F is not None else None},
        "F_b1": fb1_meta,
        "submission_sha256": hashlib.sha256((submission or "").encode("utf-8")).hexdigest()
                             if exit_status == "Submitted" else None,
        "budget_log": agent.budget_log if agent else None, **summ,
        "pricing_failure": pricing,
        "blind_submit": blind_submit(commands_from_messages(agent.messages)) if agent is not None and exit_status == "Submitted" else None,
        "finished": ID.now(), "terminal": True})
    ID.atomic_json(os.path.join(out, "record.json"), rec)
    return rec


def main():
    ap = argparse.ArgumentParser()
    for x in ("--instance", "--arm", "--dataset-base", "--problem-file", "--image-id", "--out", "--spend"):
        ap.add_argument(x, required=True)
    ap.add_argument("--stop-usd", type=float, required=True)
    ap.add_argument("--attempt", default=None)
    ap.add_argument("--dev", action="store_true")
    if "--check-env" in sys.argv:                         # review I3: batch.py runs this with --agent-python at start
        bad = check_env()
        print(json.dumps({"env_ok": not bad, "mismatches": bad}))
        raise SystemExit(0 if not bad else 3)
    a = ap.parse_args(); out = os.path.abspath(a.out); os.makedirs(out, exist_ok=True)
    bad = check_env()
    if bad:                                               # never reach the API with an unpinned environment
        raise SystemExit(f"agent environment differs from {ID.ENV_LOCK}: {bad[:5]}")
    J = ID.import_d1("judge")
    import minisweagent, litellm
    from importlib.metadata import version
    from minisweagent.environments.docker import DockerEnvironment
    from model_log import LoggedLitellmModel
    from snapshot import DockerSnapshotter
    from spend import Spend
    cfg = json.load(open(os.path.join(HERE, "config_public_gemini3flash.json")))
    registry, registry_sha = ID.load_registry()
    if not a.dev and not registry.get("confirmed_by_author"):
        raise SystemExit("pricing_registry.json is not confirmed by the author; a formal trajectory may not start")
    img = ID.image_name(a.instance)
    rec = {"instance_id": a.instance, "arm": a.arm, "attempt": a.attempt or uuid.uuid4().hex[:8], "k": ID.K,
           "cost_limit": ID.COST_LIMIT, "dev": a.dev, "image": img, "expected_image_id": a.image_id,
           "bridge_code_sha256": ID.bridge_code_sha256(), "d1_code_sha256": ID.d1_code_sha256(),
           "mini_version": minisweagent.__version__, "litellm_version": version("litellm"),
           "litellm_local_cost_map": os.environ.get("LITELLM_LOCAL_MODEL_COST_MAP"),
           "pricing_registry_sha256": registry_sha, "pricing_registry_confirmed": bool(registry.get("confirmed_by_author")),
           "pricing_registry": {k: registry.get(k) for k in ("confirmed_by_author", "versions", "relative_tolerance",
                                                              "provisional_usd_per_unpriced_call")},
           "started": ID.now()}
    pre = image_status(lambda: J.image_id(img), a.image_id)
    if pre != "OK":                                      # never pull; nothing is spent
        rec.update({"exit_status": None, "eng_fail": True, "why": ["IMAGE_MISSING_OR_DRIFT" if pre == "DRIFT" else "IMAGE_CHECK_FAILED"],
                    "code": "ENG_FAIL",
                    "rule": {"S": "unknown", "H": "unknown"}, "resource_stop": False, "terminal": True,
                    "finished": ID.now(), "F": {"available": False}, "model_versions": []})
        ID.atomic_json(os.path.join(out, "record.json"), rec); return
    env_cfg = dict(cfg["environment"], image=img, run_args=["--rm", "--pull", "never"])
    model = LoggedLitellmModel(call_log_path=os.path.join(out, "calls.json"), registry=registry, **cfg["model"])
    snap = DockerSnapshotter(env_cfg["cwd"], os.path.join(out, "snap"), a.dataset_base,
                             keepalive=f"sleep {env_cfg.get('container_timeout', '2h')}")
    env = None
    try:
        env = DockerEnvironment(**env_cfg)
    except Exception as e:
        rec.update({"env_error": f"{type(e).__name__}: {e}"})
    if env is None:
        finalize(rec, out, None, {}, rec["env_error"], model)
    else:
        run_trajectory(rec=rec, out=out, model=model, env=env, snapshotter=snap, task_text=open(a.problem_file).read(),
                       agent_kwargs=cfg["agent"], spend=Spend(a.spend, a.stop_usd, dev=a.dev, key_prefix=rec["attempt"]),
                       image_ok_after=lambda: image_status(lambda: J.image_id(img), a.image_id),
                       alive_fn=lambda e: docker_alive(e.container_id))
    print(json.dumps({k: rec.get(k) for k in ("instance_id", "arm", "attempt", "code", "n_calls", "model_versions")}))


if __name__ == "__main__":
    main()
