# -*- coding: utf-8 -*-
r"""fry_gh_publish.py —— ★461 本机一键：把打包仓出好的二进制放进本公开仓 → GitHub 多平台真机验收 → 结论与交付件带回本机。

【为什么要有这个仓】交叉编译（zig）能在 Windows 上编出 macOS / Linux 的库，但**结果对不对只能在目标平台上真跑 golden**；
  手边没有 Mac ⇒ 借 GitHub 的 macOS（Apple 芯片 + Intel）、Linux（x64 + ARM）、Windows 机器当目标机。
  本仓是公开仓 ⇒ **只放二进制 + 验收脚本，不放算法源码**（stage 有「源码泄漏闸」，带了源码直接拒绝）。

【六步（all = 依次全做；每一步也能单独跑）】
  stage    从打包仓 out/ 收集本平台无关的全部输入 → versions/_YYMMDD_HHMM_v<版本>_<关键词>/inputs/ + manifest.json
           闸：版本号 / 算法指纹处处一致 · ★源码泄漏闸（压缩包里不许有 cpp/ 目录，也不许有与算法源码同内容的文件）
  push     git commit + push（只提交 versions/<目录>/ 与本仓脚本）
  ci       gh workflow run fry_verify.yml → 等它跑完 → gh run download 到 versions/<目录>/verified/
  record   复核下载回来的结论（每个登记平台都 ✓、交付件 sha256 = verdict）→ 写 versions/history.json → commit + push
  release  （可选）gh release 只挂交付件 → 下载回本机核对 → ★按你的规则：GitHub 的 release 必带「Source code」压缩包，
           所以默认**下载后把 release 和 tag 删掉**（--keep-release 保留；这里的 Source code 只是本仓的二进制和脚本，不是算法源码）
  deliver  把验收通过的交付件交给打包仓：<打包仓>/out/github_verified/<目录>/<平台>/（再由打包仓的交付脚本送进算法仓）

用法（Windows，先 `gh auth login`；本仓在打包期间设为 public）：
  python tools\fry_gh_publish.py all
  python tools\fry_gh_publish.py stage --source-repo ..\fry_p_3d_rip_260915_auto_build_dll --keyword 水转印加网
  python tools\fry_gh_publish.py ci --version-dir _261109_0130_v0.2.0_水转印加网
  python tools\fry_gh_publish.py status
只用 Python 标准库 + git + gh。
"""
from __future__ import annotations

import argparse
import calendar
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

REPO = Path(__file__).resolve().parents[1]
VERSIONS = REPO / "versions"
HISTORY = VERSIONS / "history.json"
PRODUCT = json.loads((REPO / "ci" / "product.json").read_text(encoding="utf-8"))
WORKFLOW = "fry_verify.yml"
TEXT_SUFFIX_SET = {".h", ".hpp", ".c", ".cc", ".cpp", ".inc", ".txt", ".cmake", ".md"}
EMPTY_SHA = hashlib.sha256(b"").hexdigest()


