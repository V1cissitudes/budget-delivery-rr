"""Delete the author-approved list of local images that the bridge experiment does not use (decision 2026-10-20,
plan B item 1). Safety: refuses while any container runs; never deletes an image in the keep set (the bridge's 105
tasks); removes by name without --force (an image still used by a container fails and is reported); logs every
result and the disk state before/after to --log. Not part of the bridge code identity (tools/).
Desktop: python bridge/tools/delete_images.py --delete-list bridge_dev/delete_list.txt \
           --selection bridge_dev/2026-10-18_bridge_selection_DRAFT.json --log bridge_dev/delete_log.json [--dry]"""
import argparse, json, subprocess, sys, time, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from pull_bridge import image_name, disk_state  # noqa: E402


def containers_running(run=subprocess.run):
    """review m10: a FAILED `docker ps` counts as "maybe running" (refuse), not as "nothing running"."""
    r = run(["docker", "ps", "-q"], capture_output=True, text=True)
    return r.returncode != 0 or r.stdout.strip() != ""


def keep_set(sel):
    return {image_name(i) for i in list(sel["high_60"]) + list(sel["low_40"]) + list(sel["dev_5"])}


def delete(names, keep, running_fn, rm_fn, log=print):
    if running_fn():
        raise SystemExit("containers are running; refusing to delete")
    bad = sorted(set(names) & keep)
    if bad:
        raise SystemExit(f"delete list contains images the bridge needs: {bad[:5]}")
    out = {}
    for n in names:
        rc, err = rm_fn(n)
        out[n] = {"rc": rc, "err": (err or "")[:200]}
        log(n, "ok" if rc == 0 else "FAILED")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete-list", required=True); ap.add_argument("--selection", required=True)
    ap.add_argument("--log", required=True); ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    names = [l.strip() for l in open(a.delete_list) if l.strip() and not l.startswith("#")]
    keep = keep_set(json.load(open(a.selection)))
    running = containers_running
    if a.dry:
        print(json.dumps({"n": len(names), "overlap_with_keep": sorted(set(names) & keep), "containers_running": running()})); return
    before = disk_state()
    res = delete(names, keep, running,
                 lambda n: (lambda r: (r.returncode, r.stderr))(subprocess.run(["docker", "image", "rm", n], capture_output=True, text=True)))
    after = disk_state()
    json.dump({"t": time.time(), "before": before, "after": after, "results": res}, open(a.log, "w"), indent=1)
    print("deleted", sum(1 for v in res.values() if v["rc"] == 0), "failed", sum(1 for v in res.values() if v["rc"]))


if __name__ == "__main__":
    main()
