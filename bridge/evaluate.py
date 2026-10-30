"""评测这一步（草稿，未封存）：每条轨迹把提交补丁与截止时的 F 交给已封存的 d1/judge.py（只导入，不复制、不改），
按草案 v3 §4 算 S、H、W，原子地写出 result.json。

- 先由 run_one 写的 record.json 与落盘的产物（submission.patch、F_end.patch）重新分类一次，与 record 里的代码
  必须一致（sha256 也要对上），否则按工程失败处理。
- 每条轨迹内按产物字节的 sha256 去重：提交补丁与 F 字节相同就只判一次（同 d1/run_task.py）。
- judge 返回未知（基础设施、不稳定、补丁打不上）或抛异常：只有那一个裁决记未知（§4 规则 3）。
- 身份字段：bridge 代码哈希、d1 代码哈希、臂、题号、k、钉死的镜像 Id、dev 标记、响应返回的模型版本串、
  mini 版本、judge 与 F 的版本号。
台式机单独调用：python evaluate.py --dir TDIR --instance ID --arm ARM --attempt A --image-id sha256:...
       --week-state week.json [--judge-python /path/to/swebench/python] [--dev]"""
import argparse, hashlib, json, os, sys
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import identity as ID  # noqa: E402
from outcome import classify, needs, score  # noqa: E402

J, EX = ID.import_d1("judge", "extract")   # sealed; only imported (judge.judge is called, EX.F_VERSION is read)

EVAL_VERSION = "bridge-eval-v4-draft"
RESUME_KEYS = ("bridge_code_sha256", "d1_code_sha256", "k", "arm", "instance_id", "image_id", "dev")


def _load(p):
    try:
        return json.load(open(p))
    except Exception:
        return None


def _read(p):
    return open(p, "rb").read() if os.path.exists(p) else None


def make_identity(*, instance_id, arm, image_id, dev, bridge_code=None, d1_code=None):
    return {"bridge_code_sha256": bridge_code or ID.bridge_code_sha256(), "d1_code_sha256": d1_code or ID.d1_code_sha256(),
            "k": ID.K, "arm": arm, "instance_id": instance_id, "image_id": image_id, "dev": bool(dev),
            "judge": J.JUDGE_VERSION, "F": EX.F_VERSION, "eval": EVAL_VERSION}


def identity_matches(result, ident):
    i = (result or {}).get("identity") or {}
    return all(i.get(k) == ident[k] for k in RESUME_KEYS)


def _check_record(rec, tdir, instance_id, arm, attempt, ident):
    """Returns (why_list, submission_str_or_None, F_bytes_or_None)."""
    if rec is None:
        return ["NO_RUN_RECORD"], None, None
    why = []
    if not rec.get("terminal"):
        why.append("RUN_RECORD_NOT_TERMINAL")
    if (rec.get("instance_id"), rec.get("arm"), rec.get("attempt")) != (instance_id, arm, attempt):
        why.append("RUN_RECORD_MISMATCH")
    if rec.get("k") != ID.K or rec.get("dev", False) != ident["dev"]:
        why.append("RUN_RECORD_K_OR_DEV_MISMATCH")
    for k in ("bridge_code_sha256", "d1_code_sha256"):
        if rec.get(k) is not None and rec[k] != ident[k]:
            why.append("CODE_IDENTITY_CHANGED_BETWEEN_RUN_AND_EVAL")
            break
    if rec.get("expected_image_id") is not None and rec["expected_image_id"] != ident["image_id"]:
        why.append("IMAGE_PIN_MISMATCH")
    sub_b = _read(os.path.join(tdir, "submission.patch"))
    if rec.get("exit_status") == "Submitted":
        if sub_b is None or hashlib.sha256(sub_b).hexdigest() != rec.get("submission_sha256"):
            why.append("SUBMISSION_ARTIFACT_INCONSISTENT")
    F = None
    if (rec.get("F") or {}).get("available"):
        F = _read(os.path.join(tdir, "F_end.patch"))
        if F is None or hashlib.sha256(F).hexdigest() != rec["F"].get("sha256"):
            why.append("F_ARTIFACT_INCONSISTENT"); F = None
    sub = sub_b.decode("utf-8", errors="surrogateescape") if sub_b is not None else None
    return why, sub, F


