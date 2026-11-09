# -*- coding: utf-8 -*-
r"""fry_ci_target.py —— ★461 在 GitHub 的一台目标机上验收一个版本目录里「本平台」那份预编二进制。

【这一步回答什么】交叉编译（zig）只证明「编得出、格式对」；**结果对不对必须在目标平台上真跑 golden**。
  本仓是公开仓、只放二进制（不放算法源码）⇒ GitHub 的 Linux / macOS / Windows 机器就是我们的「目标机」。

【输入】versions/<版本目录>/manifest.json + inputs/（由 tools/fry_gh_publish.py stage 放进来）
【输出】--out 目录：
    verdict.json      这台机器的验收结论（ok_bool、每条检查、机器信息、交付件 sha256）
    deliverable/      验收通过的交付件（加网 .tar.zst / .zip；仿真 .zip）+ SHA256SUMS.txt
    logs/             原始日志（验收失败时看这里）
【判据】全部来自两个打包仓已有的冒烟（加网 ★409 九道闸 + golden 23 件逐字节；仿真八条 + golden 9 用例 18 次），
  本脚本**不另立判据**，只负责：①核对输入没被改（sha256 = manifest）②调对的那条验收路径 ③把结论落成机读文件。

用法（CI 里由 .github/workflows/fry_verify.yml 调）：
  python ci/fry_ci_target.py --version-dir versions/<目录> --target linux-x64 --out _ci_out/linux-x64
只用 Python 标准库。退出码：0 = 本平台验收通过；1 = 没过；2 = 输入有问题。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import time
import zipfile
from pathlib import Path
from typing import Any, Dict, List

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
TARGET_LIST = ["win32-x64", "linux-x64", "linux-arm64", "darwin-arm64", "darwin-x64"]
HALFTONE_KIT_NAME_DICT = {"darwin-arm64": "macos-arm64", "darwin-x64": "macos-x64"}   # 加网验收包里的目录名


def sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def host_target() -> str:
    """这台机器是哪个平台（与打包仓同一套叫法）。"""
    s, m = platform.system(), platform.machine().lower()
    arch = "arm64" if m in ("arm64", "aarch64") else "x64"
    return {"Windows": "win32", "Linux": "linux", "Darwin": "darwin"}.get(s, s.lower()) + "-" + arch


def machine_info() -> Dict[str, str]:
    info = {"platform_str": platform.platform(), "machine_str": platform.machine(), "python_str": sys.version.split()[0],
            "runner_str": os.environ.get("RUNNER_NAME", ""), "image_str": os.environ.get("ImageOS", "") + " " + os.environ.get("ImageVersion", "")}
    if platform.system() == "Darwin":
        info["macos_str"] = platform.mac_ver()[0]
    if platform.system() == "Linux":
        try:
            info["glibc_str"] = " ".join(platform.libc_ver())
        except Exception:                                                # noqa: BLE001
            pass
    return info


def run(cmd: List[str], cwd: Path, log: Path, env: Dict[str, str] = None) -> int:
    """跑一条命令，输出同时进日志和 CI 控制台；返回退出码。"""
    print("$ " + " ".join(cmd), flush=True)
    t0 = time.time()
    with open(log, "a", encoding="utf-8", errors="replace") as f:
        f.write("\n$ %s\n" % " ".join(cmd))
        p = subprocess.Popen(cmd, cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env)
        for raw in p.stdout:
            line = raw.decode("utf-8", errors="replace")
            sys.stdout.write(line)
            f.write(line)
        rc = p.wait()
        f.write("[rc=%d, %.1f s]\n" % (rc, time.time() - t0))
    sys.stdout.flush()
    return rc


def extract(archive: Path, dst: Path) -> None:
    dst.mkdir(parents=True, exist_ok=True)
    name = archive.name
    if name.endswith(".zip"):
        with zipfile.ZipFile(archive) as z:
            z.extractall(dst)
    elif name.endswith(".tar.gz") or name.endswith(".tgz"):
        with tarfile.open(archive, "r:gz") as t:
            t.extractall(dst)
    else:
        raise SystemExit("✗ 不认识的压缩格式：%s" % name)


def only_child(d: Path) -> Path:
    kids = [p for p in d.iterdir() if p.is_dir()]
    if len(kids) != 1:
        raise SystemExit("✗ %s 里应当只有一个顶层目录，实际 %s" % (d, [p.name for p in kids]))
    return kids[0]


def check_inputs(vdir: Path, man: Dict[str, Any]) -> List[str]:
    """输入闸：inputs/ 里每个文件的 sha256 = manifest（防止推上来的二进制与登记的不是同一批）。"""
    probs = []
    for key, row in man["inputs_dict"].items():
        p = vdir / row["file_str"]
        if not p.is_file():
            probs.append("缺输入 %s：%s" % (key, row["file_str"]))
        elif sha256_file(p) != row["sha256_str"]:
            probs.append("输入 %s 的 sha256 与 manifest 不符（被改过 / 推错了）" % key)
    return probs


def golden_count_check(log: Path, kit: Path) -> Dict[str, Any]:
    """★461 加网：从日志读「golden x / y 件逐字节一致」，要求 x = y = golden.json 登记的件数（闸要报查了多少个，B.1-17②）。"""
    import re
    want = len(json.loads((kit / "tests" / "data" / "golden.json").read_text(encoding="utf-8"))["case_list"])
    txt = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
    rows = [(int(a), int(b)) for a, b in re.findall(r"golden[^0-9\n]{0,12}(\d+) / (\d+) 件逐字节一致", txt)]
    ok = bool(rows) and all(a == b == want for a, b in rows)
    return {"key_str": "golden 件数", "ok_bool": ok,
            "detail_str": "登记 %d 件 ｜ 日志里 %s" % (want, "、".join("%d/%d" % r for r in rows) or "没找到计数行")}


# ───────────────────────────── 加网（fry-halftone） ─────────────────────────────
def halftone_unix(vdir: Path, man: Dict[str, Any], target: str, work: Path, out: Path, checks: List[Dict[str, Any]]) -> bool:
    log = out / "logs" / "verify.log"
    extract(vdir / man["inputs_dict"]["kit"]["file_str"], work)
    kit = only_child(work)
    rc = run(["bash", "run_on_target.sh"], kit, log, env=dict(os.environ, FRY_SDK_PYTHON=sys.executable))
    checks.append({"key_str": "run_on_target.sh", "ok_bool": rc == 0, "detail_str": "rc=%d（冒烟九道闸 + golden 23 件逐字节 + 命令行契约 44 项）" % rc})
    kt = HALFTONE_KIT_NAME_DICT.get(target, target)
    res = kit / "results" / kt
    pkg = res / ("fry-halftone-%s-%s.tar.zst" % (man["version_str"], target))
    for f in ("verify.log", "build_report.json", "target_env.txt", "SHA256SUMS.txt"):
        if (res / f).is_file():
            shutil.copy2(res / f, out / "logs" / f)
    checks.append(golden_count_check(log, kit))
    ok = rc == 0 and pkg.is_file()
    if (res / "build_report.json").is_file():
        rep = json.loads((res / "build_report.json").read_text(encoding="utf-8"))
        checks.append({"key_str": "build_report", "ok_bool": rep.get("platform_str", target) == target,
                       "detail_str": "平台 %s ｜ 版本 %s" % (rep.get("platform_str"), rep.get("version_str"))})
    if ok:
        shutil.copy2(pkg, out / "deliverable" / pkg.name)
    checks.append({"key_str": "交付件", "ok_bool": ok, "detail_str": pkg.name if ok else "没有生成 %s" % pkg.name})
    return ok and all(c["ok_bool"] for c in checks)


def halftone_windows(vdir: Path, man: Dict[str, Any], target: str, work: Path, out: Path, checks: List[Dict[str, Any]]) -> bool:
    """Windows 的 dll 是本机 MSVC 编的（不是交叉产物）⇒ 用验收包里同一套测试脚本对着它跑一遍。"""
    log = out / "logs" / "verify.log"
    extract(vdir / man["inputs_dict"]["kit"]["file_str"], work / "kit")
    extract(vdir / man["inputs_dict"]["win32-x64"]["file_str"], work / "pkg")
    kit, pkg = only_child(work / "kit"), only_child(work / "pkg")
    lib, cli = pkg / "bin" / "fry_halftone.dll", pkg / "bin" / "fry-halftone-cli.exe"
    ver = man["version_str"]
    sys.path.insert(0, str(kit / "tools"))
    try:
        import fry_param_schema as PS                                    # noqa: E402  验收包自带，只用标准库
        fp_lib = PS.read_lib_text(lib, "FryHalftone_GetAlgorithmFingerprint")
    except Exception as exc:                                             # noqa: BLE001
        fp_lib = "（读不出：%r）" % exc
    fp_want = man["algorithm_fingerprint_str"]
    checks.append({"key_str": "指纹闸", "ok_bool": fp_lib == fp_want, "detail_str": "库自报 %s ｜ 登记 %s" % (fp_lib[:16], fp_want[:16])})
    steps = [("包自检 verify_delivery.py", [sys.executable, str(pkg / "verify_delivery.py")], pkg),
             ("C 接口用例 test_c_abi", [sys.executable, str(kit / "tests" / "test_c_abi.py"), "--lib", str(lib), "--version", ver], kit),
             ("golden 逐字节 test_golden", [sys.executable, str(kit / "tests" / "test_golden.py"), "--lib", str(lib)], kit),
             ("命令行契约 test_cli_contract", [sys.executable, str(kit / "tests" / "test_cli_contract.py"), "--cli", str(cli), "--version", ver], kit)]
    for key, cmd, cwd in steps:
        rc = run(cmd, cwd, log)
        checks.append({"key_str": key, "ok_bool": rc == 0, "detail_str": "rc=%d" % rc})
    checks.append(golden_count_check(log, kit))
    ok = all(c["ok_bool"] for c in checks)
    if ok:
        src = vdir / man["inputs_dict"]["win32-x64"]["file_str"]
        shutil.copy2(src, out / "deliverable" / src.name)
    return ok


# ───────────────────────────── 仿真（fry_sim） ─────────────────────────────
def sim_unix(vdir: Path, man: Dict[str, Any], target: str, work: Path, out: Path, checks: List[Dict[str, Any]]) -> bool:
    log = out / "logs" / "verify.log"
    extract(vdir / man["inputs_dict"][target]["file_str"], work)
    kit = only_child(work)
    # zipfile 不还原可执行位 ⇒ run_on_target.sh 自己会 chmod；python3 指到 CI 装的那个
    env = dict(os.environ)
    env["PATH"] = str(Path(sys.executable).parent) + os.pathsep + env.get("PATH", "")
    rc = run(["sh", "run_on_target.sh"], kit, log, env=env)
    checks.append({"key_str": "run_on_target.sh", "ok_bool": rc == 0, "detail_str": "rc=%d（冒烟八条：导出面 / rpath / 自报指纹 / golden 逐位 1 线程 + 多线程 / C / Python 示例）" % rc})
    rep_p = kit / "out" / ("build_report_%s.json" % target)
    rep = json.loads(rep_p.read_text(encoding="utf-8")) if rep_p.is_file() else {}
    if rep_p.is_file():
        shutil.copy2(rep_p, out / "logs" / rep_p.name)
    smoke = rep.get("smoke_dict", {}).get("checks_list", [])
    for c in smoke:
        checks.append({"key_str": "冒烟 " + c.get("key_str", "?"), "ok_bool": bool(c.get("ok_bool")), "detail_str": c.get("detail_str", "")[:300]})
    # ⚠ run_on_target.sh 在没有 python3 时会退化成只跑 selftest、照样 rc=0 ⇒ 必须见到冒烟报告且 8 条全在（B.1-43 ⑥）
    checks.append({"key_str": "冒烟条数", "ok_bool": len(smoke) == 8, "detail_str": "%d / 8" % len(smoke)})
    pkg = kit / "out" / "package" / ("fry_sim_sdk-%s-%s.zip" % (man["version_str"], target))
    ok = rc == 0 and bool(rep.get("smoke_bool")) and pkg.is_file()
    if ok:
        shutil.copy2(pkg, out / "deliverable" / pkg.name)
    checks.append({"key_str": "交付件", "ok_bool": ok, "detail_str": pkg.name if ok else "没有生成 %s" % pkg.name})
    return ok and all(c["ok_bool"] for c in checks)


def sim_windows(vdir: Path, man: Dict[str, Any], target: str, work: Path, out: Path, checks: List[Dict[str, Any]]) -> bool:
    import ctypes
    log = out / "logs" / "verify.log"
    src = vdir / man["inputs_dict"]["win32-x64"]["file_str"]
    extract(src, work)
    pkg = only_child(work)
    lib = pkg / "bin" / "fry_sim.dll"
    try:
        L = ctypes.CDLL(str(lib))
        for f in ("fry_sim_sdk_version", "fry_sim_sdk_fingerprint"):
            getattr(L, f).restype = ctypes.c_char_p
        ver, fp = L.fry_sim_sdk_version().decode(), L.fry_sim_sdk_fingerprint().decode()
    except Exception as exc:                                             # noqa: BLE001
        ver, fp = "?", "（加载失败：%r）" % exc
    checks.append({"key_str": "自报版本", "ok_bool": ver == man["version_str"], "detail_str": ver})
    checks.append({"key_str": "指纹闸", "ok_bool": fp == man["algorithm_fingerprint_str"], "detail_str": "库自报 %s ｜ 登记 %s" % (fp[:16], man["algorithm_fingerprint_str"][:16])})
    info = json.loads((pkg / "sdk_info.json").read_text(encoding="utf-8"))
    bad = [k for k, h in info.get("files_dict", {}).items() if not (pkg / k).is_file() or sha256_file(pkg / k) != h]
    checks.append({"key_str": "包内清单 sha256", "ok_bool": not bad and bool(info.get("files_dict")), "detail_str": "%d 个文件%s" % (len(info.get("files_dict", {})), "，不符：%s" % bad[:5] if bad else "")})
    cli = str(pkg / "bin" / "fry-sim-cli.exe")
    for key, cmd in (("cli version", [cli, "version"]),
                     ("golden 逐位 selftest", [cli, "selftest", "--golden", str(pkg / "golden")]),
                     ("纯 C 示例", [str(pkg / "bin" / "FrySimDemoC.exe")]),
                     ("Python 示例", [sys.executable, str(pkg / "examples" / "python" / "fry_sim_demo.py"), str(lib)])):
        rc = run(cmd, pkg, log)
        checks.append({"key_str": key, "ok_bool": rc == 0, "detail_str": "rc=%d" % rc})
    txt = log.read_text(encoding="utf-8", errors="replace")
    checks.append({"key_str": "selftest 失败数 = 0", "ok_bool": "失败 0" in txt, "detail_str": "日志里找「失败 0」"})
    ok = all(c["ok_bool"] for c in checks)
    if ok:
        shutil.copy2(src, out / "deliverable" / src.name)
    return ok


ROUTE_DICT = {("halftone", "unix"): halftone_unix, ("halftone", "windows"): halftone_windows,
              ("sim", "unix"): sim_unix, ("sim", "windows"): sim_windows}


def main(argv=None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description="在本机（CI 目标机）验收一个版本目录里本平台的预编二进制")
    ap.add_argument("--version-dir", required=True)
    ap.add_argument("--target", required=True, choices=TARGET_LIST)
    ap.add_argument("--out", required=True)
    ns = ap.parse_args(argv)
    vdir = (REPO / ns.version_dir).resolve() if not Path(ns.version_dir).is_absolute() else Path(ns.version_dir)
    out = Path(ns.out).resolve()
    if out.exists():
        shutil.rmtree(out)
    (out / "deliverable").mkdir(parents=True)
    (out / "logs").mkdir()
    product = json.loads((REPO / "ci" / "product.json").read_text(encoding="utf-8"))["product_str"]
    man = json.loads((vdir / "manifest.json").read_text(encoding="utf-8"))
    t0 = time.time()
    verdict = {"schema_str": "fry-gh-verdict/1", "product_str": product, "version_str": man["version_str"],
               "version_dir_str": vdir.name, "target_str": ns.target, "host_target_str": host_target(),
               "machine_dict": machine_info(), "started_at_str": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "algorithm_fingerprint_str": man["algorithm_fingerprint_str"], "checks_list": [], "ok_bool": False}
    checks = verdict["checks_list"]

    def finish(ok: bool, code: int) -> int:
        verdict["ok_bool"] = ok
        verdict["seconds_float"] = round(time.time() - t0, 1)
        verdict["deliverable_dict"] = {p.name: sha256_file(p) for p in sorted((out / "deliverable").iterdir()) if p.is_file()}
        if verdict["deliverable_dict"]:
            (out / "deliverable" / "SHA256SUMS.txt").write_text(
                "".join("%s  %s\n" % (h, n) for n, h in verdict["deliverable_dict"].items()), encoding="utf-8", newline="\n")
        (out / "verdict.json").write_text(json.dumps(verdict, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")
        print("\n%s %s ｜ %s %s ｜ %d 项检查 ｜ %.1f s" % ("✓ 验收通过" if ok else "✗ 验收没过", ns.target, product, man["version_str"],
              len(checks), verdict["seconds_float"]))
        for c in checks:
            print("  %s %-28s %s" % ("✓" if c["ok_bool"] else "✗", c["key_str"], c["detail_str"][:160]))
        return code

    # ① 平台闸：在 x64 的机器上「验收」arm64 的二进制 = 什么都没验（B.1-43 ⑩「测错了东西」）
    host_ok = verdict["host_target_str"] == ns.target
    checks.append({"key_str": "平台闸", "ok_bool": host_ok, "detail_str": "本机 %s ｜ 要验 %s" % (verdict["host_target_str"], ns.target)})
    if not host_ok:
        return finish(False, 2)
    if ns.target not in man["targets_list"]:
        checks.append({"key_str": "版本目录含本平台", "ok_bool": False, "detail_str": "manifest 只登记了 %s" % man["targets_list"]})
        return finish(False, 2)
    # ② 输入闸
    probs = check_inputs(vdir, man)
    checks.append({"key_str": "输入闸 sha256", "ok_bool": not probs, "detail_str": "；".join(probs) or "%d 个输入与 manifest 一致" % len(man["inputs_dict"])})
    if probs:
        return finish(False, 2)
    # ③ 真跑
    work = out.parent / ("_work_" + ns.target)
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    route = ROUTE_DICT[(product, "windows" if ns.target.startswith("win32") else "unix")]
    try:
        ok = route(vdir, man, ns.target, work, out, checks)
    except SystemExit as exc:                                            # ⚠ SystemExit 不是 Exception（CLAUDE.md A.4）
        checks.append({"key_str": "异常", "ok_bool": False, "detail_str": str(exc)})
        ok = False
    except Exception as exc:                                             # noqa: BLE001
        checks.append({"key_str": "异常", "ok_bool": False, "detail_str": repr(exc)})
        ok = False
    return finish(ok, 0 if ok else 1)


if __name__ == "__main__":
    sys.exit(main())
