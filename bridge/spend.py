"""Run-wide API spend ledger with a hard stop line and a pricing pause (prereg DRAFT v4 §7). One JSON file shared by
all trajectories of one batch. The dev dry run uses its own ledger file (dev=True) with its own cap; the formal ledger
starts at zero (v4 §7.1: dry-run spend counts toward the approved total budget, not toward the formal stop line).

State: usd (priced spend), usd_provisional (reservations for unpriced calls; never treated as zero), stop_usd, dev,
blocked (None or {reason, t}), history (clears and reconciliations).
- exhausted(): usd + usd_provisional >= stop_usd.
- block(reason): set by the first unpriced call; batch.py refuses to start any further slot while blocked.
- Journal (spend.json.journal.jsonl, append-only, written under the same lock): one line per charge, keyed
  '<attempt>:<call i>:<response id>' for model calls, and one line per author reconciliation. reconcile() checks it
  against every calls.json under the batch output; batch.py pauses on any discrepancy (v4 §7.3).
- The $1 provisional reservation is an accounting placeholder, NOT an upper bound on the unknown call's cost.
- Author-only CLI after checking the provider bill and the price of the new version string:
    python spend.py --ledger spend.json --out OUT --clear --note "..." --reconciled-usd X --bill-id ID
  replaces the open provisional reservations (and resolves calls that reached calls.json without a charge) by the
  billed amount X (added to usd). Without --clear: prints the ledger and the reconcile problems."""
import argparse, fcntl, glob, json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import identity as ID  # noqa: E402


class Spend:
    def __init__(self, path, stop_usd, dev=None, key_prefix=None):
        self.path, self.stop, self.prefix = path, stop_usd, key_prefix
        self.journal = path + ".journal.jsonl"
        if not os.path.exists(path):
            tmp = path + ".tmp"
            json.dump({"usd": 0.0, "usd_provisional": 0.0, "stop_usd": stop_usd, "dev": bool(dev), "blocked": None,
                       "history": []}, open(tmp, "w"))
            os.replace(tmp, path)
        elif dev is not None and json.load(open(path)).get("dev", False) != bool(dev):
            raise SystemExit(f"spend ledger {path} was created for dev={not dev}; dry-run and formal ledgers are separate")

    def _journal(self, rec):
        with open(self.journal, "a") as f:
            f.write(json.dumps(dict(rec, t=time.time())) + "\n"); f.flush(); os.fsync(f.fileno())

    def _upd(self, fn, journal=None):
        """Read-modify-write under an exclusive lock on a separate lock file; the new state is written to a temp file
        and renamed into place, so a crash never leaves a truncated ledger (review item 8)."""
        with open(self.path + ".lock", "a+") as lk:
            fcntl.flock(lk, fcntl.LOCK_EX)
            st = json.load(open(self.path)); out = fn(st)
            rec = journal(out) if callable(journal) else journal
            if rec is not None:                          # same lock: totals and journal never diverge silently
                self._journal(rec)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(st, f); f.flush(); os.fsync(f.fileno())
            os.replace(tmp, self.path)
            return out

    def add(self, usd, provisional=False, key=None):
        """key = '<attempt>:<call i>:<response id>' for a model call (see reconcile()); None for other charges."""
        fld = "usd_provisional" if provisional else "usd"
        return self._upd(lambda st: st.__setitem__(fld, st.get(fld, 0.0) + float(usd)) or st[fld],
                         journal={"type": "charge", "key": key, "usd": float(usd), "provisional": bool(provisional)})

    def call_key(self, call):
        return f"{self.prefix}:{call.get('i')}:{call.get('response_id')}"

    def total(self):
        return self._upd(lambda st: st["usd"] + st.get("usd_provisional", 0.0))

    def exhausted(self):
        return self._upd(lambda st: st["usd"] + st.get("usd_provisional", 0.0) >= self.stop)

    def block(self, reason):
        def fn(st):
            if not st.get("blocked"):
                st["blocked"] = {"reason": reason, "t": time.time()}
        return self._upd(fn)

    def blocked(self):
        return self._upd(lambda st: st.get("blocked"))

    def _journal_rows(self):
        return [json.loads(l) for l in open(self.journal)] if os.path.exists(self.journal) else []

    def clear(self, note, reconciled_usd, resolves=(), bill_id=None):
        """Author-only, after checking the provider bill: replaces every open provisional reservation (and resolves the
        listed call keys, e.g. calls that reached calls.json without a charge) by the billed amount reconciled_usd.
        Idempotent (review C2-5): bill_id identifies the bill/reconciliation; a repeated bill_id changes nothing and
        returns False."""
        if not bill_id:
            raise ValueError("bill_id is required (one id per provider bill / reconciliation)")
        def fn(st):
            if bill_id in st.setdefault("bill_ids", []):
                return False
            # review m4: the journal is the authoritative record. If an earlier clear wrote its journal line but crashed
            # before the totals were replaced, only repair the totals (no second journal line).
            prior = [r for r in self._journal_rows() if r.get("type") == "reconcile" and r.get("bill_id") == bill_id]
            if prior:
                st["usd"] += float(prior[0]["usd"]); st["usd_provisional"] = 0.0; st["blocked"] = None
                st["bill_ids"].append(bill_id)
                st.setdefault("history", []).append({"t": time.time(), "note": note, "bill_id": bill_id, "repaired_totals": True})
                return "REPAIRED"
            ev = {"t": time.time(), "note": note, "bill_id": bill_id, "cleared": st.get("blocked"),
                  "provisional_before": st.get("usd_provisional", 0.0), "reconciled_usd": float(reconciled_usd),
                  "resolves": list(resolves)}
            st["usd"] += float(reconciled_usd); st["usd_provisional"] = 0.0
            st["blocked"] = None
            st["bill_ids"].append(bill_id)
            st.setdefault("history", []).append(ev)
            return True
        return self._upd(fn, journal=lambda done: {"type": "reconcile", "bill_id": bill_id, "usd": float(reconciled_usd),
                                                   "resolves": list(resolves), "note": note} if done is True else None)

    def rebuild_totals(self, note):
        """Author-only (review m4): recompute the totals file from the append-only journal, e.g. after a crash between
        a journal line and the totals replacement. The rebuild itself is recorded in the journal (type 'rebuild')."""
        def fn(st):
            rows = self._journal_rows()
            last = max([i for i, r in enumerate(rows) if r["type"] == "reconcile"], default=-1)
            usd = sum(r["usd"] for r in rows if r["type"] == "charge" and not r["provisional"]) + \
                sum(r["usd"] for r in rows if r["type"] == "reconcile")
            prov = sum(r["usd"] for i, r in enumerate(rows) if r["type"] == "charge" and r["provisional"] and i > last)
            before = {"usd": st["usd"], "usd_provisional": st.get("usd_provisional", 0.0)}
            st["usd"], st["usd_provisional"] = usd, prov
            st["bill_ids"] = sorted({r.get("bill_id") for r in rows if r["type"] == "reconcile" and r.get("bill_id")})
            st.setdefault("history", []).append({"t": time.time(), "note": note, "rebuild": True, "before": before})
            return before
        return self._upd(fn, journal=lambda before: {"type": "rebuild", "note": note, "before": before})