def say(text: str) -> None:
    print(text, flush=True)


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def run(cmd: List[str], cwd: Path = REPO, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    say("$ " + " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(cwd), text=True, encoding="utf-8", errors="replace",
                       capture_output=capture)
    if check and r.returncode != 0:
        raise SystemExit("✗ 命令失败（rc=%d）：%s\n%s" % (r.returncode, " ".join(cmd), (r.stdout or "") + (r.stderr or "") if capture else ""))
    return r


# ───────────────────────────── 源码泄漏闸（纯函数 + 读压缩包） ─────────────────────────────
def lock_hashes(lock: Dict[str, Any]) -> Dict[str, str]:
    """两个打包仓的锁文件格式不同（加网 files_dict / 仿真 file_sha256_dict）⇒ 统一成 {相对路径: sha256}。"""
    raw = lock.get("file_sha256_dict") or lock.get("files_dict") or {}
    out = {}
    for rel, v in raw.items():
        h = v if isinstance(v, str) else (v.get("sha256_str") or v.get("sha256") or "")
        if h:
            out[rel] = h
    return out


def archive_members(path: Path) -> List[Tuple[str, bytes]]:
    rows = []
    if path.name.endswith(".zip"):
        with zipfile.ZipFile(path) as z:
            for zi in z.infolist():
                if not zi.is_dir():
                    rows.append((zi.filename, z.read(zi)))
    else:
        with tarfile.open(path, "r:*") as t:
            for m in t.getmembers():
                if m.isfile():
                    rows.append((m.name, t.extractfile(m).read()))
    return rows


def leak_problems(members: List[Tuple[str, bytes]], lock: Dict[str, Any]) -> List[str]:
    """★461 压缩包里有没有算法源码：①路径里有 cpp/ 目录（= 载荷目录）②内容与算法源码任一文件相同（换行规范化后比 sha256）。
    两条互相独立：①防「整个目录带进来」，②防「改了名 / 换了位置带进来」。"""
    want = {h for h in lock_hashes(lock).values() if h != EMPTY_SHA}
    probs = []
    for name, data in members:
        parts = name.replace("\\", "/").split("/")
        if "cpp" in parts[:-1]:
            probs.append("含载荷目录 cpp/：%s" % name)
            continue
        suf = Path(name).suffix.lower()
        norm = data.replace(b"\r\n", b"\n") if (suf in TEXT_SUFFIX_SET or Path(name).name == "CMakeLists.txt") else data
        if sha256_bytes(norm) in want:
            probs.append("内容 = 算法源码：%s" % name)
    return probs


# ───────────────────────────── 收集输入（两个产品各一套规则） ─────────────────────────────
def collect_halftone(src: Path) -> Dict[str, Any]:
    info = json.loads((src / "out" / "kit" / "kit_info.json").read_text(encoding="utf-8"))
    ver = info["version_str"]
    kit = src / "out" / "kit" / ("fry-halftone-%s-verify-kit.tar.gz" % ver)
    win = src / "out" / ("fry-halftone-%s-win32-x64.zip" % ver)
    for p in (kit, win):
        if not p.is_file():
            raise SystemExit("✗ 缺 %s —— 先在打包仓跑 fry_release.py（--linux prebuilt --mac-kit）" % p)
    tags = sorted({row["tag_str"] for row in info["targets_dict"].values()})
    return {"version_str": ver, "kit_fingerprint_str": info["algorithm_fingerprint_str"],
            "inputs": {"kit": kit, "win32-x64": win}, "targets_list": ["win32-x64"] + tags}


def collect_sim(src: Path) -> Dict[str, Any]:
    ver = (src / "VERSION").read_text(encoding="utf-8").strip()
    inputs = {"win32-x64": src / "out" / "package" / ("fry_sim_sdk-%s-win32-x64.zip" % ver)}
    for t in ("linux-x64", "linux-arm64", "darwin-arm64", "darwin-x64"):
        inputs[t] = src / "out" / "cross" / t / ("fry_sim_target_kit-%s-%s.zip" % (ver, t))
    miss = [str(p) for p in inputs.values() if not p.is_file()]
    if miss:
        raise SystemExit("✗ 缺 %s —— 先在打包仓跑 fry_release.bat" % miss)
    return {"version_str": ver, "kit_fingerprint_str": "", "inputs": inputs, "targets_list": list(inputs)}


def git_head(repo: Path) -> str:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else ""


def load_history() -> Dict[str, Any]:
    if HISTORY.is_file():
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    return {"schema_str": "fry-gh-history/1", "product_str": PRODUCT["product_str"], "versions_list": []}


def save_history(h: Dict[str, Any]) -> None:
    VERSIONS.mkdir(exist_ok=True)
    HISTORY.write_text(json.dumps(h, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def latest_dir() -> str:
    dirs = sorted(p.name for p in VERSIONS.glob("_*") if (p / "manifest.json").is_file())
    if not dirs:
        raise SystemExit("✗ versions/ 里还没有版本目录 —— 先 stage")
    return dirs[-1]


def cmd_stage(ns) -> str:
    src = Path(ns.source_repo or (REPO.parent / PRODUCT["source_repo_name_str"])).resolve()
    lock = json.loads((src / "cpp.lock.json").read_text(encoding="utf-8"))
    fp = lock["tree_sha256_str"]
    got = (collect_halftone if PRODUCT["product_str"] == "halftone" else collect_sim)(src)
    if got["kit_fingerprint_str"] and got["kit_fingerprint_str"] != fp:
        raise SystemExit("✗ 验收包的算法指纹 %s ≠ 打包仓锁文件 %s —— 验收包过期，先重出" % (got["kit_fingerprint_str"][:16], fp[:16]))
    # ★ 源码泄漏闸：公开仓的第一道闸
    leaks = []
    for key, p in got["inputs"].items():
        for prob in leak_problems(archive_members(p), lock):
            leaks.append("%s（%s）：%s" % (key, p.name, prob))
    if leaks:
        raise SystemExit("✗ 源码泄漏闸：下面这些会把算法源码推到公开仓，拒绝 stage\n  " + "\n  ".join(leaks[:30]))
    say("✓ 源码泄漏闸：%d 个压缩包里没有 cpp/ 目录，也没有与 %d 个算法源码文件同内容的文件"
        % (len(got["inputs"]), len(lock_hashes(lock))))
    sha = {k: sha256_file(p) for k, p in got["inputs"].items()}
    # 同一版本 + 同一批二进制已经登记过 ⇒ 复用（不重复占仓库体积）
    for row in load_history()["versions_list"]:
        if row["version_str"] == got["version_str"] and row.get("inputs_sha256_dict") == sha and not ns.force:
            say("✓ 这批二进制已登记过：versions/%s（%s）—— 复用；要另起一份用 --force" % (row["dir_str"], row.get("status_str")))
            return row["dir_str"]
    stamp = ns.stamp or time.strftime("%y%m%d_%H%M")
    name = "_%s_v%s_%s" % (stamp, got["version_str"], ns.keyword or PRODUCT["keyword_str"])
    vdir = VERSIONS / name
    if vdir.exists():
        raise SystemExit("✗ %s 已存在" % vdir)
    (vdir / "inputs").mkdir(parents=True)
    inputs_dict = {}
    for key, p in got["inputs"].items():
        shutil.copy2(p, vdir / "inputs" / p.name)
        inputs_dict[key] = {"file_str": "inputs/" + p.name, "sha256_str": sha[key], "size_int": p.stat().st_size}
    man = {"schema_str": "fry-gh-version/1", "product_str": PRODUCT["product_str"], "title_str": PRODUCT["title_str"],
           "version_str": got["version_str"], "dir_str": name, "keyword_str": ns.keyword or PRODUCT["keyword_str"],
           "created_at_str": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "algorithm_fingerprint_str": fp,
           "algo_commit_str": lock.get("source_dict", {}).get("commit_str", ""), "source_repo_commit_str": git_head(src),
           "inputs_dict": inputs_dict, "targets_list": got["targets_list"]}
    (vdir / "manifest.json").write_text(json.dumps(man, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    h = load_history()
    h["versions_list"].append({"dir_str": name, "version_str": got["version_str"], "algorithm_fingerprint_str": fp,
                               "inputs_sha256_dict": sha, "status_str": "staged", "staged_at_str": man["created_at_str"]})
    save_history(h)
    say("✓ stage → versions/%s（%d 个输入，%d 个平台：%s）" % (name, len(inputs_dict), len(got["targets_list"]), "、".join(got["targets_list"])))
    return name


WORKFLOW_SRC = REPO / "ci" / "workflows" / WORKFLOW          # 正本（远程工具写不了 .github/，所以正本放 ci/）
WORKFLOW_DST = REPO / ".github" / "workflows" / WORKFLOW


def install_workflow() -> bool:
    """★461 把 ci/workflows/ 里的工作流正本复制到 .github/workflows/（不同才复制）；返回是否改动。"""
    new = WORKFLOW_SRC.read_bytes()
    if WORKFLOW_DST.is_file() and WORKFLOW_DST.read_bytes() == new:
        return False
    WORKFLOW_DST.parent.mkdir(parents=True, exist_ok=True)
    WORKFLOW_DST.write_bytes(new)
    say("· 工作流：ci/workflows/%s → .github/workflows/%s（正本有更新）" % (WORKFLOW, WORKFLOW))
    return True


def cmd_push(ns, vname: str, message: str = "") -> None:
    install_workflow()
    # ⚠ git add 里只要有一个路径不存在，整条命令失败、一个都不加 ⇒ 只传存在的
    paths = [p for p in ["versions/" + vname, "versions/history.json", "ci", ".github", "tools", "README.md",
                         ".gitattributes", ".gitignore"] if (REPO / p).exists()] + [p.name for p in REPO.glob("*.bat")]
    run(["git", "add", "-A", "--"] + paths)
    st = run(["git", "status", "--porcelain"], capture=True)
    if st.stdout.strip():
        msg = message or ("stage %s" % vname)
        if os.environ.get("FRY_GIT_TRAILER"):                            # 可选：提交尾注（例如署名）
            msg += "\n\n" + os.environ["FRY_GIT_TRAILER"]
        run(["git", "commit", "-m", msg])
    run(["git", "push", "origin", "HEAD"])


def find_run(sha: str, since: float, timeout_s: int = 180) -> str:
    t_end = time.time() + timeout_s
    while time.time() < t_end:
        r = run(["gh", "run", "list", "--workflow", WORKFLOW, "--event", "workflow_dispatch", "--limit", "20",
                 "--json", "databaseId,headSha,createdAt,status"], capture=True)
        for row in json.loads(r.stdout or "[]"):
            created = calendar.timegm(time.strptime(row["createdAt"][:19], "%Y-%m-%dT%H:%M:%S"))   # gh 给的是 UTC
            if row["headSha"] == sha and created >= since - 120:
                return str(row["databaseId"])
        time.sleep(5)
    raise SystemExit("✗ 触发后 %d s 还没看到对应的 CI 运行（网页 Actions 页看看）" % timeout_s)


def cmd_ci(ns, vname: str) -> str:
    sha = git_head(REPO)
    t0 = time.time()
    run(["gh", "workflow", "run", WORKFLOW, "--ref", ns.ref, "-f", "version_dir=" + vname])
    rid = find_run(sha, t0)
    say("· CI 运行 %s（%s）—— 等它跑完（5 台机器并行，一般 5~15 分钟）" % (rid, vname))
    run(["gh", "run", "watch", rid, "--interval", "20"], check=False)
    dst = VERSIONS / vname / "verified"
    if dst.exists():
        shutil.rmtree(dst)
    run(["gh", "run", "download", rid, "-D", str(dst)])
    (dst / "run_id.txt").write_text(rid + "\n", encoding="utf-8", newline="\n")
    view = run(["gh", "run", "view", rid, "--json", "conclusion,url,jobs"], capture=True, check=False)
    (dst / "run_view.json").write_text(view.stdout or "{}", encoding="utf-8", newline="\n")
    return rid


def check_verified(vdir: Path) -> Dict[str, Any]:
    """复核 CI 带回来的东西（不只信 CI 的绿勾）：summary ✓、每个登记平台都有 verdict 且 ✓、交付件 sha256 = verdict 记的。"""
    man = json.loads((vdir / "manifest.json").read_text(encoding="utf-8"))
    ver = vdir / "verified"
    sp = ver / "verified-summary" / "summary.json"
    summary = json.loads(sp.read_text(encoding="utf-8")) if sp.is_file() else {}
    rows, probs = [], []
    for t in man["targets_list"]:
        vp = ver / ("verified-" + t) / "verdict.json"
        if not vp.is_file():
            probs.append("%s 没有 verdict（这台机器没跑 / 没上传）" % t)
            continue
        v = json.loads(vp.read_text(encoding="utf-8"))
        if not v.get("ok_bool"):
            probs.append("%s 验收没过：%s" % (t, [c["key_str"] for c in v.get("checks_list", []) if not c["ok_bool"]]))
        if v.get("algorithm_fingerprint_str") != man["algorithm_fingerprint_str"]:
            probs.append("%s 的 verdict 指纹与 manifest 不符" % t)
        for n, h in v.get("deliverable_dict", {}).items():
            p = vp.parent / "deliverable" / n
            if not p.is_file() or sha256_file(p) != h:
                probs.append("%s 交付件 %s 下载后 sha256 不符" % (t, n))
        if v.get("ok_bool") and not v.get("deliverable_dict"):
            probs.append("%s 说通过了但没有交付件" % t)
        rows.append({"target_str": t, "ok_bool": bool(v.get("ok_bool")), "checks_int": len(v.get("checks_list", [])),
                     "machine_str": v.get("machine_dict", {}).get("platform_str", ""), "deliverable_dict": v.get("deliverable_dict", {})})
    if not summary.get("ok_bool"):
        probs.append("CI 汇总没过（或没下载到 summary.json）")
    return {"ok_bool": not probs, "problems_list": probs, "rows_list": rows, "summary_dict": summary}


def cmd_record(ns, vname: str) -> Dict[str, Any]:
    vdir = VERSIONS / vname
    res = check_verified(vdir)
    rid = (vdir / "verified" / "run_id.txt").read_text(encoding="utf-8").strip() if (vdir / "verified" / "run_id.txt").is_file() else ""
    (vdir / "verified" / "local_check.json").write_text(json.dumps(res, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
    h = load_history()
    for row in h["versions_list"]:
        if row["dir_str"] == vname:
            row.update(status_str="verified" if res["ok_bool"] else "failed", run_id_str=rid,
                       verified_at_str=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                       platforms_dict={r["target_str"]: r["ok_bool"] for r in res["rows_list"]})
    save_history(h)
    for r in res["rows_list"]:
        say("  %s %-13s %3d 项检查 ｜ %s" % ("✓" if r["ok_bool"] else "✗", r["target_str"], r["checks_int"], r["machine_str"]))
    for p in res["problems_list"]:
        say("  ✗ " + p)
    say(("✓ 多平台验证通过：%s" if res["ok_bool"] else "✗ 多平台验证没过：%s") % vname)
    if not ns.no_push:
        cmd_push(ns, vname, "verified %s: %s" % (vname, "ok" if res["ok_bool"] else "FAILED"))
    return res


def cmd_release(ns, vname: str) -> None:
    vdir = VERSIONS / vname
    res = check_verified(vdir)
    if not res["ok_bool"]:
        raise SystemExit("✗ 没通过验收的版本不发布：%s" % res["problems_list"])
    man = json.loads((vdir / "manifest.json").read_text(encoding="utf-8"))
    files = [vdir / "verified" / ("verified-" + r["target_str"]) / "deliverable" / n for r in res["rows_list"] for n in r["deliverable_dict"]]
    tag = "%s-v%s-%s" % (PRODUCT["product_str"], man["version_str"], vname.split("_v")[0].strip("_"))
    notes = "%s v%s ｜ 算法指纹 %s… ｜ %d 个平台真机验收通过（%s）。只含编译好的二进制。" % (
        PRODUCT["title_str"], man["version_str"], man["algorithm_fingerprint_str"][:16], len(res["rows_list"]),
        "、".join(r["target_str"] for r in res["rows_list"]))
    run(["gh", "release", "create", tag, "--title", "%s v%s（%s）" % (PRODUCT["title_str"], man["version_str"], vname), "--notes", notes]
        + [str(f) for f in files])
    dl = vdir / "release_download"
    if dl.exists():
        shutil.rmtree(dl)
    run(["gh", "release", "download", tag, "-D", str(dl)])
    bad = [f.name for f in files if not (dl / f.name).is_file() or sha256_file(dl / f.name) != sha256_file(f)]
    if bad:
        raise SystemExit("✗ release 下载回来的文件与上传的不一致：%s（release 保留，便于排查）" % bad)
    say("✓ release %s：%d 个交付件，下载回本机核对一致 → %s" % (tag, len(files), dl))
    if not ns.keep_release:
        # 你的规则：release 会附带 GitHub 自动生成的 Source code 压缩包 ⇒ 下载好之后删掉 release 与 tag
        run(["gh", "release", "delete", tag, "--yes", "--cleanup-tag"])
        say("✓ 已删除 release 与 tag（--keep-release 可保留）；交付件在 versions/%s/release_download/ 与 verified/ 两处" % vname)


def cmd_deliver(ns, vname: str) -> Path:
    vdir = VERSIONS / vname
    res = check_verified(vdir)
    if not res["ok_bool"]:
        raise SystemExit("✗ 没通过验收的版本不交付：%s" % res["problems_list"])
    src = Path(ns.source_repo or (REPO.parent / PRODUCT["source_repo_name_str"])).resolve()
    dst = src / "out" / "github_verified" / vname
    if dst.exists():
        shutil.rmtree(dst)
    for r in res["rows_list"]:
        d = dst / r["target_str"]
        d.mkdir(parents=True)
        base = vdir / "verified" / ("verified-" + r["target_str"])
        for n in r["deliverable_dict"]:
            shutil.copy2(base / "deliverable" / n, d / n)
        shutil.copy2(base / "verdict.json", d / "verdict.json")
    shutil.copy2(vdir / "manifest.json", dst / "manifest.json")
    say("✓ 交付件 → %s（%d 个平台）" % (dst, len(res["rows_list"])))
    return dst


def cmd_status(ns) -> None:
    h = load_history()
    say("%s ｜ %d 个版本目录" % (PRODUCT["title_str"], len(h["versions_list"])))
    for row in h["versions_list"]:
        plats = row.get("platforms_dict", {})
        say("  %-46s v%-8s %-9s %s" % (row["dir_str"], row["version_str"], row.get("status_str"),
            " ".join(("✓" if ok else "✗") + t for t, ok in plats.items())))


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="二进制 → GitHub 多平台真机验收 → 带回本机")
    ap.add_argument("cmd", choices=["all", "stage", "push", "ci", "record", "release", "deliver", "status"])
    ap.add_argument("--source-repo", default="", help="打包仓目录（默认本仓同级的 %s）" % PRODUCT["source_repo_name_str"])
    ap.add_argument("--version-dir", default="", help="versions/ 下的目录名（默认最新的）")
    ap.add_argument("--keyword", default="")
    ap.add_argument("--stamp", default="", help="版本目录时间戳 YYMMDD_HHMM（默认现在）")
    ap.add_argument("--ref", default="main")
    ap.add_argument("--force", action="store_true", help="同一批二进制也另起一个版本目录")
    ap.add_argument("--no-release", action="store_true", help="all 时跳过 release")
    ap.add_argument("--keep-release", action="store_true", help="release 下载核对后不删除")
    ap.add_argument("--no-push", action="store_true")
    ap.add_argument("--result-json", default="")
    ns = ap.parse_args(argv)
    os.chdir(str(REPO))
    result: Dict[str, Any] = {"cmd_str": ns.cmd}
    if ns.cmd == "status":
        cmd_status(ns)
        return 0
    if ns.cmd in ("all", "stage"):
        vname = cmd_stage(ns)
    else:
        vname = ns.version_dir or latest_dir()
    result["version_dir_str"] = vname
    if ns.cmd in ("all", "push"):
        cmd_push(ns, vname)
    if ns.cmd in ("all", "ci"):
        result["run_id_str"] = cmd_ci(ns, vname)
    if ns.cmd in ("all", "record"):
        result["record_dict"] = cmd_record(ns, vname)
    ok = result.get("record_dict", {}).get("ok_bool", True)
    if ok and ((ns.cmd == "all" and not ns.no_release) or ns.cmd == "release"):
        cmd_release(ns, vname)
    if ok and ns.cmd in ("all", "deliver"):
        result["deliver_dir_str"] = str(cmd_deliver(ns, vname))
    result["ok_bool"] = ok
    if ns.result_json:
        Path(ns.result_json).write_text(json.dumps(result, ensure_ascii=False, indent=1, default=str) + "\n", encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
