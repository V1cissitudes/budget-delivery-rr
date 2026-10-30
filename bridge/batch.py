"""批量驱动（草稿，未封存；封存前不在任何样本题上运行）。只用标准库，在装了 swebench 5.0.2 的 d1 环境里运行；
agent 一侧用单独的 mini-swe-agent 2.1.0 虚拟环境（--agent-python），以子进程调用 run_one.py。

- 顺序（草案 §3"运行顺序"，字面实现，见 README 待确认）：题目按 SHA256("D1-BRIDGE-ORDER-v1\\n"+id) 的十六进制
  升序排列；同一道题的各臂紧挨着跑，臂的先后取该摘要的整数值对 n! 取模，在规范臂序 prompts.ARMS 的全排列
  （itertools.permutations 的顺序）里取对应的那一个。
- 每一槽（题 × 臂）：先 run_one（agent 运行），紧接着 evaluate（判分），再决定是否重跑。
- ENG_FAIL 至多从头重跑 1 次（§4 规则 1）：旧目录移入 attempts/，历史写进 attempts/<id>.<arm>.history.json 与
  新结果的 attempt_history；中途崩溃、没有终态结果的目录同样算 1 次尝试。两次都失败 => 终态 ENG_FAIL，S/H/W 未知。
  支出停止线触发的 ENG_FAIL（resource_stop）不重跑。
- 续跑：已有 result.json 只有在可解析、终态、身份（bridge 代码哈希、d1 代码哈希、k、臂、题号、钉死镜像 Id、dev）
  全部一致、且不是"还能重跑的 ENG_FAIL"时才复用；否则移入 attempts/ 并计入历史。
- 账本 ledger.jsonl 记录每一次运行（开始、结束、代码、S/H/W、模型版本串、重跑、跳过、停止）。
- 支出停止线：每条轨迹开始前查 spend.py 的全局账本，到线即停；轨迹内部由 BridgeAgent 在每次请求前再查。
  未完成的槽位没有 result.json，分析时记未知。
- 不拉镜像：本地镜像 Id 与钉死的 Id 不一致就跳过该槽（不消耗尝试次数），记 IMAGE_NOT_READY。
台式机用法：
  python batch.py --selection docs/prereg/2026-10-18_bridge_selection_DRAFT.json --tasks-meta meta.json
      --images images.json --out OUT --spend spend.json --stop-usd 200 --week-state week.json
      --agent-python /path/to/mini-venv/bin/python [--arms silent,neutral,number,bats] [--dev]"""
import argparse, glob, hashlib, itertools, json, os, shutil, signal, subprocess, sys, time, uuid
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import identity as ID  # noqa: E402
import prompts as P  # noqa: E402
from spend import Spend, reconcile  # noqa: E402

ORDER_SALT = "D1-BRIDGE-ORDER-v1\n"
BATCH_VERSION = "bridge-batch-v4.1"
ENG_STREAK_STOP = 3       # consecutive ENG_FAIL attempts (not resource stops) => pause the batch (v4 §7.4)
JUDGE_STREAK_STOP = 3     # consecutive judged products with an infrastructure-unknown verdict => pause (addendum 2, I1)
INFRA_REASONS = ("INFRA_UNRECOVERED", "JUDGE_EXCEPTION")   # d1 judge / bridge evaluate reasons that are infrastructure
RUN_TIMEOUT_S = 105 * 60  # wall-clock limit of one run_one subprocess, below the 2 h container keep-alive (review m1)
MIN_FREE_BYTES = 20e9     # free space required in the output filesystem before every launch (review m8)


def order_digest(iid):
    return hashlib.sha256((ORDER_SALT + iid).encode()).hexdigest()


def task_order(ids):
    return sorted(ids, key=order_digest)


def arm_order(iid, arms):
    canon = [a for a in P.ARMS if a in arms]
    assert len(canon) == len(set(arms)) == len(arms), arms
    perms = list(itertools.permutations(canon))
    return list(perms[int(order_digest(iid), 16) % len(perms)])


def schedule(ids, arms):
    return [(iid, arm) for iid in task_order(ids) for arm in arm_order(iid, arms)]


def selection_ids(sel, dev):
    return list(sel["dev_5"]) if dev else list(sel["high_60"]) + list(sel["low_40"])


def active(history):
    """Attempts that count toward the 2-attempt cap: voided ones (systematic-failure streaks, v4 §7.4) do not."""
    return [h for h in (history or []) if not h.get("voided")]


def voided_attempts(history):
    return {h.get("attempt") for h in (history or []) if h.get("voided") and h.get("attempt")}


def counts_as_attempt(res, history):
    """A terminal result counts toward the cap only if it came from a real attempt that has not been voided (the
    synthetic TWO_ATTEMPTS_NO_RESULT record and voided attempts do not; addendum 1, review C2-4)."""
    return bool(res) and res.get("attempt") is not None and res.get("attempt") not in voided_attempts(history)


def rerun_eligible(res, history=None):
    """ENG_FAIL (not a resource stop) with fewer than 2 counted attempts. The slot's history FILE is authoritative when
    given (it carries voids); otherwise the history embedded in the result is used."""
    hist = res.get("attempt_history") if history is None else history
    used = len(active(hist)) + (1 if counts_as_attempt(res, hist) else 0)
    return res.get("code") == "ENG_FAIL" and not res.get("resource_stop") and used < 2


STREAK_STATE = "streak_state.json"


def load_streak(out):
    p = os.path.join(out, STREAK_STATE)
    st = json.load(open(p)) if os.path.exists(p) else {}
    for k, v in (("streak", []), ("tripped", None), ("resolutions", []), ("judge_streak", 0), ("judge_tripped", None),
                 ("applied", [])):
        st.setdefault(k, v)
    return st


