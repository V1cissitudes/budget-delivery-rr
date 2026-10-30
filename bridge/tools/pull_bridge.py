"""Pull the bridge experiment's missing images under the author-approved disk rule (decision 2026-10-20, plan B):
  available = D free + reusable space inside the WSL vhdx (vhdx file size - WSL used bytes, floored at 0)
  reserve   = 4 x (new unique compressed bytes of this image) + 5 GB
  pull only if  available - reserve >= 100 GB  and  WSL free - reserve >= 80 GB ; otherwise STOP_DISK
  cumulative new unique compressed bytes <= cap (35 GB) ; otherwise STOP_NET_CAP
Layer lists come from the Docker Hub tags API (does not count as pulls). Layers of images already present locally count
as present. Rate-limited pulls are retried every 30 minutes (up to --max-waits times). Resumable: progress and pinned
local Ids are written after every image to --out in the format batch.py expects ({"images": {id: {"pinned_id": ...}}}).
Never deletes anything; no task is run. Not part of the bridge code identity (tools/).
Desktop: python bridge/tools/pull_bridge.py --selection bridge_dev/2026-10-18_bridge_selection_DRAFT.json \
           --out bridge_images.json [--cap-gb 35] [--dry]"""
import argparse, json, os, subprocess, sys, time, urllib.request

PS = "/mnt/c/Windows/System32/WindowsPowerShell/v1.0/powershell.exe"
VHDX = r"D:\WSL\Ubuntu-24.04\ext4.vhdx"
GB = 1e9


def image_name(iid):
    return "swebench/sweb.eval.x86_64." + iid.replace("__", "_1776_").lower() + ":latest"


def hub_layers(img, opener=urllib.request.urlopen):
    repo = img.split("/", 1)[1].split(":")[0]
    url = f"https://hub.docker.com/v2/namespaces/swebench/repositories/{repo}/tags/latest/images"
    x = [i for i in json.load(opener(url, timeout=30)) if i.get("architecture") == "amd64"][0]
    return {l["digest"]: l["size"] for l in x["layers"] if l.get("digest") and l.get("size")}


def disk_state():
    """(D free bytes, vhdx file bytes, WSL used bytes, WSL free bytes)."""
    ps = lambda c: float(subprocess.run([PS, "-NoProfile", "-Command", c], capture_output=True, text=True, timeout=60).stdout.strip())
    st = os.statvfs("/")
    return (ps("(Get-PSDrive D).Free"), ps(f"(Get-Item '{VHDX}').Length"),
            (st.f_blocks - st.f_bfree) * st.f_frsize, st.f_bavail * st.f_frsize)


def disk_ok(new_bytes, state):
    d_free, vhdx, used, wsl_free = state
    reusable = max(0.0, vhdx - used)
    reserve = 4 * new_bytes + 5 * GB
    ok = (d_free + reusable - reserve >= 100 * GB) and (wsl_free - reserve >= 80 * GB)
    return ok, {"D_free_gb": round(d_free / GB, 1), "vhdx_reusable_gb": round(reusable / GB, 1),
                "wsl_free_gb": round(wsl_free / GB, 1), "reserve_gb": round(reserve / GB, 1)}


def plan(ids, local, layers_of):
    """Order-preserving: for each missing image, its new bytes given everything present or pulled before it."""
    seen = set()
    for img in local:
        seen.update(layers_of(img))
    out = []
    for i in ids:
        img = image_name(i)
        if img in local:
            out.append((i, img, 0, True)); continue
        L = layers_of(img); new = sum(v for k, v in L.items() if k not in seen)
        seen.update(L); out.append((i, img, new, False))
    return out


def run(ids, local, layers_of, state_fn, pull_fn, inspect_fn, out_path, cap, max_waits=16, sleep=time.sleep, log=print):
    prev = json.load(open(out_path)) if os.path.exists(out_path) else {"net_unique_bytes": 0, "images": {}}
    res, net = dict(prev["images"]), prev["net_unique_bytes"]
    for i, img, new, have in plan(ids, local, layers_of):
        if res.get(i, {}).get("pinned_id"):
            continue
        if not have:
            if net + new > cap:
                res[i] = {"image": img, "status": "STOP_NET_CAP", "new_bytes": new}; _save(out_path, net, res); log("STOP_NET_CAP", i); return "STOP"
            ok, ds = disk_ok(new, state_fn())
            if not ok:
                res[i] = {"image": img, "status": "STOP_DISK", "new_bytes": new, **ds}; _save(out_path, net, res); log("STOP_DISK", i, ds); return "STOP"
            waits = 0
            while True:
                rc, err = pull_fn(img)
                if rc == 0:
                    break
                if "toomanyrequests" in (err or "").lower() and waits < max_waits:
                    waits += 1; log("rate limited; waiting 30 min", i, waits); sleep(1800); continue
                res[i] = {"image": img, "status": "PULL_FAILED", "err": (err or "")[:300]}; _save(out_path, net, res); log("PULL_FAILED", i); return "STOP"
            net += new
        res[i] = {"image": img, "pinned_id": inspect_fn(img), "new_bytes_counted": 0 if have else new, "t": time.time()}
        _save(out_path, net, res)
    log("done", len(res), "net GB", round(net / GB, 2)); return "END"


def _save(p, net, res):
    tmp = p + ".tmp"
    json.dump({"net_unique_bytes": net, "images": res}, open(tmp, "w"), indent=1); os.replace(tmp, p)


def _pull(img):
    for t in range(3):                                   # transient network errors
        r = subprocess.run(["docker", "pull", "-q", img], capture_output=True, text=True)
        if r.returncode == 0 or "toomanyrequests" in r.stderr.lower():
            return r.returncode, r.stderr
        time.sleep(60 * (t + 1))
    return r.returncode, r.stderr


def _inspect(img):
    return subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", img], capture_output=True, text=True).stdout.strip() or None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--selection", required=True); ap.add_argument("--out", required=True)
    ap.add_argument("--cap-gb", type=float, default=35.0); ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    sel = json.load(open(a.selection)); ids = list(sel["high_60"]) + list(sel["low_40"]) + list(sel["dev_5"])
    local = {l.strip().replace("docker.io/", "") + ":latest" for l in subprocess.run(
        ["docker", "image", "ls", "--format", "{{.Repository}}"], capture_output=True, text=True).stdout.splitlines() if "swebench" in l}
    cache = {}
    def layers_of(img):
        if img not in cache:
            cache[img] = hub_layers(img); time.sleep(0.3)
        return cache[img]
    if a.dry:
        p = plan(ids, local, layers_of); new = sum(x[2] for x in p)
        print(json.dumps({"missing": sum(1 for x in p if not x[3]), "new_unique_gb": round(new / GB, 2),
                          "disk_check_for_whole_batch": disk_ok(new, disk_state())}, indent=1)); return
    print(run(ids, local, layers_of, disk_state, _pull, _inspect, a.out, a.cap_gb * GB))


if __name__ == "__main__":
    main()
