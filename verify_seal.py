"""Recompute the sealed code identity of bridge/ and compare with the seal records. Run from the repo root:
python3 -I verify_seal.py"""
import sys, os, json
sys.path.insert(0, "bridge"); import identity as ID
cur = ID.bridge_code_sha256()
s2 = json.load(open("docs/prereg/2026-10-20_bridge_seal2_record.json"))["bridge_code_sha256"]
print("bridge/ identity now :", cur)
print("seal2 record (final) :", s2)
print("MATCH" if cur == s2 else "MISMATCH")
