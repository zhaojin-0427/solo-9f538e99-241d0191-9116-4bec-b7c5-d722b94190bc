#!/usr/bin/env python3
"""Self-tests for the leader "补页" (page supplement) workflow.

Starts real uvicorn processes with separate SQLite files and verifies:

* open returns body + version;
* open/scored papers can be supplemented by (paper_no, seen_version, full text);
* supplement withdraws the score and returns the paper to ``open`` at the
  same grading point;
* old surface and the withdrawn score are queryable;
* supplemented papers only accept scores carrying the current version, while
* never-supplemented papers still accept version-less scores;
* a scored paper can be supplemented even when the quota is full, and the
  resulting open/pending-regrade paper counts towards intake quota;
* one seen version can be supplemented only once and failed supplements do
  not change body, score or quota count;
* databases created by the baseline release migrate in place without losing
  existing rows/scores.

Run with project dependencies available, e.g.::

    pip install -r requirements.txt
    python scripts/supplement_smoke.py
"""

from __future__ import annotations

import os
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]


def wait_health(base_url: str, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    last: Exception | None = None
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url}/health", timeout=1.0, trust_env=False)
            if response.status_code == 200:
                return
        except Exception as exc:  # startup race
            last = exc
        time.sleep(0.25)
    raise RuntimeError(f"service at {base_url} did not become healthy: {last!r}")