def finish_pending(tdir, history, hist_path):
    """Complete an interrupted pending move (shared by Batch and void_streak). Returns True if one was completed."""
    if history and history[-1].get("pending"):
        if os.path.exists(tdir) and not os.path.exists(history[-1]["moved_to"]):
            shutil.move(tdir, history[-1]["moved_to"])
        history[-1].pop("pending"); ID.atomic_json(hist_path, history)
        return True
    return False


def save_streak(out, st):
    ID.atomic_json(os.path.join(out, STREAK_STATE), st)


def _void_one(out, e, note):
    """Void one streak attempt in output dir OUT (marks first, then the pending-protocol move; crash-safe)."""
    iid, arm, att = e["id"], e["arm"], e["attempt"]
    tdir = os.path.join(out, iid, arm); hist_p = os.path.join(out, "attempts", f"{iid}.{arm}.history.json")
    os.makedirs(os.path.join(out, "attempts"), exist_ok=True)
    hist = json.load(open(hist_p)) if os.path.exists(hist_p) else []
    finish_pending(tdir, hist, hist_p)            # review m5(a): complete an earlier interrupted void/move first
    for h in hist:
        if h.get("attempt") == att:
            h.update(voided=True, void_note=note)
    if att not in voided_attempts(hist):          # the voided attempt may still be the live terminal result
        hist.append({"attempt": att, "voided": True, "void_note": note, "reason": "voided_streak_mark", "t": ID.now()})
    ID.atomic_json(hist_p, hist)
    res_p = os.path.join(tdir, "result.json")
    if os.path.exists(res_p):
        r = json.load(open(res_p))
        if r.get("attempt") == att or (r.get("why") == ["TWO_ATTEMPTS_NO_RESULT"] and len(active(hist)) < 2):
            dst = os.path.join(out, "attempts", f"{iid}.{arm}-voided-{int(time.time() * 1000)}")
            hist.append({"moved_to": dst, "reason": "voided_streak", "attempt": None, "code": r.get("code"),
                         "voided": True, "void_note": note, "t": ID.now(), "pending": True})
            ID.atomic_json(hist_p, hist)
            shutil.move(tdir, dst)
            hist[-1].pop("pending"); ID.atomic_json(hist_p, hist)


def void_streak(out, streak_file, note):
    """Author-only, after the cause of a STOP_ENG_STREAK pause has been found and fixed (v4 §7.4): the attempts listed in
    the streak file no longer count toward their slot's 2-attempt cap. A slot whose terminal result came from a voided
    attempt is moved aside (kept, marked voided) so it runs again. Nothing is deleted. Crash-safe (review C2-4): the
    void marks are persisted first, the move uses the same pending protocol as Batch._move, and a slot is judged from
    its history file, so an interrupted void is completed on resume. Resolves the persisted streak pause."""
    if not note:
        raise SystemExit("a note (cause and fix) is required")
    out = os.path.abspath(out)
    ss0 = load_streak(out)
    if ss0.get("tripped") and os.path.abspath(ss0["tripped"]) != os.path.abspath(streak_file):   # review m5(b)
        raise SystemExit(f"the current pause refers to {ss0['tripped']}, not {streak_file}; void that file or use --ack-streak")
    st = json.load(open(streak_file)); voided = []
    for e in st["attempts"]:
        # AB check 1: an attempt inherited from a prior dir (I2) is voided IN THAT DIR (its history, its result), so the
        # prior dir no longer counts the slot as done and the new dir runs it again
        target = os.path.abspath(e.get("dir") or out)
        if target == out:
            _void_one(out, e, note)
        else:
            with ID.OutLock(target):
                _void_one(target, e, note)
        voided.append([e["id"], e["arm"], e["attempt"], target])
    ss = load_streak(out)
    ss["resolutions"].append({"how": "void", "file": os.path.abspath(streak_file), "note": note, "t": ID.now()})
    ss.update(streak=[], tripped=None); save_streak(out, ss)
    ID.ledger(os.path.join(out, "ledger.jsonl"), {"event": "VOID_STREAK", "file": os.path.abspath(streak_file),
                                                    "note": note, "voided": voided})
    return voided


def ack_judge_streak(out, note):
    """Author-only: lift a persisted STOP_JUDGE_STREAK pause after the evaluation environment has been checked/fixed."""
    if not note:
        raise SystemExit("a note (cause and fix) is required")
    out = os.path.abspath(out); ss = load_streak(out)
    ss["resolutions"].append({"how": "ack_judge", "tripped": ss.get("judge_tripped"), "note": note, "t": ID.now()})
    ss.update(judge_streak=0, judge_tripped=None); save_streak(out, ss)
    ID.ledger(os.path.join(out, "ledger.jsonl"), {"event": "ACK_JUDGE_STREAK", "note": note})


def infra_counts(res):
    """(n infrastructure-unknown products, n products the judge handled) from one result's verdicts."""
    uniq = {}
    for k, v in (res.get("verdicts") or {}).items():          # submission and F share one verdict when bytes are equal
        if isinstance(v, dict):
            uniq[v.get("product_sha256") or k] = v
    infra = sum(1 for v in uniq.values() if v.get("verdict") is None and v.get("reason") in INFRA_REASONS)
    return infra, len(uniq) - infra


def ack_streak(out, note):
    """Author-only: lift a persisted STOP_ENG_STREAK pause WITHOUT voiding (the failed attempts keep counting)."""
    if not note:
        raise SystemExit("a note (cause and fix) is required")
    out = os.path.abspath(out); ss = load_streak(out)
    ss["resolutions"].append({"how": "ack", "tripped": ss.get("tripped"), "note": note, "t": ID.now()})
    ss.setdefault("acked", []).extend(e.get("attempt") for e in ss["streak"])   # never re-counted on resume (m6)
    ss.update(streak=[], tripped=None); save_streak(out, ss)
    ID.ledger(os.path.join(out, "ledger.jsonl"), {"event": "ACK_STREAK", "note": note})


