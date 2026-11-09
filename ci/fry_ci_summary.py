# -*- coding: utf-8 -*-
r"""fry_ci_summary.py —— ★461 汇总各平台的 verdict.json：每个登记的平台都必须有结论且通过，才算这个版本「多平台验证通过」。

⚠ 判据是「manifest 登记的平台 ⊆ 通过的平台」，不是「通过数 ≥ N」—— 少一台机器没跑（矩阵被删 / 任务被取消）
  不许静默算过（CLAUDE.md B.1-17 ②：闸报「通过」必须同时报查了多少个，0 个算红）。

用法：python ci/fry_ci_summary.py --version-dir versions/<目录> --verdicts <放各平台 verdict 的目录> --out summary.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def summarize(man: dict, verdicts: list) -> dict:
    by_target = {v["target_str"]: v for v in verdicts}
    rows = []
    for t in man["targets_list"]:
        v = by_target.get(t)
        rows.append({"target_str": t, "ok_bool": bool(v and v["ok_bool"]), "present_bool": v is not None,
                     "machine_str": (v or {}).get("machine_dict", {}).get("platform_str", ""),
                     "checks_int": len((v or {}).get("checks_list", [])),
                     "failed_list": [c["key_str"] for c in (v or {}).get("checks_list", []) if not c["ok_bool"]],
                     "deliverable_dict": (v or {}).get("deliverable_dict", {}), "seconds_float": (v or {}).get("seconds_float")})
    extra = sorted(set(by_target) - set(man["targets_list"]))
    ok = bool(rows) and all(r["ok_bool"] for r in rows)
    return {"schema_str": "fry-gh-summary/1", "version_str": man["version_str"], "version_dir_str": man["dir_str"],
            "algorithm_fingerprint_str": man["algorithm_fingerprint_str"], "ok_bool": ok,
            "passed_int": sum(r["ok_bool"] for r in rows), "expected_int": len(rows), "extra_targets_list": extra,
            "rows_list": rows, "summarized_at_str": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "run_url_str": "%s/%s/actions/runs/%s" % (os.environ.get("GITHUB_SERVER_URL", ""), os.environ.get("GITHUB_REPOSITORY", ""),
                                                     os.environ.get("GITHUB_RUN_ID", "")) if os.environ.get("GITHUB_RUN_ID") else ""}


def markdown(s: dict) -> str:
    lines = ["## %s v%s ｜ %s" % ("✅ 多平台验证通过" if s["ok_bool"] else "❌ 多平台验证没过", s["version_str"], s["version_dir_str"]), "",
             "算法指纹 `%s…` ｜ 通过 %d / %d" % (s["algorithm_fingerprint_str"][:16], s["passed_int"], s["expected_int"]), "",
             "| 平台 | 结论 | 检查项 | 没过的 | 机器 | 耗时 s |", "|---|---|---|---|---|---|"]
    for r in s["rows_list"]:
        lines.append("| %s | %s | %d | %s | %s | %s |" % (r["target_str"], "✅" if r["ok_bool"] else ("❌" if r["present_bool"] else "⚪ 没跑"),
                     r["checks_int"], "、".join(r["failed_list"]) or "—", r["machine_str"], r["seconds_float"]))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser()
    ap.add_argument("--version-dir", required=True)
    ap.add_argument("--verdicts", required=True)
    ap.add_argument("--out", required=True)
    ns = ap.parse_args(argv)
    vdir = Path(ns.version_dir) if Path(ns.version_dir).is_absolute() else REPO / ns.version_dir
    man = json.loads((vdir / "manifest.json").read_text(encoding="utf-8"))
    verdicts = [json.loads(p.read_text(encoding="utf-8")) for p in sorted(Path(ns.verdicts).rglob("verdict.json"))]
    s = summarize(man, verdicts)
    Path(ns.out).write_text(json.dumps(s, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    md = markdown(s)
    print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as f:
            f.write(md)
    return 0 if s["ok_bool"] else 1


if __name__ == "__main__":
    sys.exit(main())
