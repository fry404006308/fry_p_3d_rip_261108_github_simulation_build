# -*- coding: utf-8 -*-
"""★461 公开验收仓自己的闸：每条都验两个方向（该过的过、该红的红 —— CLAUDE.md D.1-9「只验一个方向 = 只证明了一半」）。
只用标准库 + pytest；不需要 gh / 网络 / 二进制。  python -m pytest -q tests"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tools"))
sys.path.insert(0, str(REPO / "ci"))
import fry_ci_summary as S       # noqa: E402
import fry_ci_target as T        # noqa: E402
import fry_gh_publish as G       # noqa: E402

SRC = b"// algo\r\nint f() { return 1; }\r\n"
LOCK = {"tree_sha256_str": "x" * 64, "file_sha256_dict": {"src/f.cpp": G.sha256_bytes(SRC.replace(b"\r\n", b"\n"))}}


# ───────── 源码泄漏闸 ─────────
def test_leak_gate_passes_clean_kit():
    assert G.leak_problems([("kit/prebuilt/lib.so", b"\x7fELF..."), ("kit/README.md", b"hello")], LOCK) == []


def test_leak_gate_catches_payload_dir():
    assert G.leak_problems([("kit/cpp/src/anything.cpp", b"whatever")], LOCK)


def test_leak_gate_catches_renamed_source_with_other_eol():
    """改名 + 换到别的目录 + 换行从 CRLF 变 LF，内容相同照样抓（第二把尺子与路径无关）。"""
    probs = G.leak_problems([("kit/tools/renamed.cpp", SRC.replace(b"\r\n", b"\n"))], LOCK)
    assert probs and "内容 = 算法源码" in probs[0]


def test_leak_gate_ignores_empty_file():
    lock = {"file_sha256_dict": {"src/empty.h": G.EMPTY_SHA}}
    assert G.leak_problems([("kit/x/empty.txt", b"")], lock) == []


def test_lock_hashes_reads_both_lock_formats():
    assert G.lock_hashes({"file_sha256_dict": {"a": "1"}}) == {"a": "1"}
    assert G.lock_hashes({"files_dict": {"a": {"sha256_str": "2"}}}) == {"a": "2"}
    assert G.lock_hashes({"files_dict": {"a": "3"}}) == {"a": "3"}


# ───────── 汇总闸：登记的平台一个都不能少 ─────────
MAN = {"version_str": "1.0.0", "dir_str": "_x", "algorithm_fingerprint_str": "f" * 64, "targets_list": ["linux-x64", "darwin-arm64"]}


def _v(t, ok=True):
    return {"target_str": t, "ok_bool": ok, "checks_list": [{"key_str": "k", "ok_bool": ok, "detail_str": ""}], "machine_dict": {}}


def test_summary_all_ok():
    assert S.summarize(MAN, [_v("linux-x64"), _v("darwin-arm64")])["ok_bool"]


def test_summary_missing_platform_is_red():
    s = S.summarize(MAN, [_v("linux-x64")])
    assert not s["ok_bool"] and s["passed_int"] == 1 and s["expected_int"] == 2


def test_summary_failed_platform_is_red():
    assert not S.summarize(MAN, [_v("linux-x64"), _v("darwin-arm64", ok=False)])["ok_bool"]


def test_summary_empty_manifest_is_red():
    assert not S.summarize(dict(MAN, targets_list=[]), [])["ok_bool"]


# ───────── 本机复核：交付件下载后被换 ⇒ 红 ─────────
def _fake_verified(tmp_path, tamper=False):
    vdir = tmp_path / "_v"
    (vdir / "verified" / "verified-summary").mkdir(parents=True)
    (vdir / "manifest.json").write_text(json.dumps(dict(MAN, targets_list=["linux-x64"])), encoding="utf-8")
    (vdir / "verified" / "verified-summary" / "summary.json").write_text(json.dumps({"ok_bool": True}), encoding="utf-8")
    d = vdir / "verified" / "verified-linux-x64"
    (d / "deliverable").mkdir(parents=True)
    (d / "deliverable" / "pkg.zip").write_bytes(b"good")
    v = dict(_v("linux-x64"), algorithm_fingerprint_str=MAN["algorithm_fingerprint_str"], deliverable_dict={"pkg.zip": G.sha256_bytes(b"good")})
    (d / "verdict.json").write_text(json.dumps(v), encoding="utf-8")
    if tamper:
        (d / "deliverable" / "pkg.zip").write_bytes(b"evil")
    return vdir


def test_check_verified_good(tmp_path):
    assert G.check_verified(_fake_verified(tmp_path))["ok_bool"]


def test_check_verified_tampered(tmp_path):
    r = G.check_verified(_fake_verified(tmp_path, tamper=True))
    assert not r["ok_bool"] and any("sha256" in p for p in r["problems_list"])


# ───────── 配置一致性 ─────────
def test_installed_workflow_equals_source():
    """.github/workflows/ 里的那份必须 = ci/workflows/ 正本（不存在 = 还没 push 过，跳过）。"""
    if not G.WORKFLOW_DST.is_file():
        pytest.skip("还没装到 .github/workflows/（第一次 push 时 install_workflow 会装）")
    assert G.WORKFLOW_DST.read_bytes() == G.WORKFLOW_SRC.read_bytes()


def test_workflow_has_a_runner_for_every_target():
    y = G.WORKFLOW_SRC.read_text(encoding="utf-8")
    for t in T.TARGET_LIST:
        assert '"%s":' % t in y, t


def test_product_json_is_known():
    p = json.loads((REPO / "ci" / "product.json").read_text(encoding="utf-8"))
    assert (p["product_str"], "unix") in T.ROUTE_DICT and (p["product_str"], "windows") in T.ROUTE_DICT


@pytest.mark.parametrize("tag,kit", [("darwin-arm64", "macos-arm64"), ("darwin-x64", "macos-x64"), ("linux-x64", "linux-x64")])
def test_halftone_kit_dir_names(tag, kit):
    assert T.HALFTONE_KIT_NAME_DICT.get(tag, tag) == kit


# ───────── 加网 golden 件数闸 ─────────
def _kit(tmp_path, n):
    (tmp_path / "tests" / "data").mkdir(parents=True)
    (tmp_path / "tests" / "data" / "golden.json").write_text(json.dumps({"case_list": [{}] * n}), encoding="utf-8")
    return tmp_path


@pytest.mark.parametrize("log_text,ok", [
    ("✓ golden 23 / 23 件逐字节一致\n  ── golden 经 CLI：23 / 23 件逐字节一致\n", True),
    ("✗ golden 22 / 23 件逐字节一致\n", False),          # 有一件不同
    ("✓ golden 3 / 3 件逐字节一致\n", False),            # --quick 只跑了 3 件 ≠ 登记 23 件
    ("全部完成\n", False),                               # 没见到计数行 = 没查
])
def test_golden_count_check(tmp_path, log_text, ok):
    log = tmp_path / "v.log"
    log.write_text(log_text, encoding="utf-8")
    assert T.golden_count_check(log, _kit(tmp_path / "k", 23))["ok_bool"] is ok