class Batch:
    """run_fn(iid=, arm=, attempt=, tdir=, image_id=) -> return code (writes tdir/record.json);
    eval_fn(tdir, instance_id=, arm=, attempt=, image_id=, dev=) -> result dict (writes tdir/result.json);
    image_id_fn(instance_id) -> local image Id or None; week_exhausted() -> list of exhausted week caps."""

    def __init__(self, *, out, ids, arms, pinned, spend, run_fn, eval_fn, image_id_fn, dev=False,
                 week_exhausted=lambda: [], bridge_code=None, d1_code=None, extra_start=None, prior=None,
                 disk_free_fn=None):
        self.out, self.ids, self.arms, self.pinned, self.spend = os.path.abspath(out), ids, arms, pinned, spend
        self.run_fn, self.eval_fn, self.image_id_fn, self.dev, self.week_exhausted = run_fn, eval_fn, image_id_fn, dev, week_exhausted
        self.bridge_code = bridge_code or ID.bridge_code_sha256()
        self.d1_code = d1_code or ID.d1_code_sha256()
        self.led = os.path.join(self.out, "ledger.jsonl")
        self.extra_start = extra_start or {}
        # prior: [(dir, bridge_code_sha256)] earlier output dirs of the SAME experiment under an older code identity
        # (addendum 2, I2): their finished slots are skipped here; each dir is checked against its own identity
        self.prior = [(os.path.abspath(d), sha) for d, sha in (prior or [])]
        self.disk_free_fn = disk_free_fn or (lambda: shutil.disk_usage(self.out).free)
        os.makedirs(os.path.join(self.out, "attempts"), exist_ok=True)
        self.state = load_streak(self.out)            # persisted across processes (review C2-2)

    @property
    def streak(self):
        return self.state["streak"]

    def ident(self, iid, arm, pin):
        return {"bridge_code_sha256": self.bridge_code, "d1_code_sha256": self.d1_code, "k": ID.K, "arm": arm,
                "instance_id": iid, "image_id": pin, "dev": bool(self.dev)}

    def log(self, **kw):
        ID.ledger(self.led, kw)

    def run(self):
        with ID.OutLock(self.out):                     # review I4: one writer per output dir
            # re-read under the lock: --ack-streak / --ack-judge-streak / --void-streak may have changed it after __init__
            self.state = load_streak(self.out)
            return self._run()

    def _scan_identity(self, out, code):
        """Every result.json / record.json in an output dir (not attempts/) and every earlier batch_start must carry
        this code identity (review I2): checked BEFORE anything is spent."""
        bad = []
        for p in glob.glob(os.path.join(out, "*", "*", "result.json")) + glob.glob(os.path.join(out, "*", "*", "record.json")):
            if os.sep + "attempts" + os.sep in p:
                continue
            try:
                r = json.load(open(p))
            except Exception:
                continue
            got = ((r.get("identity") or {}).get("bridge_code_sha256") if p.endswith("result.json") else r.get("bridge_code_sha256"))
            d1 = ((r.get("identity") or {}).get("d1_code_sha256") if p.endswith("result.json") else r.get("d1_code_sha256"))
            dev = ((r.get("identity") or {}).get("dev") if p.endswith("result.json") else r.get("dev"))
            if got not in (None, code) or d1 not in (None, self.d1_code) or (dev is not None and bool(dev) != bool(self.dev)):
                bad.append(os.path.relpath(p, out))
        led = os.path.join(out, "ledger.jsonl")
        if os.path.exists(led):
            for line in open(led):
                e = json.loads(line)
                if e.get("event") == "batch_start" and (e.get("bridge_code_sha256") != code or e.get("d1_code_sha256") != self.d1_code):
                    bad.append(f"ledger batch_start {e.get('t')}"); break
        return bad

    def _run(self):
        sch = schedule(self.ids, self.arms)
        scan = self._scan_identity(self.out, self.bridge_code)       # before anything is written to the ledger
        for d, sha in self.prior:
            scan += [f"prior {d}: {x}" for x in self._scan_identity(d, sha)]
        # a refused start is NOT logged as batch_start, so it cannot block a later resume under the original identity
        self.log(event="batch_refused" if scan else "batch_start", batch=BATCH_VERSION, bridge_code_sha256=self.bridge_code,
                 d1_code_sha256=self.d1_code, arms=self.arms, n_tasks=len(self.ids), n_slots=len(sch), dev=self.dev,
                 schedule_sha256=hashlib.sha256(json.dumps(sch).encode()).hexdigest(), **self.extra_start)
        status = "END"
        refused = None if scan else self._inherit_prior_state()
        if refused:
            self.log(event="STOP_STATE_CHAIN", reason=refused)
            status, sch = "STOPPED", []
        elif scan:
            self.log(event="STOP_IDENTITY_MISMATCH", scope="directory_scan", n=len(scan), where=scan[:20])
            status, sch = "STOPPED", []
        elif self.state.get("tripped"):
            self.log(event="STOP_ENG_STREAK_UNRESOLVED", streak_file=self.state["tripped"])
            status, sch = "STOPPED", []
        elif self.state.get("judge_tripped"):
            self.log(event="STOP_JUDGE_STREAK_UNRESOLVED", streak_file=self.state["judge_tripped"])
            status, sch = "STOPPED", []
        elif not self._reconciled("batch_start"):
            status = "STOPPED"
            sch = []
        for n, (iid, arm) in enumerate(sch, 1):
            st = self.slot(iid, arm, n)
            if st == "STOP" or not self._reconciled(f"after {iid}/{arm}"):
                status = "STOPPED"
                break
        done = sum(1 for iid, arm in schedule(self.ids, self.arms) if self._terminal(iid, arm))
        n = len(self.ids) * len(self.arms)
        if status == "END" and done < n:
            status = "END_INCOMPLETE"                  # review m9: END means every slot has a terminal result
        self.log(event="batch_end", status=status, n_slots=n, n_terminal=done, n_no_result=n - done)
        return status

    def _inherit_prior_state(self):
        """Closure review B / AB check 2: a new output dir that continues the experiment (I2) starts from the streak
        state of the prior dirs (run order), so consecutive failures are counted across dir boundaries. Only on the
        dir's first start (no state file yet); afterwards its own persisted state rules. The chain is walked back from
        the LAST prior dir to the latest one with a state file; a prior dir without a state file is skipped only if it
        holds no result/record at all, otherwise the start is refused (state lost; the author must repair it). Results
        of the chosen dir that were written but never applied (crash window, panoramic ⑥) are applied here, in schedule
        order. A carried-over pause is lifted only by --void-streak / --ack-streak / --ack-judge-streak on the NEW dir;
        a void acts in the dir where each attempt lives. Returns None, or the reason the start is refused."""
        if not self.prior or os.path.exists(os.path.join(self.out, STREAK_STATE)):
            return None
        chosen = None
        for d, _ in reversed(self.prior):
            if os.path.exists(os.path.join(d, STREAK_STATE)):
                chosen = d; break
            if glob.glob(os.path.join(d, "*", "*", "result.json")) + glob.glob(os.path.join(d, "*", "*", "record.json")):
                return f"prior dir {d} has results but no {STREAK_STATE}: its streak state is lost"
        if chosen is None:
            return None                                   # no prior dir ever ran anything
        src = load_streak(chosen)
        self.state.update({k: src[k] for k in ("streak", "tripped", "judge_streak", "judge_tripped")})
        self.state["streak"] = [dict(e, dir=e.get("dir") or chosen) for e in self.state["streak"]]
        self.state["acked"] = list(src.get("acked", []))
        self.state["applied"] = list(src.get("applied", []))
        self.state["inherited_from"] = {"dir": chosen, "t": ID.now()}
        late = []
        for iid, arm in schedule(self.ids, self.arms):
            try:
                r = json.load(open(os.path.join(chosen, iid, arm, "result.json")))
            except Exception:
                continue
            hp = os.path.join(chosen, "attempts", f"{iid}.{arm}.history.json")
            hist = json.load(open(hp)) if os.path.exists(hp) else []
            if r.get("terminal") and r.get("attempt") and counts_as_attempt(r, hist) and r["attempt"] not in self.state["applied"]:
                self._apply(r, iid, arm, r["attempt"], src=chosen); late.append([iid, arm, r["attempt"]])
        save_streak(self.out, self.state)
        self.log(event="STREAK_INHERITED", prior=chosen, streak=len(self.state["streak"]), tripped=self.state["tripped"],
                 judge_streak=self.state["judge_streak"], judge_tripped=self.state["judge_tripped"], applied_late=late)
        return None

    def _terminal(self, iid, arm):
        p = os.path.join(self.out, iid, arm, "result.json")
        try:
            if json.load(open(p)).get("terminal"):
                return True
        except Exception:
            pass
        return self._prior_done(iid, arm)

    def _prior_done(self, iid, arm):
        for d, sha in self.prior:
            p = os.path.join(d, iid, arm, "result.json")
            hp = os.path.join(d, "attempts", f"{iid}.{arm}.history.json")
            try:
                r = json.load(open(p))
            except Exception:
                continue
            hist = json.load(open(hp)) if os.path.exists(hp) else []
            if r.get("terminal") and (r.get("identity") or {}).get("bridge_code_sha256") == sha and not rerun_eligible(r, hist):
                return True
        return False

    def _reconciled(self, where):
        """v4 §7.3: local calls.json vs spend journal; any discrepancy pauses the batch before more money is spent."""
        # panoramic ④: an experiment continued in a new dir (I2) shares one spend ledger with its prior dirs, so the
        # calls of every identity-checked dir of the experiment are reconciled together
        probs = reconcile([self.out] + [d for d, _ in self.prior], self.spend.path)
        if probs:
            self.log(event="STOP_RECONCILE", where=where, n=len(probs), problems=probs[:20])
        return not probs

    # --- one slot (task x arm)
    def _paths(self, iid, arm):
        return (os.path.join(self.out, iid, arm), os.path.join(self.out, "attempts", f"{iid}.{arm}.history.json"))

    def _move(self, tdir, iid, arm, history, hist_path, reason, res=None, counts=True):
        """History entry first (pending), then the move, then mark done: a crash in between is completed on resume
        without counting the attempt twice. counts=False: the moved result was not a counted attempt (voided or
        synthetic), so its entry is marked voided."""
        dst = os.path.join(self.out, "attempts", f"{iid}.{arm}-{len(history) + 1}-{int(time.time() * 1000)}")
        history.append({"moved_to": dst, "reason": reason, "attempt": (res or {}).get("attempt"),
                        "code": (res or {}).get("code"), "why": (res or {}).get("why"), "t": ID.now(), "pending": True,
                        **({} if counts else {"voided": True, "void_note": "not a counted attempt"})})
        ID.atomic_json(hist_path, history)
        shutil.move(tdir, dst)
        history[-1].pop("pending"); ID.atomic_json(hist_path, history)
        self.log(event="moved", id=iid, arm=arm, reason=reason, to=dst, n_history=len(history))

    def _finish_pending(self, tdir, iid, arm, history, hist_path):
        if finish_pending(tdir, history, hist_path):
            self.log(event="move_completed_on_resume", id=iid, arm=arm)

    def _evaluate(self, tdir, iid, arm, attempt, pin):
        try:
            return self.eval_fn(tdir, instance_id=iid, arm=arm, attempt=attempt, image_id=pin, dev=self.dev)
        except Exception as e:      # the agent run is kept; the slot is evaluated again on resume (no agent rerun)
            self.log(event="EVAL_EXCEPTION", id=iid, arm=arm, attempt=attempt, error=f"{type(e).__name__}: {e}")
            return None

    def slot(self, iid, arm, n=None):
        tdir, hist_path = self._paths(iid, arm)
        if not os.path.exists(os.path.join(tdir, "result.json")) and self._prior_done(iid, arm):
            self.log(event="skip_prior", id=iid, arm=arm)   # finished under an older identity in a prior dir (I2)
            return "DONE"
        pin = (self.pinned.get(iid) or {}).get("pinned_id")
        ident = self.ident(iid, arm, pin)
        res_p = os.path.join(tdir, "result.json")
        history = json.load(open(hist_path)) if os.path.exists(hist_path) else []
        self._finish_pending(tdir, iid, arm, history, hist_path)
        r = None
        if os.path.exists(res_p):
            try:
                r = json.load(open(res_p))
            except Exception:
                r = None
        ok = False
        if r is not None and r.get("terminal"):
            bad = {k: [(r.get("identity") or {}).get(k), v] for k, v in ident.items() if (r.get("identity") or {}).get(k) != v}
            if bad:      # review C2-1: never replace a finished result because the code/pins changed; stop and report
                self.log(event="STOP_IDENTITY_MISMATCH", id=iid, arm=arm, fields=bad)
                return "STOP"
            ok = True
        if not ok and r is None:                         # a finished agent run whose evaluation never completed
            rec = None
            try:
                rec = json.load(open(os.path.join(tdir, "record.json")))
            except Exception:
                pass
            if rec and rec.get("terminal") and rec.get("attempt") and rec.get("instance_id") == iid and rec.get("arm") == arm \
                    and rec.get("bridge_code_sha256") == self.bridge_code:
                ex = self.week_exhausted()
                if ex:                                    # review I1(c): never start an evaluation over the week caps
                    self.log(event="STOP_WEEK_CAP", id=iid, arm=arm, caps=ex)
                    return "STOP"
                self.log(event="eval_only_resume", id=iid, arm=arm, attempt=rec["attempt"])
                res = self._evaluate(tdir, iid, arm, rec["attempt"], pin)
                if res is None:
                    return "STOP"
                return self._after(res, tdir, iid, arm, history, hist_path, res_p, rec["attempt"], resumed=True)
        if ok and history and not r.get("attempt_history"):
            r["attempt_history"] = history               # the rerun was interrupted right after its evaluation
            ID.atomic_json(res_p, r)
        if ok and r.get("attempt") and counts_as_attempt(r, history):
            # panoramic ⑥ (supersedes m6): a result written just before a crash has not updated the streak state yet;
            # apply it now, once per attempt (success resets, ENG_FAIL extends, judge infra counts)
            self._apply(r, iid, arm, r["attempt"])
        if ok and not rerun_eligible(r, history):
            self.log(event="skip_done", id=iid, arm=arm, code=r.get("code"))
            return "DONE"
        if os.path.exists(tdir):
            self._move(tdir, iid, arm, history, hist_path, "eng_fail_rerun" if ok else "incomplete",
                       r if ok else None, counts=counts_as_attempt(r, history) if ok else True)
        return self._attempts(tdir, iid, arm, history, hist_path, res_p, pin, ident, n)

    def _attempts(self, tdir, iid, arm, history, hist_path, res_p, pin, ident, n):
        while True:
            if len(active(history)) >= 2:
                os.makedirs(tdir, exist_ok=True)
                ID.atomic_json(res_p, {"instance_id": iid, "arm": arm, "attempt": None, "k": ID.K, "dev": bool(self.dev),
                                       "identity": dict(ident, model_versions=[]), "code": "ENG_FAIL",
                                       "rule": {"S": "unknown", "H": "unknown"}, "S": None, "H": None, "W": None,
                                       "why": ["TWO_ATTEMPTS_NO_RESULT"], "resource_stop": False,
                                       "attempt_history": history, "terminal": True, "evaluated": ID.now()})
                self.log(event="terminal_after_two_attempts", id=iid, arm=arm)
                return "DONE"
            if self.spend.exhausted():
                self.log(event="STOP_SPEND", id=iid, arm=arm, stop_usd=self.spend.stop)
                return "STOP"
            blk = self.spend.blocked()
            if blk:                                      # v4 §7.3: pricing pause; resume only after the author clears it
                self.log(event="STOP_PRICING", id=iid, arm=arm, blocked=blk)
                return "STOP"
            if self.state.get("tripped") or len(self.streak) >= ENG_STREAK_STOP:   # v4 §7.4 (persisted, review C2-2)
                if not self.state.get("tripped"):
                    f = os.path.join(self.out, f"streak_{int(time.time() * 1000)}.json")
                    ID.atomic_json(f, {"attempts": self.streak, "t": ID.now()})
                    self.state["tripped"] = f; save_streak(self.out, self.state)
                self.log(event="STOP_ENG_STREAK", id=iid, arm=arm, streak=len(self.streak), streak_file=self.state["tripped"])
                return "STOP"
            if self.state.get("judge_tripped") or self.state.get("judge_streak", 0) >= JUDGE_STREAK_STOP:   # I1
                if not self.state.get("judge_tripped"):
                    f = os.path.join(self.out, f"judge_streak_{int(time.time() * 1000)}.json")
                    ID.atomic_json(f, {"judge_streak": self.state["judge_streak"], "t": ID.now()})
                    self.state["judge_tripped"] = f; save_streak(self.out, self.state)
                self.log(event="STOP_JUDGE_STREAK", id=iid, arm=arm, streak_file=self.state["judge_tripped"])
                return "STOP"
            ex = self.week_exhausted()
            if ex:
                self.log(event="STOP_WEEK_CAP", id=iid, arm=arm, caps=ex)
                return "STOP"
            try:
                free = self.disk_free_fn()
            except Exception:
                free = None
            if free is None or free < MIN_FREE_BYTES:      # review m8 (a failed check also stops: conservative)
                self.log(event="STOP_DISK", id=iid, arm=arm, free_bytes=free)
                return "STOP"
            local = self.image_id_fn(iid)
            if not pin or local != pin:
                self.log(event="IMAGE_NOT_READY", id=iid, arm=arm, local=local, pinned=pin)
                return "SKIP"
            if not self._reconciled(f"before {iid}/{arm}"):     # every launch, incl. immediate reruns (review C2-3)
                return "STOP"
            attempt = uuid.uuid4().hex[:8]
            os.makedirs(tdir, exist_ok=True)
            ID.atomic_json(os.path.join(tdir, "attempt.json"), {"attempt": attempt, "t": ID.now()})   # key for reconcile()
            self.log(event="run_start", id=iid, arm=arm, n=n, attempt=attempt, try_no=len(active(history)) + 1)
            t0 = time.time()
            try:
                rc = self.run_fn(iid=iid, arm=arm, attempt=attempt, tdir=tdir, image_id=pin)
            except Exception as e:                       # the evaluator then sees no/partial record => ENG_FAIL
                rc = f"{type(e).__name__}: {e}"
            res = self._evaluate(tdir, iid, arm, attempt, pin)
            if res is None:
                return "STOP"
            self.log(event="run_end", id=iid, arm=arm, attempt=attempt, rc=rc, s=round(time.time() - t0, 1),
                     code=res.get("code"), S=res.get("S"), H=res.get("H"), W=res.get("W"), why=res.get("why"),
                     resource_stop=res.get("resource_stop"),
                     model_versions=(res.get("identity") or {}).get("model_versions"))
            out = self._after(res, tdir, iid, arm, history, hist_path, res_p, attempt)
            if out != "AGAIN":
                return out

    def _apply(self, res, iid, arm, attempt, src=None):
        """Apply one attempt's result to the persisted streak state exactly once (panoramic ⑥): idempotent per attempt
        id, so a crash between writing result.json and this update is completed on resume, never applied twice."""
        if attempt in self.state["applied"]:
            return
        if res.get("code") == "ENG_FAIL" and not res.get("resource_stop"):
            if attempt not in [e.get("attempt") for e in self.state["streak"]] and attempt not in self.state.get("acked", []):
                self.state["streak"].append({"id": iid, "arm": arm, "attempt": attempt, "dir": src or self.out})
        else:
            self.state["streak"] = []
        infra, handled = infra_counts(res)                # I1: consecutive infrastructure-unknown judged products
        if handled:
            self.state["judge_streak"] = 0
        self.state["judge_streak"] = self.state.get("judge_streak", 0) + infra
        self.state["applied"].append(attempt)
        save_streak(self.out, self.state)

    def _after(self, res, tdir, iid, arm, history, hist_path, res_p, attempt, resumed=False):
        self._apply(res, iid, arm, attempt)
        if rerun_eligible(res, history):
            self._move(tdir, iid, arm, history, hist_path, "eng_fail_rerun", res)
            self.log(event="rerun", id=iid, arm=arm, after_attempt=attempt)
            if resumed:
                pin = (self.pinned.get(iid) or {}).get("pinned_id")
                return self._attempts(tdir, iid, arm, history, hist_path, res_p, pin, self.ident(iid, arm, pin), None)
            return "AGAIN"
        if history:
            res["attempt_history"] = history
            ID.atomic_json(res_p, res)
        return "STOP" if res.get("resource_stop") else "DONE"


    def reeval_infra(self, note):
        """Author-only (addendum 2, I1): after the cause of an infrastructure failure in evaluation has been found and
        fixed, and BEFORE the analysis is run, re-evaluate ONCE from the saved artifacts the products whose verdict was
        infrastructure-unknown (INFRA_UNRECOVERED / JUDGE_EXCEPTION), in terminal non-ENG_FAIL results. The agent is
        not rerun and no attempt is used. Panoramic review:
          ① only those products are judged again; every other verdict (definite, EVAL_UNSTABLE, OTHER) is reused as is;
          ③ refused unless every result/record in OUT carries this code identity (a dir is re-evaluated only by the
            code it was produced under; --prior-out does not change that), and each slot's identity must match;
          ⑤ crash-safe: a marker attempts/<id>.<arm>.reeval1.json is written first; each product is CLAIMED in it
            before the judge is called (closure review A) and its verdict stored right after. On resume a claimed
            product with a stored verdict reuses it, and a claimed product without one keeps its original unknown
            verdict (reason REEVAL_INTERRUPTED) and is never judged again: at most one re-judgment per product.
            result.json is written once, with reeval_count = 1, at the end.
        The previous result is kept in attempts/."""
        if not note:
            raise SystemExit("a note (cause and fix) is required")
        done = []
        with ID.OutLock(self.out):
            scan = self._scan_identity(self.out, self.bridge_code)
            if scan:
                self.log(event="REEVAL_REFUSED", reason="directory identity", n=len(scan), where=scan[:20])
                raise SystemExit(f"re-evaluation refused: {len(scan)} file(s) in {self.out} carry another code identity "
                                 f"(e.g. {scan[0]}); re-evaluate a directory only with the code it was produced under")
            for iid, arm in schedule(self.ids, self.arms):
                tdir, _ = self._paths(iid, arm); rp = os.path.join(tdir, "result.json")
                mp = os.path.join(self.out, "attempts", f"{iid}.{arm}.reeval1.json")
                try:
                    r = json.load(open(rp))
                except Exception:
                    continue
                marker = json.load(open(mp)) if os.path.exists(mp) else None
                if not r.get("terminal") or r.get("code") == "ENG_FAIL" or r.get("reeval_count", 0) >= 1:
                    continue
                if marker is None and not infra_counts(r)[0]:
                    continue
                pin = (self.pinned.get(iid) or {}).get("pinned_id")
                bad = {k: [(r.get("identity") or {}).get(k), v] for k, v in self.ident(iid, arm, pin).items()
                       if (r.get("identity") or {}).get(k) != v}
                if bad or (marker and marker.get("attempt") != r.get("attempt")):
                    self.log(event="REEVAL_REFUSED", id=iid, arm=arm, reason="slot identity", fields=bad); continue
                ex = self.week_exhausted()
                if ex:
                    self.log(event="STOP_WEEK_CAP", id=iid, arm=arm, caps=ex, during="reeval_infra"); break
                if marker is None:
                    keep = os.path.join(self.out, "attempts", f"{iid}.{arm}.result-pre-reeval-{int(time.time() * 1000)}.json")
                    shutil.copy2(rp, keep)
                    marker = {"attempt": r.get("attempt"), "kept": keep, "note": note, "t": ID.now(), "claimed": [], "done": {}}
                    ID.atomic_json(mp, marker)
                else:
                    self.log(event="REEVAL_RESUMED", id=iid, arm=arm, already=sorted(marker["done"]),
                             lost=sorted(set(marker.get("claimed", [])) - set(marker["done"])))
                marker.setdefault("claimed", [])
                orig = {v["product_sha256"]: v for v in (r.get("verdicts") or {}).values()
                        if isinstance(v, dict) and v.get("product_sha256")}
                final = {h: v for h, v in orig.items() if not (v.get("verdict") is None and v.get("reason") in INFRA_REASONS)}
                for h in set(marker["claimed"]) - set(marker["done"]):   # re-judgment used up, verdict lost: stays unknown
                    final[h] = dict(orig.get(h) or {"verdict": None, "slots": []}, verdict=None, reason="REEVAL_INTERRUPTED")
                final.update(marker["done"])               # products already re-judged before an interruption

                def claim(h, marker=marker, mp=mp):
                    marker["claimed"].append(h); ID.atomic_json(mp, marker)

                def keep_verdict(h, v, marker=marker, mp=mp):
                    marker["done"][h] = v; ID.atomic_json(mp, marker)
                try:
                    res = self.eval_fn(tdir, instance_id=iid, arm=arm, attempt=r["attempt"], image_id=pin, dev=self.dev,
                                       tag_suffix="-re1", prior_verdicts=final, on_claim=claim, on_verdict=keep_verdict,
                                       write=False)
                except Exception as e:
                    self.log(event="REEVAL_EXCEPTION", id=iid, arm=arm, error=f"{type(e).__name__}: {e}"); continue
                if res.get("code") != r.get("code"):       # artifacts no longer match the record: never overwrite
                    self.log(event="REEVAL_REFUSED", id=iid, arm=arm, reason="code changed", before=r.get("code"),
                             after=res.get("code"), why=res.get("why")); continue
                res.update(reeval_count=1, reeval_of=marker["kept"], reeval_note=marker["note"],
                           reeval_products=sorted(marker["done"]),
                           reeval_lost=sorted(set(marker["claimed"]) - set(marker["done"])))
                if r.get("attempt_history"):
                    res["attempt_history"] = r["attempt_history"]
                ID.atomic_json(rp, res)
                self.log(event="REEVAL_INFRA", id=iid, arm=arm, before=[r.get("S"), r.get("H"), r.get("W")],
                         after=[res.get("S"), res.get("H"), res.get("W")], kept=marker["kept"],
                         rejudged=sorted(marker["done"]))
                done.append([iid, arm])
        return done


