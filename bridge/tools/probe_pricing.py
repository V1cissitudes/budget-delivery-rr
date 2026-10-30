"""Smallest real-API check of the pricing chain (prereg v4 §7.5 item 1), BEFORE any dev trajectory: ONE model call with
the frozen public model config through LoggedLitellmModel + the pricing registry, charged to the dry-run ledger with a
call key. Prints the served model string, the raw modelVersion, usage and the pricing status; never prints the key.
Desktop (mini venv, GEMINI_API_KEY set by the author in the environment, never in a file in the repo):
  LITELLM_LOCAL_MODEL_COST_MAP=True ~/code/d1-pilot/.venv-mini/bin/python bridge/tools/probe_pricing.py \
      --spend ~/code/d1-pilot/bridge_dev/spend.json --stop-usd 15 --out ~/code/d1-pilot/bridge_dev/probe"""
import argparse, json, os, sys, uuid
os.environ.setdefault("LITELLM_LOCAL_MODEL_COST_MAP", "True")
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import identity as ID  # noqa: E402


def probe(model, spend, out):
    os.makedirs(out, exist_ok=True)
    msgs = [{"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Run the command `echo OK` with the bash tool."}]
    err = None
    try:
        model.query(msgs)
    except Exception as e:                      # PricingUnavailable or API error: still charge what reached calls.json
        err = f"{type(e).__name__}: {e}"
    for c in model.calls:
        key = spend.call_key(c)
        if c.get("cost") is not None:
            spend.add(c["cost"], key=key)
        else:
            spend.add(c.get("provisional_usd") or 1.0, provisional=True, key=key)
            spend.block(f"probe: {c.get('pricing_status')} served={c.get('model_version')}")
    ID.atomic_json(os.path.join(out, "attempt.json"), {"attempt": spend.prefix})
    rep = {"error": err, "calls": [{k: c.get(k) for k in ("model_version", "raw_model_version", "usage", "raw_usage",
                                                           "pricing_status", "cost", "cost_litellm", "cost_registry")}
                                   for c in model.calls]}
    ID.atomic_json(os.path.join(out, "probe_report.json"), rep)
    return rep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--spend", required=True); ap.add_argument("--stop-usd", type=float, required=True); ap.add_argument("--out", required=True)
    a = ap.parse_args()
    from model_log import LoggedLitellmModel
    from spend import Spend
    cfg = json.load(open(os.path.join(HERE, "config_public_gemini3flash.json")))
    registry, sha = ID.load_registry()
    attempt = "probe-" + uuid.uuid4().hex[:6]
    sp = Spend(os.path.abspath(a.spend), a.stop_usd, dev=True, key_prefix=attempt)
    if sp.exhausted() or sp.blocked():
        raise SystemExit(f"dry-run ledger exhausted or paused: {sp.blocked()}")
    model = LoggedLitellmModel(call_log_path=os.path.join(os.path.abspath(a.out), "calls.json"), registry=registry, **cfg["model"])
    rep = probe(model, sp, os.path.abspath(a.out))
    rep["pricing_registry_sha256"] = sha
    print(json.dumps(rep, indent=1, default=str))


if __name__ == "__main__":
    main()
