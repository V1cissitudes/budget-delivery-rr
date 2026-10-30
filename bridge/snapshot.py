"""Workspace snapshots for the bridge runner, using the sealed F (d1/extract.py, F-v6) so H/W products are defined
exactly as in K2/K3. DockerSnapshotter is desktop-only; LocalSnapshotter serves the offline tests.

F-b1 (prereg v4 §9, author-confirmed 2026-10-20 as a SECONDARY sensitivity product; F-v6 stays primary and its
snapshot is always taken first and kept unchanged):
  1. if the F-v6 snapshot is invalid because processes other than the keep-alive were running, record the process
     table, kill every non-PID-1 process in the container (`kill -9 -1`), wait 2 s, and take a second F-v6 extraction;
     the F-b1 base is that second extraction if no process remains before and after it, otherwise F-b1 is unavailable;
  2. from the base patch, drop the sections of NEW files (absent at t0) whose bytes are not valid UTF-8 (the K3 cause
     of 'patch not utf-8', e.g. sphinx objects.inv); every other section is kept byte for byte.
F-b1 changes the extraction and stopping rules; it is NOT a recovery of the original cut-off state."""
import hashlib, os, re, subprocess, sys, tarfile, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import identity as _ID  # noqa: E402
EX = _ID.import_d1("extract")      # sealed F; no bytecode is written into d1/, d1 is not left on sys.path


FB1_VERSION = "F-b1-v2"   # v2 (addendum 1, review C1-2): kill only on CONFIRMED stray processes; review m11 bump
_SEC = re.compile(rb"(?m)^(?=diff --git )")


def _is_new_file(section):
    head = section.split(b"\n@@", 1)[0].split(b"\nGIT binary patch", 1)[0]
    return b"\nnew file mode " in head


def _path(section):
    """Path of a NEW-file section from its `diff --git a/P b/P` header (both sides equal for a new file), so names that
    contain ' b/' or spaces are recovered exactly; git-quoted names (special characters) are returned as written.
    Only used for the audit list of dropped files (review C1-3)."""
    rest = section.split(b"\n", 1)[0].decode("utf-8", errors="replace")[len("diff --git "):]
    if rest.startswith('"'):
        return rest
    n, odd = divmod(len(rest) - 5, 2)
    if not odd and n > 0 and rest.startswith("a/") and rest[2 + n:5 + n] == " b/" and rest[2:2 + n] == rest[5 + n:]:
        return rest[2:2 + n]
    return rest


def drop_new_non_utf8(patch):
    """(filtered patch bytes, [dropped paths]). Pure function on F-v6 patch bytes (F-b1 rule 2)."""
    kept, dropped = [], []
    for sec in _SEC.split(patch):
        if not sec:
            continue
        if sec.startswith(b"diff --git ") and _is_new_file(sec):
            try:
                sec.decode("utf-8")
            except UnicodeDecodeError:
                dropped.append(_path(sec)); continue
        kept.append(sec)
    return b"".join(kept), dropped


def stray_confirmed(procs):
    """True only if both process checks SUCCEEDED (lists, not None) and at least one shows a non-keep-alive process."""
    return (isinstance(procs, (list, tuple)) and len(procs) == 2 and all(isinstance(p, list) for p in procs)
            and any(len(p) for p in procs))


def fb1_product(fv6_patch, fv6_valid, procs, resnap=None):
    """F-b1 from an F-v6 snapshot. resnap() -> (patch, valid, procs_after) is called only when F-v6 is invalid
    (stray processes); it must kill the processes and re-extract. Returns a dict with 'patch' (or None)."""
    out = {"version": FB1_VERSION, "killed": False, "procs_before_kill": None, "procs_after_kill": None}
    base = fv6_patch if fv6_valid else None
    if not fv6_valid:
        if not stray_confirmed(procs):          # review C1-2: a FAILED process check is not evidence of stray processes
            return dict(out, patch=None, valid=False,
                        reason="F-v6 invalid but stray processes not confirmed (process check failed); nothing killed")
        if resnap is None:
            return dict(out, patch=None, valid=False, reason="F-v6 invalid and no re-snapshot available")
        out.update(killed=True, procs_before_kill=procs)
        try:
            p2, ok2, after = resnap()
        except Exception as e:
            return dict(out, patch=None, valid=False, reason=f"re-snapshot failed: {type(e).__name__}: {e}")
        out["procs_after_kill"] = after
        if not ok2:
            return dict(out, patch=None, valid=False, reason="processes remained after kill")
        base = p2
    filtered, dropped = drop_new_non_utf8(base)
    return dict(out, patch=filtered, valid=True, dropped_new_non_utf8=dropped,
                sha256=hashlib.sha256(filtered).hexdigest(), same_as_fv6=fv6_valid and filtered == fv6_patch)