CURRENT = {"proc": None}


def _kill_current(*_):
    """Kill the running run_one process GROUP (review I4): no orphan keeps spending after the batch stops."""
    p = CURRENT.get("proc")
    if p is not None and p.poll() is None:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except Exception:
            pass


def _on_signal(signum, frame):
    _kill_current()
    raise SystemExit(f"stopped by signal {signum}")


def run_subprocess(cmd, log_path, timeout_s, cwd=None):
    """Run one child in its own process group (review I4) with a wall-clock limit (review m1). Returns the exit code,
    or "TIMEOUT" after killing the whole group."""
    with open(log_path, "w") as logf:
        p = subprocess.Popen(cmd, stdout=logf, stderr=subprocess.STDOUT, text=True, cwd=cwd, start_new_session=True)
        CURRENT["proc"] = p
        try:
            return p.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            _kill_current(); p.wait()
            return "TIMEOUT"
        finally:
            CURRENT["proc"] = None


def sha256_file(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def main():
    if any(x in sys.argv for x in ("--void-streak", "--ack-streak", "--ack-judge-streak")):   # author maintenance (v4 §7.4)
        vp = argparse.ArgumentParser(); vp.add_argument("--void-streak"); vp.add_argument("--ack-streak", action="store_true")
        vp.add_argument("--ack-judge-streak", action="store_true")
        vp.add_argument("--out", required=True); vp.add_argument("--note", required=True); v = vp.parse_args()
        with ID.OutLock(v.out):
            if v.void_streak:
                print(json.dumps(void_streak(v.out, v.void_streak, v.note)))
            elif v.ack_judge_streak:
                ack_judge_streak(v.out, v.note)
            else:
                ack_streak(v.out, v.note)
        return
    ap = argparse.ArgumentParser()
    for x in ("--selection", "--tasks-meta", "--images", "--out", "--spend", "--week-state", "--agent-python"):
        ap.add_argument(x, required=True)
    ap.add_argument("--stop-usd", type=float, required=True)
    ap.add_argument("--arms", default=",".join(P.ARMS))
    ap.add_argument("--judge-python", default=sys.executable)
    ap.add_argument("--dev", action="store_true")
    ap.add_argument("--prior-out", action="append", default=[], help="DIR=BRIDGE_SHA of an earlier output dir (addendum 2, I2)")
    for x in ("--expect-selection-sha", "--expect-tasks-meta-sha", "--expect-images-sha"):
        ap.add_argument(x, default=None, help="required in a formal run (review m7)")
    ap.add_argument("--reeval-infra", action="store_true", help="author-only: re-evaluate infrastructure unknowns once (I1)")
    ap.add_argument("--note", default=None)
    a = ap.parse_args()
    for name in ("agent_python", "judge_python"):        # relative paths break: run_one runs with cwd = bridge/
        path = os.path.abspath(getattr(a, name))
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            raise SystemExit(f"--{name.replace('_', '-')} {path} is not an executable file")
        setattr(a, name, path)
    d1_code = ID.check_d1()                              # fail-closed on the sealed measurement code
    shas = {"selection": sha256_file(a.selection), "tasks_meta": sha256_file(a.tasks_meta), "images": sha256_file(a.images)}
    if not a.dev:                                        # review m7: formal inputs must be the sealed / recorded ones
        for k, want in (("selection", a.expect_selection_sha), ("tasks_meta", a.expect_tasks_meta_sha), ("images", a.expect_images_sha)):
            if not want or shas[k] != want:
                raise SystemExit(f"{k} sha256 {shas[k]} != expected {want}")
    chk = subprocess.run([a.agent_python, os.path.join(HERE, "run_one.py"), "--check-env"], capture_output=True, text=True, cwd=HERE)
    if chk.returncode != 0:                              # review I3: the agent environment must match the frozen lock
        raise SystemExit(f"agent environment check failed: {chk.stdout.strip()[-800:]} {chk.stderr.strip()[-400:]}")
    import evaluate as EV
    J, WK = ID.import_d1("judge", "week")
    out = os.path.abspath(a.out); os.makedirs(out, exist_ok=True)
    arms = [x for x in a.arms.split(",") if x]
    sel = json.load(open(a.selection)); ids = selection_ids(sel, a.dev)
    meta = json.load(open(a.tasks_meta))
    missing = [i for i in ids if not (meta.get(i) or {}).get("base_commit") or not (meta.get(i) or {}).get("problem_statement")]
    if missing:
        raise SystemExit(f"tasks-meta lacks base_commit/problem_statement for {missing}")
    pinned = json.load(open(a.images))["images"]
    registry, registry_sha = ID.load_registry()
    if not a.dev and not registry.get("confirmed_by_author"):
        raise SystemExit("pricing_registry.json is not confirmed by the author; a formal run may not start (v4 §7.3)")
    sp = Spend(os.path.abspath(a.spend), a.stop_usd, dev=a.dev)
    if json.load(open(sp.path)).get("stop_usd") != a.stop_usd:
        raise SystemExit("spend ledger was created with a different stop line")
    wk = WK.Week(a.week_state)
    prior = []
    for x in a.prior_out:
        d, _, sha = x.partition("=")
        if not sha:
            raise SystemExit(f"--prior-out needs DIR=BRIDGE_SHA, got {x}")
        prior.append((d, sha))

    def run_fn(*, iid, arm, attempt, tdir, image_id):
        pf = os.path.join(tdir, "problem.txt")
        open(pf, "w").write(meta[iid]["problem_statement"])
        cmd = [a.agent_python, os.path.join(HERE, "run_one.py"), "--instance", iid, "--arm", arm,
               "--dataset-base", meta[iid]["base_commit"], "--problem-file", pf, "--image-id", image_id, "--out", tdir,
               "--spend", sp.path, "--stop-usd", str(a.stop_usd), "--attempt", attempt] + (["--dev"] if a.dev else [])
        return run_subprocess(cmd, os.path.join(tdir, f"run-{attempt}.log"), RUN_TIMEOUT_S, cwd=HERE)

    def eval_fn(tdir, **kw):
        jf = lambda *x, **k: J.judge(*x, python=a.judge_python, **k)   # noqa: E731
        return EV.evaluate(tdir, judge_fn=jf, week=wk, bridge_code=b.bridge_code, d1_code=d1_code, **kw)

    b = Batch(out=out, ids=ids, arms=arms, pinned=pinned, spend=sp, run_fn=run_fn, eval_fn=eval_fn,
              image_id_fn=lambda iid: J.image_id(ID.image_name(iid)), dev=a.dev, week_exhausted=wk.exhausted,
              d1_code=d1_code, prior=prior,
              extra_start={"input_sha256": shas, "pricing_registry_sha256": registry_sha, "week_state": os.path.abspath(a.week_state),
                           "spend_ledger": sp.path, "prior": prior, "agent_env_check": chk.stdout.strip()[-200:]})
    if a.reeval_infra:
        print(json.dumps(b.reeval_infra(a.note))); return
    signal.signal(signal.SIGTERM, _on_signal); signal.signal(signal.SIGINT, _on_signal)
    try:
        print(b.run())
    finally:
        _kill_current()


if __name__ == "__main__":
    main()
