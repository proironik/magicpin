"""
Build submission.jsonl for the 30 canonical test pairs.

    python generate_submission.py            # uses ./expanded (run dataset/generate_dataset.py first)
    python generate_submission.py --show     # also pretty-print each message
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from composer import compose

ROOT = Path(__file__).parent


def load_dir(d: Path) -> dict:
    out = {}
    for f in sorted(d.glob("*.json")):
        with open(f, encoding="utf-8") as fp:
            data = json.load(fp)
        key = data.get("merchant_id") if d.name == "merchants" else \
            data.get("customer_id") if d.name == "customers" else \
            data.get("id") if d.name == "triggers" else data.get("slug")
        out[key] = data
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", default=str(ROOT / "expanded"))
    ap.add_argument("--out", default=str(ROOT / "submission.jsonl"))
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    data = Path(args.data)
    if not (data / "test_pairs.json").exists():
        print(f"Missing {data / 'test_pairs.json'} — run: python dataset/generate_dataset.py --seed-dir dataset --out expanded")
        return 1
    cats, mers = load_dir(data / "categories"), load_dir(data / "merchants")
    custs, trgs = load_dir(data / "customers"), load_dir(data / "triggers")
    pairs = json.load(open(data / "test_pairs.json", encoding="utf-8"))["pairs"]

    lines = []
    for p in pairs:
        trg = trgs[p["trigger_id"]]
        mer = mers[p["merchant_id"]]
        cat = cats[mer["category_slug"]]
        cust = custs.get(p.get("customer_id")) if p.get("customer_id") else None
        msg = compose(cat, mer, trg, cust)
        rec = {"test_id": p["test_id"], "body": msg["body"], "cta": msg["cta"], "send_as": msg["send_as"],
               "suppression_key": msg["suppression_key"], "rationale": msg["rationale"]}
        lines.append(rec)
        if args.show:
            print(f"\n=== {p['test_id']} | {trg['kind']} | {mer['identity']['name']}"
                  + (f" -> {cust['identity']['name']}" if cust else "") + f" | {msg['send_as']} | {msg['cta']}")
            print(msg["body"])
    with open(args.out, "w", encoding="utf-8") as f:
        for rec in lines:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    print(f"\nWrote {len(lines)} lines -> {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