def _attempt_of(d):
    for f in ("attempt.json", "record.json"):
        try:
            a = json.load(open(os.path.join(d, f))).get("attempt")
            if a:
                return a
        except Exception:
            pass
    return None


def reconcile(out, ledger_path, tol=1e-9):
    """Offline check before (and during) a resume (prereg v4 §7.3): every model call recorded in any calls.json under
    OUT (live slots and attempts/; OUT may be a list of dirs: the current dir plus the prior dirs of the same experiment
    that share this ledger, addendum I2 / panoramic ④) must have exactly one charge in the spend journal, with the same amount if priced or
    a provisional reservation if unpriced; no keyed charge may lack its call; every provisional reservation must have
    been reconciled; the ledger totals must equal the journal. Only calls whose response never reached calls.json are
    outside this check (they need the provider bill). Returns a list of problems (empty = consistent)."""
    st = json.load(open(ledger_path)); jp = ledger_path + ".journal.jsonl"
    rows = [json.loads(l) for l in open(jp)] if os.path.exists(jp) else []
    charges, dup, resolved, recon_usd, bills, dup_bills = {}, [], set(), 0.0, set(), []
    for r in rows:
        if r["type"] == "charge" and r.get("key"):
            if r["key"] in charges:
                dup.append(r["key"])
            charges[r["key"]] = r
        elif r["type"] == "reconcile":
            if r.get("bill_id") in bills:
                dup_bills.append(r.get("bill_id"))
            bills.add(r.get("bill_id"))
            resolved.update(r.get("resolves") or []); recon_usd += r["usd"]
    reconciled_upto = max([i for i, r in enumerate(rows) if r["type"] == "reconcile"], default=-1)
    problems = [f"DUP_CHARGE {k}" for k in dup] + [f"DUP_RECONCILE bill_id={b}" for b in dup_bills]
    seen = set()
    outs = [out] if isinstance(out, str) else list(out)
    files = [(o, cf) for o in outs for cf in sorted(glob.glob(os.path.join(o, "**", "calls.json"), recursive=True))]
    for o, cf in files:
        d = os.path.dirname(cf); att = _attempt_of(d)
        try:
            calls = json.load(open(cf))
        except Exception:
            problems.append(f"UNREADABLE {cf}"); continue
        for c in calls:
            k = f"{att}:{c.get('i')}:{c.get('response_id')}"; seen.add(k)
            ch = charges.get(k)
            if k in resolved:
                continue
            if ch is None:
                problems.append(f"MISSING_CHARGE {k} ({os.path.relpath(cf, os.path.dirname(o))}; cost={c.get('cost')}, status={c.get('pricing_status')})")
            elif c.get("cost") is not None and (ch["provisional"] or abs(ch["usd"] - c["cost"]) > tol):
                problems.append(f"COST_MISMATCH {k}: calls.json {c['cost']} vs journal {ch['usd']}")
            elif c.get("cost") is None and not ch["provisional"]:
                problems.append(f"UNPRICED_BUT_CHARGED {k}")
    for k, ch in charges.items():
        if k not in seen and k not in resolved:
            problems.append(f"ORPHAN_CHARGE {k}")
    open_prov = [r for i, r in enumerate(rows) if r["type"] == "charge" and r["provisional"] and i > reconciled_upto]
    if open_prov:
        problems.append(f"OPEN_PROVISIONAL {len(open_prov)} reservation(s) not reconciled")
    usd_j = sum(r["usd"] for r in rows if r["type"] == "charge" and not r["provisional"]) + recon_usd
    prov_j = sum(r["usd"] for r in open_prov)
    if abs(st["usd"] - usd_j) > 1e-6 or abs(st.get("usd_provisional", 0.0) - prov_j) > 1e-6:
        problems.append(f"TOTALS_MISMATCH ledger usd={st['usd']:.6f}/prov={st.get('usd_provisional', 0.0):.6f} "
                        f"vs journal usd={usd_j:.6f}/prov={prov_j:.6f}")
    return problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ledger", required=True); ap.add_argument("--clear", action="store_true")
    ap.add_argument("--note", default=None); ap.add_argument("--reconciled-usd", type=float, default=None)
    ap.add_argument("--out", default=None, help="batch output dir: run reconcile(); with --clear, resolve its MISSING_CHARGE keys")
    ap.add_argument("--bill-id", default=None, help="unique id of the bill / reconciliation (required with --clear)")
    ap.add_argument("--prior-out", action="append", default=[], help="prior output dir sharing this ledger (I2; repeatable)")
    ap.add_argument("--rebuild-totals", action="store_true", help="recompute the totals from the journal (author-only)")
    a = ap.parse_args()
    if (a.clear or a.rebuild_totals) and not a.out:
        raise SystemExit("--out (the batch output dir) is required with --clear / --rebuild-totals, to take its lock")
    import contextlib
    with (ID.OutLock(a.out) if a.out else contextlib.nullcontext()):
        _main(a)


def _main(a):
    st = json.load(open(a.ledger))
    if a.rebuild_totals:
        if not a.note:
            raise SystemExit("--note is required")
        Spend(a.ledger, st["stop_usd"]).rebuild_totals(a.note)
        st = json.load(open(a.ledger))
    dirs = [a.out] + list(a.prior_out)
    probs = reconcile(dirs, a.ledger) if a.out else []
    if a.clear:
        if not a.note or a.reconciled_usd is None or not a.bill_id:
            raise SystemExit("--note, --reconciled-usd (the billed amount) and --bill-id are required")
        keys = [p.split()[1] for p in probs if p.startswith("MISSING_CHARGE")]
        if not Spend(a.ledger, st["stop_usd"]).clear(a.note, a.reconciled_usd, keys, bill_id=a.bill_id):
            print(f"bill_id {a.bill_id} was already applied; nothing changed")
        st = json.load(open(a.ledger)); probs = reconcile(dirs, a.ledger) if a.out else []
    print(json.dumps({"ledger": st, "reconcile_problems": probs}, indent=1))


if __name__ == "__main__":
    main()