class DockerSnapshotter:
    def __init__(self, cwd, outdir, dataset_base, keepalive="sleep 2h"):
        self.cwd, self.outdir, self.base, self.keepalive = cwd, outdir, dataset_base, keepalive
        self.objdir = os.path.join(outdir, "objects"); self.t0_tree = None; self.n = 0
        os.makedirs(outdir, exist_ok=True)

    def _procs(self, cid):
        r = subprocess.run(["docker", "top", cid, "-eo", "pid,args"], capture_output=True, text=True, timeout=60)
        if r.returncode:
            return None
        rows = [l.split(None, 1)[1] if len(l.split(None, 1)) > 1 else "" for l in r.stdout.splitlines()[1:]]
        if self.keepalive in rows:
            rows.remove(self.keepalive)
        return rows

    def _copy(self, cid, dest):
        tarp = os.path.join(dest, "snap.tar")
        with open(tarp, "wb") as f:
            subprocess.run(["docker", "cp", f"{cid}:{self.cwd}", "-"], stdout=f, check=True, timeout=1800)
        with tarfile.open(tarp) as tf:
            tf.extractall(dest, filter="tar")
        os.remove(tarp)
        return os.path.join(dest, os.path.basename(self.cwd.rstrip("/")))

    def base_snapshot(self, env):
        """Before the agent's first step: image HEAD contract and the t0 tree all later F are diffed against."""
        with tempfile.TemporaryDirectory(dir=self.outdir) as d:
            repo = self._copy(env.container_id, d)
            hc = EX.head_vs_dataset_base(repo, self.base, os.path.join(self.outdir, "head_vs_base.diff"))
            if not hc["ok"]:
                raise RuntimeError(f"image HEAD vs dataset base: {hc['kind']}")
            self.t0_tree = EX.workspace_tree(repo, hc["head"], self.objdir)
        return {"t0_tree": self.t0_tree, "head_check": hc}

    def __call__(self, env):
        assert self.t0_tree, "base_snapshot() must run first"
        cid = env.container_id
        p1 = self._procs(cid)
        with tempfile.TemporaryDirectory(dir=self.outdir) as d:
            f = EX.extract(self._copy(cid, d), self.t0_tree, self.objdir)
        p2 = self._procs(cid)
        self.n += 1
        path = os.path.join(self.outdir, f"F_end_{self.n}.patch"); open(path, "wb").write(f["patch"])   # F-v6, kept as is
        valid = p1 == [] and p2 == []
        res = {"path": path, "sha256": f["sha256"], "valid": valid, "procs": [p1, p2], "flags": f["flags"]}
        res["b1"] = self._fb1(cid, f["patch"], valid, [p1, p2])
        return res

    def _fb1(self, cid, fv6_patch, valid, procs):
        """F-b1 is secondary: computing AND saving it are fully isolated, so no F-b1 failure can disturb the F-v6
        result returned above (review C1-1)."""
        try:
            b1 = fb1_product(fv6_patch, valid, procs, resnap=lambda: self._kill_and_resnap(cid))
            if b1.get("patch") is not None:
                path = os.path.join(self.outdir, f"F_b1_{self.n}.patch")
                with open(path, "wb") as fh:
                    fh.write(b1["patch"])
                b1["path"] = path
            return {k: v for k, v in b1.items() if k != "patch"}
        except Exception as e:
            return {"version": FB1_VERSION, "valid": False, "reason": f"F-b1 failed: {type(e).__name__}: {e}"}

    def _kill_and_resnap(self, cid):
        subprocess.run(["docker", "exec", cid, "sh", "-c", "kill -9 -1 2>/dev/null; true"], capture_output=True, timeout=60)
        time.sleep(2)
        q1 = self._procs(cid)
        with tempfile.TemporaryDirectory(dir=self.outdir) as d:
            f = EX.extract(self._copy(cid, d), self.t0_tree, self.objdir)
        q2 = self._procs(cid)
        return f["patch"], q1 == [] and q2 == [], [q1, q2]


class LocalSnapshotter:
    """Offline tests: env.config.cwd is a local git repo; F = binary diff of the working tree (incl. untracked) vs HEAD."""
    def __call__(self, env):
        cwd = env.config.cwd
        subprocess.run(["git", "-C", cwd, "add", "-A"], check=True, capture_output=True)
        p = subprocess.run(["git", "-C", cwd, "diff", "--cached", "--binary"], check=True, capture_output=True).stdout
        subprocess.run(["git", "-C", cwd, "reset", "-q"], check=True, capture_output=True)
        b1 = fb1_product(p, True, [[], []])
        return {"patch": p, "sha256": hashlib.sha256(p).hexdigest(), "valid": True,
                "b1": dict({k: v for k, v in b1.items() if k != "patch"}, patch_bytes=b1["patch"])}