def evaluate(tdir, *, instance_id, arm, attempt, image_id, dev=False, judge_fn=None, week=None, img=None,
             bridge_code=None, d1_code=None, tag_suffix="", prior_verdicts=None, on_claim=None, on_verdict=None, write=True):
    """judge_fn has d1 judge.judge's signature: (instance_id, patch_bytes, tag, workdir, *, img, expected_id, week)
    -> {'verdict': 1/0/None, 'reason': ..., ...}. Writes tdir/result.json atomically (unless write=False) and returns it.
    Re-evaluation (addendum I1; panoramic review ①⑤): prior_verdicts {product_sha256: verdict} are FINAL and reused
    without calling the judge; only other products are judged. on_claim(sha) is called BEFORE each such judgment (the
    caller persists that the product's one re-judgment is used; closure review A) and on_verdict(sha, verdict) right
    after it."""
    judge_fn = judge_fn or J.judge
    img = img or ID.image_name(instance_id)
    ident = make_identity(instance_id=instance_id, arm=arm, image_id=image_id, dev=dev,
                          bridge_code=bridge_code, d1_code=d1_code)
    rec = _load(os.path.join(tdir, "record.json"))
    why, sub, F = _check_record(rec, tdir, instance_id, arm, attempt, ident)
    rec = rec or {}
    ident["model_versions"] = rec.get("model_versions") or []
    ident["mini_version"] = rec.get("mini_version")
    ident["raw_model_versions"] = rec.get("raw_model_versions") or []
    ident["pricing_registry_sha256"] = rec.get("pricing_registry_sha256")
    ident["pricing_registry"] = rec.get("pricing_registry")
    eng = bool(why) or bool(rec.get("eng_fail"))
    code, rule = classify(exit_status=rec.get("exit_status"), submission=sub, limit_kind=rec.get("limit_kind"),
                          workspace_patch=F, eng_fail=eng)
    if not why and code != rec.get("code"):
        why.append(f"RECLASSIFY_MISMATCH:{rec.get('code')}->{code}")
        code, rule = classify(exit_status=None, submission=None, limit_kind=None, workspace_patch=None, eng_fail=True)
    need = needs(code, rule, F is not None)
    cache, verdicts = {}, {}

    def verdict(b):
        h = hashlib.sha256(b).hexdigest()
        if h not in cache and prior_verdicts and h in prior_verdicts:
            cache[h] = {k: v for k, v in prior_verdicts[h].items() if k != "product_sha256"}
        if h not in cache:
            if on_claim is not None:                     # raises => the judge is not called
                on_claim(h)
            tag = f"{instance_id.replace('__', '_')}-{arm}-{attempt}-{h[:10]}{tag_suffix}"   # suffix: re-evaluation (I1)
            try:
                cache[h] = judge_fn(instance_id, b, tag, os.path.join(tdir, "eval"), img=img, expected_id=image_id, week=week)
            except Exception as e:                       # judge infrastructure failure => this verdict unknown only
                cache[h] = {"verdict": None, "reason": "JUDGE_EXCEPTION", "error": f"{type(e).__name__}: {e}"}
            if on_verdict is not None:
                on_verdict(h, dict(cache[h]))
        return dict(cache[h], product_sha256=h)

    if need["submission"]:
        verdicts["submission"] = verdict(sub.encode("utf-8", errors="surrogateescape"))
    if need["F"]:
        verdicts["F"] = verdict(F)
    shw = score(code, rule, sub_verdict=(verdicts.get("submission") or {}).get("verdict"),
                f_verdict=(verdicts.get("F") or {}).get("verdict"), f_available=need["F"])
    # F-b1 (secondary sensitivity product, prereg v4 §9): judged only where F-v6 is unusable (no valid snapshot, or
    # its verdict is unknown); everywhere else H_b1 = H and W_b1 = W. S is never affected.
    fb1_meta = rec.get("F_b1") or {}
    Fb1 = None
    if fb1_meta.get("available"):
        Fb1 = _read(os.path.join(tdir, "F_b1.patch"))
        if Fb1 is None or hashlib.sha256(Fb1).hexdigest() != fb1_meta.get("sha256"):
            why.append("F_B1_ARTIFACT_INCONSISTENT"); Fb1 = None
    fv6_problem = code != "ENG_FAIL" and (F is None or (verdicts.get("F") or {}).get("verdict") is None)
    H_b1, W_b1, fb1_used = shw["H"], shw["W"], False
    if code == "ENG_FAIL":
        H_b1 = W_b1 = None
    elif fv6_problem and Fb1 is not None:
        verdicts["F_b1"] = verdict(Fb1); v = verdicts["F_b1"].get("verdict"); fb1_used = True
        W_b1 = v
        if code in ("CAP_STEP_DIFF", "CAP_COST", "FORMAT_EXIT"):
            H_b1 = v
    res = {"instance_id": instance_id, "arm": arm, "attempt": attempt, "k": ID.K, "dev": bool(dev), "identity": ident,
           "code": code, "rule": rule, **shw, "H_b1": H_b1, "W_b1": W_b1, "F_b1_used": fb1_used,
           "why": sorted(set(why) | set(rec.get("why") or [])),
           "resource_stop": bool(rec.get("resource_stop")), "pricing_failure": rec.get("pricing_failure"),
           "verdicts": verdicts,
           "n_unique_products_judged": len(cache),
           "run": {k: rec.get(k) for k in ("exit_status", "limit_kind", "n_calls", "n_calls_at_stop", "agent_cost_usd",
                                           "api_cost_usd", "tokens", "n_api_responses", "started", "finished",
                                           "error", "F", "unpriced_calls", "provisional_usd", "pricing_statuses",
                                           "blind_submit")},
           "budget_log_consistent": all(r.get("consistent") for r in (rec.get("budget_log") or [])),
           "evaluated": ID.now(), "terminal": True}
    if write:
        ID.atomic_json(os.path.join(tdir, "result.json"), res)
    return res


def main():
    ap = argparse.ArgumentParser()
    for x in ("--dir", "--instance", "--arm", "--attempt", "--image-id", "--week-state"):
        ap.add_argument(x, required=True)
    ap.add_argument("--judge-python", default=sys.executable)
    ap.add_argument("--dev", action="store_true")
    a = ap.parse_args()
    ID.check_d1()
    WK = ID.import_d1("week")
    jf = lambda *x, **kw: J.judge(*x, python=a.judge_python, **kw)   # noqa: E731
    r = evaluate(os.path.abspath(a.dir), instance_id=a.instance, arm=a.arm, attempt=a.attempt, image_id=a.image_id,
                 dev=a.dev, judge_fn=jf, week=WK.Week(a.week_state))
    print(json.dumps({k: r[k] for k in ("instance_id", "arm", "code", "S", "H", "W")}))


if __name__ == "__main__":
    main()
