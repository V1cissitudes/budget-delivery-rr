"""Desktop check of the F-b1 stray-process path with real Docker (no API, no download): start a container from a local
dev image with the frozen public environment config, take the t0 base snapshot, leave a background process and a new
non-UTF-8 text file, then snapshot. Expect: F-v6 written and marked invalid (stray process); F-b1 killed the process,
re-extracted, dropped the non-UTF-8 new file. Run with the mini venv:
  .venv-mini/bin/python bridge/tools/check_fb1_docker.py --instance pytest-dev__pytest-5631 --dataset-base SHA --out DIR"""
import argparse, json, os, sys
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import identity as ID  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--instance", required=True); ap.add_argument("--dataset-base", required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    from minisweagent.environments.docker import DockerEnvironment
    from snapshot import DockerSnapshotter
    cfg = json.load(open(os.path.join(HERE, "config_public_gemini3flash.json")))
    env_cfg = dict(cfg["environment"], image=ID.image_name(a.instance), run_args=["--rm", "--pull", "never"])
    snap = DockerSnapshotter(env_cfg["cwd"], os.path.abspath(a.out), a.dataset_base, keepalive=f"sleep {env_cfg.get('container_timeout', '2h')}")
    env = DockerEnvironment(**env_cfg)
    try:
        base = snap.base_snapshot(env)
        env.execute({"command": "nohup sleep 600 >/dev/null 2>&1 &"})
        env.execute({"command": "printf 'hello\\nwor\\377ld\\n' > fb1_probe_nonutf8.txt && echo '# fb1' >> README.rst"})
        r = snap(env)
    finally:
        env.cleanup()
    out = {"t0_ok": bool(base.get("t0_tree")), "fv6_valid": r["valid"], "fv6_procs": r["procs"],
           "fv6_patch_exists": os.path.exists(r["path"]), "b1": r["b1"]}
    print(json.dumps(out, indent=1, default=str))


if __name__ == "__main__":
    main()