def start_service(name: str, port: int, db_path: Path, extra_env: dict[str, str]) -> subprocess.Popen:
    log_path = db_path.with_suffix(".log")
    log = log_path.open("wb")
    env = os.environ.copy()
    env.update(
        {
            "SITE_ID": name,
            "DATABASE_PATH": str(db_path),
            "RECOVERY_ENABLED": "false",
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.update(extra_env)
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_health(f"http://127.0.0.1:{port}")
    except Exception:
        process.terminate()
        try:
            out = Path(log_path).read_text(errors="replace")[-4000:]
        except Exception:
            out = ""
        raise RuntimeError(f"{name} failed to start; log tail:\n{out}")
    return process


def stop_service(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def restart_service(
    old: subprocess.Popen,
    name: str,
    port: int,
    db_path: Path,
    extra_env: dict[str, str],
) -> subprocess.Popen:
    stop_service(old)
    return start_service(name, port, db_path, extra_env)


def assert_status(response: httpx.Response, expected: int, context: str) -> dict[str, Any]:
    if response.status_code != expected:
        raise AssertionError(
            f"{context}: expected HTTP {expected}, got {response.status_code}: {response.text}"
        )
    if response.content:
        return response.json()
    return {}


def make_old_schema_db(path: Path) -> None:
    """Create a SQLite file exactly like the pre-supplement baseline."""

    conn = sqlite3.connect(path)
    try:
        conn.execute(
            """
            CREATE TABLE papers (
                paper_no TEXT PRIMARY KEY,
                status TEXT NOT NULL CHECK (
                    status IN ('unopened', 'open', 'scored',
                               'transfer_pending', 'transferred_out')
                ),
                body TEXT NOT NULL,
                score REAL,
                score_comment TEXT,
                grader TEXT,
                source_site TEXT,
                transfer_order_no TEXT,
                opened_at TEXT,
                scored_at TEXT,
                transferred_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            INSERT INTO papers (
                paper_no, status, body, score, score_comment, grader,
                source_site, transfer_order_no, opened_at, scored_at,
                transferred_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "OLD-001",
                "scored",
                "旧卷面原文-OLD-001-迁移前",
                83.0,
                "迁移前评语",
                "old-teacher",
                "legacy-site",
                None,
                "2026-11-20T08:00:00+00:00",
                "2026-11-20T09:00:00+00:00",
                None,
                "2026-11-20T08:00:00+00:00",
                "2026-11-20T09:00:00+00:00",
            ),
        )
        conn.commit()
    finally:
        conn.close()


def run_version_flow(base: str) -> list[str]:
    checks: list[str] = []

    # Open returns body and version 1.
    r = httpx.post(f"{base}/papers/V-001/open", timeout=5, trust_env=False)
    data = assert_status(r, 200, "open V-001")
    assert data["version"] == 1 and data["current_version"] == 1, data
    assert "匿名答卷 V-001" in data["body"], data
    checks.append("开卷返回正文和版本 version=1")

    # Never-supplemented paper accepts a version-less score.
    r = httpx.post(f"{base}/papers/V-001/score", json={"score": 88, "grader": "g1"}, timeout=5, trust_env=False)
    data = assert_status(r, 200, "score V-001 without version before supplement")
    assert data["status"] == "scored" and data["score"] == 88, data
    checks.append("未补过的卷接受不带版本的交分")

    # Leader supplements the scored paper using the seen version.
    full_text_v2 = "补齐后的完整正文 V-001（含新发现的漏扫页全文）"
    r = httpx.post(
        f"{base}/papers/V-001/supplement",
        json={
            "seen_version": 1,
            "body": full_text_v2,
            "leader": "leader-zhang",
            "reason": "第 3 页漏扫",
        },
        timeout=5,
        trust_env=False,
    )
    data = assert_status(r, 200, "supplement scored V-001")
    assert data["status"] == "open", data
    assert data["score"] is None, data
    assert data["version"] == 2 and data["current_version"] == 2, data
    assert data["body"] == full_text_v2, data
    assert data["withdrawn_score"] == 88, data
    checks.append("已交分卷补页后撤下分数、回到 open、版本升到 2")

    # Old-version score is rejected after supplement (the key regression).
    r = httpx.post(f"{base}/papers/V-001/score", json={"score": 90, "version": 1}, timeout=5, trust_env=False)
    assert_status(r, 409, "old version score after supplement")
    err_code = r.json()["error"]["code"]
    assert "version" in err_code, err_code
    checks.append("补页后的旧版交分（version=1）被 409 拒绝")

    # Missing version is also rejected for supplemented papers.
    r = httpx.post(f"{base}/papers/V-001/score", json={"score": 90}, timeout=5, trust_env=False)
    assert_status(r, 409, "version-less score after supplement")
    assert r.json()["error"]["code"] == "score_version_required", r.json()
    checks.append("补过页的卷不带版本交分被 409 拒绝")

    # Failed scores did not change the paper.
    r = httpx.get(f"{base}/papers/V-001", params={"include_body": "true"}, timeout=5, trust_env=False)
    data = assert_status(r, 200, "get V-001 after failed scores")
    assert data["status"] == "open" and data["score"] is None and data["version"] == 2, data
    assert data["body"] == full_text_v2, data
    checks.append("旧版交分失败后卷面/分数/版本不变")

    # Current-version score is accepted.
    r = httpx.post(
        f"{base}/papers/V-001/score",
        json={"score": 91, "version": 2, "grader": "g2"},
        timeout=5,
        trust_env=False,
    )
    data = assert_status(r, 200, "current version score")
    assert data["status"] == "scored" and data["score"] == 91, data
    checks.append("携带当前版本 version=2 的交分成功")

    # Old surface + withdrawn score are queryable through every alias.
    for path in ("/revisions", "/versions", "/history", "/supplements"):
        r = httpx.get(f"{base}/papers/V-001{path}", timeout=5, trust_env=False)
        hist = assert_status(r, 200, f"history alias {path}")
        assert hist["count"] == 1, hist
        rec = hist[hist_key(hist)][0]
        assert rec["version"] == 1 and rec["score"] == 88, rec
        assert "匿名答卷 V-001" in rec["body"], rec
        assert rec["new_body"] == full_text_v2, rec
    checks.append("旧卷面和当时分数 88 可经 revisions/versions/history/supplements 查询")

    # Specific old/new version endpoints.
    r = httpx.get(f"{base}/papers/V-001/versions/1", timeout=5, trust_env=False)
    rec = assert_status(r, 200, "get version 1")
    assert rec["score"] == 88 and "匿名答卷 V-001" in rec["body"], rec
    r = httpx.get(f"{base}/papers/V-001/versions/current", timeout=5, trust_env=False)
    cur = assert_status(r, 200, "get current version")
    assert cur["version"] == 2 and cur["score"] == 91, cur
    checks.append("可按版本号查询旧卷面，current 指向当前版")

    # Paper detail embeds history too.
    r = httpx.get(f"{base}/papers/V-001", timeout=5, trust_env=False)
    detail = assert_status(r, 200, "paper detail history")
    assert len(detail["revisions"]) == 1 and detail["revisions"][0]["score"] == 88, detail
    checks.append("查卷详情也内嵌旧卷面记录")

    # Same seen version can only be supplemented once.
    r = httpx.post(
        f"{base}/papers/V-001/supplement",
        json={"seen_version": 1, "body": "重复补页"},
        timeout=5,
        trust_env=False,
    )
    data = assert_status(r, 409, "duplicate supplement same version")
    assert data["error"]["code"] == "version_already_supplemented", data
    checks.append("同一版本重复补页被 409 version_already_supplemented 拒绝")

    # A mismatched seen version is rejected and changes nothing.
    r = httpx.post(
        f"{base}/papers/V-001/supplement",
        json={"seen_version": 99, "body": "错误版本补页"},
        timeout=5,
        trust_env=False,
    )
    assert_status(r, 409, "wrong version supplement")
    r = httpx.get(f"{base}/papers/V-001", params={"include_body": "true"}, timeout=5, trust_env=False)
    data = assert_status(r, 200, "V-001 unchanged after failed supplements")
    assert data["status"] == "scored" and data["score"] == 91 and data["version"] == 2, data
    assert data["body"] == full_text_v2, data
    checks.append("补页版本不符时不改卷面、分数和版本")

    # Second-round supplement on version 2 withdraws score 91.
    full_text_v3 = "第二次补页后的 V-001 完整正文"
    r = httpx.post(
        f"{base}/supplement",
        json={"paper_no": "V-001", "version": 2, "full_text": full_text_v3},
        timeout=5,
        trust_env=False,
    )
    data = assert_status(r, 200, "second supplement V-001")
    assert data["version"] == 3 and data["withdrawn_score"] == 91 and data["status"] == "open", data
    r = httpx.post(f"{base}/papers/V-001/score", json={"score": 92, "version": 2}, timeout=5, trust_env=False)
    assert_status(r, 409, "stale v2 score after second supplement")
    r = httpx.post(f"{base}/papers/V-001/score", json={"score": 95, "version": 3}, timeout=5, trust_env=False)
    assert_status(r, 200, "v3 score accepted")
    r = httpx.get(f"{base}/papers/V-001/revisions", timeout=5, trust_env=False)
    hist = assert_status(r, 200, "two revisions")
    assert hist["count"] == 2, hist
    assert [x["score"] for x in hist["revisions"]] == [88, 91], hist
    checks.append("可再次按当前版本补页；历史依次保存 88、91，旧版交分继续被拒")

    # An open (never scored) paper can also be supplemented.
    r = httpx.post(f"{base}/papers/V-002/open", timeout=5, trust_env=False)
    assert_status(r, 200, "open V-002")
    r = httpx.post(f"{base}/papers/V-002/supplement", json={"version": 1, "body": "V-002 补全正文"}, timeout=5, trust_env=False)
    data = assert_status(r, 200, "supplement open V-002")
    assert data["status"] == "open" and data["score"] is None and data["version"] == 2, data
    assert data["withdrawn_score"] is None, data
    r = httpx.post(f"{base}/papers/V-002/score", json={"score": 70}, timeout=5, trust_env=False)
    assert_status(r, 409, "V-002 version required")
    r = httpx.post(f"{base}/papers/V-002/score", json={"score": 72, "seen_version": 2}, timeout=5, trust_env=False)
    assert_status(r, 200, "V-002 score with seen_version alias")
    checks.append("已开卷未交分的卷也可补页，随后必须带新版本交分")

    # Unopened paper cannot be supplemented.
    r = httpx.post(f"{base}/papers/V-003/supplement", json={"version": 1, "body": "x"}, timeout=5, trust_env=False)
    assert_status(r, 409, "supplement unopened V-003")
    checks.append("未开卷的卷补页被 409 拒绝")

    # Chinese field aliases work end to end.
    httpx.post(f"{base}/papers/V-003/open", timeout=5, trust_env=False)
    r = httpx.post(
        f"{base}/supplement",
        json={"卷号": "V-003", "所见版本": 1, "全文": "V-003 中文别名补页正文"},
        timeout=5,
        trust_env=False,
    )
    data = assert_status(r, 200, "chinese aliases supplement")
    assert data["version"] == 2 and "中文别名" in data["body"], data
    checks.append("中文别名字段（卷号/所见版本/全文）可用于补页")

    return checks


def hist_key(hist: dict[str, Any]) -> str:
    # The envelope exposes several equal aliases; pick the canonical one.
    return "revisions" if "revisions" in hist else "items"


def run_quota_flow(base: str) -> list[str]:
    checks: list[str] = []

    cap = assert_status(httpx.get(f"{base}/capacity", timeout=5, trust_env=False), 200, "capacity start")
    assert cap["max_unscored"] == 3 and cap["unscored_count"] == 5, cap

    # Score three papers to leave two unscored (one slot free).
    for no, score in (("Q-003", 77), ("Q-004", 66), ("Q-005", 55)):
        r = httpx.post(f"{base}/papers/{no}/score", json={"score": score}, timeout=5, trust_env=False)
        assert_status(r, 200, f"score {no}")
    cap = assert_status(httpx.get(f"{base}/capacity", timeout=5, trust_env=False), 200, "capacity after scoring")
    assert cap["unscored_count"] == 2 and cap["remaining_quota"] == 1, cap

    # One inbound batch fills the quota exactly.
    r = httpx.post(
        f"{base}/internal/transfers",
        json={
            "order_no": "IN-OK-1",
            "source_site": "quota-origin",
            "paper_nos": ["NEW-1"],
            "papers": [{"paper_no": "NEW-1", "body": "新转入卷 NEW-1"}],
        },
        timeout=5,
        trust_env=False,
    )
    inbound = assert_status(r, 200, "inbound accept")
    assert inbound["decision"] == "accepted" and inbound["unscored_count_after"] == 3, inbound
    cap = assert_status(httpx.get(f"{base}/capacity", timeout=5, trust_env=False), 200, "capacity full")
    assert cap["unscored_count"] == 3 and cap["remaining_quota"] == 0, cap
    checks.append("准备：接收一卷后名额恰好已满（unscored=3/3）")

    # Scored Q-003 can be supplemented even though quota is full.
    q3_body = "Q-003 补页后的完整正文（满额补页）"
    r = httpx.post(
        f"{base}/papers/Q-003/supplement",
        json={"seen_version": 1, "body": q3_body},
        timeout=5,
        trust_env=False,
    )
    data = assert_status(r, 200, "supplement at full quota")
    assert data["status"] == "open" and data["score"] is None and data["version"] == 2, data
    assert data["withdrawn_score"] == 77, data
    checks.append("名额已满时，已交分 Q-003 仍可补页并撤下 77 分")

    # The supplemented pending-regrade paper is counted in the quota.
    cap = assert_status(httpx.get(f"{base}/capacity", timeout=5, trust_env=False), 200, "capacity after supplement")
    assert cap["unscored_count"] == 4, cap
    assert cap["pending_regrade_count"] == 1, cap
    assert cap["remaining_quota"] == -1, cap

    r = httpx.post(
        f"{base}/internal/transfers",
        json={
            "order_no": "IN-NO-1",
            "source_site": "quota-origin",
            "paper_nos": ["NEW-2"],
            "papers": [{"paper_no": "NEW-2", "body": "不应入账的卷"}],
        },
        timeout=5,
        trust_env=False,
    )
    rejected = assert_status(r, 200, "inbound rejected after supplement")
    assert rejected["decision"] == "rejected", rejected
    assert rejected["reason"] == "quota_exceeded", rejected
    assert rejected["unscored_count_before"] == 4, rejected
    checks.append("接收新调卷时补页待评卷计入限额：按 4 计数并以 quota_exceeded 拒收")

    # A wrong-version failed supplement on scored Q-004 preserves score/quota.
    r = httpx.post(f"{base}/papers/Q-004/supplement", json={"seen_version": 7, "body": "bad"}, timeout=5, trust_env=False)
    assert_status(r, 409, "failed supplement Q-004")
    q4 = assert_status(httpx.get(f"{base}/papers/Q-004", timeout=5, trust_env=False), 200, "Q-004 after failure")
    assert q4["status"] == "scored" and q4["score"] == 66 and q4["version"] == 1, q4
    cap2 = assert_status(httpx.get(f"{base}/capacity", timeout=5, trust_env=False), 200, "quota unchanged")
    assert cap2["unscored_count"] == 4, cap2
    checks.append("补页失败不改 Q-004 的卷面/66 分，也不改变名额计数 4")

    # Duplicate supplement on Q-003 likewise changes nothing.
    r = httpx.post(
        f"{base}/papers/Q-003/supplement",
        json={"seen_version": 1, "body": "dup"},
        timeout=5,
        trust_env=False,
    )
    assert_status(r, 409, "duplicate Q-003 supplement")
    q3 = assert_status(
        httpx.get(f"{base}/papers/Q-003", params={"include_body": "true"}, timeout=5, trust_env=False),
        200,
        "Q-003 after duplicate",
    )
    assert q3["status"] == "open" and q3["score"] is None and q3["version"] == 2, q3
    assert q3["body"] == q3_body, q3
    cap3 = assert_status(httpx.get(f"{base}/capacity", timeout=5, trust_env=False), 200, "quota still 4")
    assert cap3["unscored_count"] == 4, cap3
    checks.append("重复补页失败后 Q-003 卷面与名额均不变")

    # Unopened supplement failure also leaves quota untouched.
    r = httpx.post(f"{base}/papers/Q-001/supplement", json={"version": 1, "body": "x"}, timeout=5, trust_env=False)
    assert_status(r, 409, "unopened Q-001 supplement")
    cap4 = assert_status(httpx.get(f"{base}/capacity", timeout=5, trust_env=False), 200, "quota unchanged 2")
    assert cap4["unscored_count"] == 4, cap4
    checks.append("未开卷补页失败同样不动名额")

    # Re-grade with the current version frees the counted slot again.
    r = httpx.post(f"{base}/papers/Q-003/score", json={"score": 99, "version": 2}, timeout=5, trust_env=False)
    assert_status(r, 200, "regrade Q-003")
    cap5 = assert_status(httpx.get(f"{base}/capacity", timeout=5, trust_env=False), 200, "quota after regrade")
    assert cap5["unscored_count"] == 3 and cap5["pending_regrade_count"] == 0, cap5
    checks.append("按新版本重新交分后，补页待评计数消失，名额恢复")

    return checks


def run_migration_flow(base: str) -> list[str]:
    checks: list[str] = []

    r = httpx.get(f"{base}/papers/OLD-001", params={"include_body": "true"}, timeout=5, trust_env=False)
    data = assert_status(r, 200, "migrated old paper readable")
    assert data["body"] == "旧卷面原文-OLD-001-迁移前", data
    assert data["score"] == 83 and data["grader"] == "old-teacher", data
    assert data["version"] == 1 and data["supplement_count"] == 0, data
    checks.append("旧 SQLite 数据原样保留，迁移后 version=1、旧分 83 仍在")

    r = httpx.post(
        f"{base}/papers/OLD-001/supplement",
        json={"seen_version": 1, "body": "迁移后补页全文"},
        timeout=5,
        trust_env=False,
    )
    data = assert_status(r, 200, "supplement migrated paper")
    assert data["withdrawn_score"] == 83 and data["version"] == 2, data
    r = httpx.post(f"{base}/papers/OLD-001/score", json={"score": 90, "version": 1}, timeout=5, trust_env=False)
    assert_status(r, 409, "migrated paper old version rejected")
    r = httpx.post(f"{base}/papers/OLD-001/score", json={"score": 90, "version": 2}, timeout=5, trust_env=False)
    assert_status(r, 200, "migrated paper new version accepted")
    r = httpx.get(f"{base}/papers/OLD-001/revisions", timeout=5, trust_env=False)
    hist = assert_status(r, 200, "migrated revision history")
    assert hist["revisions"][0]["score"] == 83, hist
    checks.append("迁移后的旧库可直接补页，旧分 83 入历史，版本交分规则生效")

    return checks


def main() -> int:
    temp_root = Path(tempfile.mkdtemp(prefix="supplement-check-"))
    print(f"临时 SQLite/日志目录: {temp_root}")
    processes: list[subprocess.Popen] = []
    proc_v: subprocess.Popen | None = None
    try:
        db_v = temp_root / "v.db"
        db_q = temp_root / "q.db"
        db_m = temp_root / "old.db"
        make_old_schema_db(db_m)

        env_v = {"SEED_PREFIX": "V", "SEED_COUNT": "3", "MAX_UNSCORED": "30"}
        env_q = {"SEED_PREFIX": "Q", "SEED_COUNT": "5", "MAX_UNSCORED": "3"}
        env_m = {"SEED_COUNT": "0", "MAX_UNSCORED": "10"}

        proc_v = start_service("supp-v", 18161, db_v, env_v)
        proc_q = start_service("supp-q", 18162, db_q, env_q)
        proc_m = start_service("supp-migrate", 18163, db_m, env_m)
        processes.extend([proc_v, proc_q, proc_m])

        checks_v = run_version_flow("http://127.0.0.1:18161")
        print("\n[1/3] 版本/补页/历史流程实测通过：")
        for item in checks_v:
            print(f"  ✓ {item}")

        checks_q = run_quota_flow("http://127.0.0.1:18162")
        print("\n[2/3] 满额补页与限额计入实测通过：")
        for item in checks_q:
            print(f"  ✓ {item}")

        checks_m = run_migration_flow("http://127.0.0.1:18163")
        print("\n[3/3] 旧 SQLite 库原地迁移实测通过：")
        for item in checks_m:
            print(f"  ✓ {item}")

        # Restart the version service and prove state/rules survive.
        proc_v = restart_service(proc_v, "supp-v", 18161, db_v, env_v)
        r = httpx.get("http://127.0.0.1:18161/papers/V-001/revisions", timeout=5, trust_env=False)
        hist = assert_status(r, 200, "history after restart")
        assert hist["count"] == 2, hist
        r = httpx.post(
            "http://127.0.0.1:18161/papers/V-001/score",
            json={"score": 1, "version": 2},
            timeout=5,
            trust_env=False,
        )
        assert_status(r, 409, "stale version still rejected after restart")
        print("\n[4/3] 重启持久化通过：两条补页历史仍在，旧版交分仍被拒")

        print("\nSUPPLEMENT_SMOKE_OK")
        print(f"数据库保留在: {temp_root}")
        return 0
    finally:
        for process in processes:
            stop_service(process)
        stop_service(proc_v if proc_v not in processes else None)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nSUPPLEMENT_SMOKE_FAILED: {exc}", file=sys.stderr)
        raise
