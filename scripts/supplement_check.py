#!/usr/bin/env python3
"""Real HTTP self-test for page supplements ("补页").

Starts two real uvicorn processes on two SQLite files and verifies:

* an old-schema SQLite file is migrated in place and its data preserved;
* open returns body + version; legacy version-less scoring still works;
* after a supplement the old-version / missing-version score is rejected,
  while a score carrying the current version is accepted;
* the same (paper, seen version) can only be supplemented once and every
  failed supplement leaves body, score and quota untouched;
* a scored paper can be supplemented while the receiver quota is full, and
  the resulting pending-regrade paper is counted by new transfer intake;
* old body + withdrawn score are queryable through history/version endpoints;
* history/version gates survive a receiver restart (Docker-style persistence).

Run with the project requirements installed:

    python scripts/supplement_check.py
"""

from __future__ import annotations

import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


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
            "HTTP_TIMEOUT": "5",
            "PYTHONUNBUFFERED": "1",
        }
    )
    env.update(extra_env)
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1",
         "--port", str(port), "--log-level", "warning"],
        cwd=ROOT,
        env=env,
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    try:
        wait_health(f"http://127.0.0.1:{port}")
    except Exception:
        process.terminate()
        out = Path(log_path).read_text(errors="replace")[-4000:] if log_path.exists() else ""
        raise RuntimeError(f"{name} failed to start; log tail:\n{out}")
    return process


def stop_service(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    process.send_signal(signal.SIGTERM)
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def restart_service(
    old: subprocess.Popen, name: str, port: int, db_path: Path, extra_env: dict[str, str]
) -> subprocess.Popen:
    stop_service(old)
    return start_service(name, port, db_path, extra_env)


def expect(response: httpx.Response, status: int, context: str) -> dict[str, Any]:
    if response.status_code != status:
        raise AssertionError(f"{context}: expected {status}, got {response.status_code}: {response.text}")
    return response.json() if response.content else {}


def build_legacy_database(db_path: Path) -> None:
    """Create a database exactly like the initial release (no version columns)."""

    conn = sqlite3.connect(db_path)
    conn.executescript(
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
        );
        """
    )
    rows = [
        ("B-001", "scored", "LEGACY B-001 原始卷面（含最初扫描内容）",
         88.0, "legacy comment", "teacher-old", "point-b", None,
         "t0", "t0", None, "t0", "t0"),
        ("B-002", "unopened", "LEGACY B-002", None, None, None, "point-b",
         None, None, None, None, "t0", "t0"),
        ("B-003", "unopened", "LEGACY B-003", None, None, None, "point-b",
         None, None, None, None, "t0", "t0"),
        ("B-004", "unopened", "LEGACY B-004", None, None, None, "point-b",
         None, None, None, None, "t0", "t0"),
    ]
    conn.executemany("INSERT INTO papers VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    conn.close()

    # Run the new startup migration against it, exactly as container start does.
    os.environ["DATABASE_PATH"] = str(db_path)
    from app.config import Settings
    from app.db import init_db

    init_db(Settings.from_env())

    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(papers)").fetchall()}
    assert {"version", "supplement_count"} <= cols, cols
    assert conn.execute("SELECT version FROM papers WHERE paper_no='B-001'").fetchone()[0] == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "paper_supplements" in tables
    score = conn.execute("SELECT score FROM papers WHERE paper_no='B-001'").fetchone()[0]
    assert score == 88.0
    conn.close()


def main() -> int:
    temp_root = Path(tempfile.mkdtemp(prefix="supplement-live-"))
    print(f"临时 SQLite/日志目录: {temp_root}")
    processes: list[subprocess.Popen] = []
    checks: list[str] = []
    try:
        db_a = temp_root / "a.db"
        db_b = temp_root / "b-legacy.db"
        build_legacy_database(db_b)
        checks.append("旧版 SQLite 原地迁移到 version/补页表，原有卷面和 88 分保留")

        port_a = free_port()
        port_b = free_port()
        env_a = {"SEED_PREFIX": "A", "SEED_COUNT": "2", "MAX_UNSCORED": "30"}
        env_b = {"SEED_PREFIX": "B", "SEED_COUNT": "4", "MAX_UNSCORED": "3"}
        proc_a = start_service("point-a-sup", port_a, db_a, env_a)
        proc_b = start_service("point-b-sup", port_b, db_b, env_b)
        processes.extend([proc_a, proc_b])
        a = f"http://127.0.0.1:{port_a}"
        b = f"http://127.0.0.1:{port_b}"

        # /docs schema exposes the new endpoint.
        schema = httpx.get(f"{b}/openapi.json", timeout=5, trust_env=False).json()
        assert "/papers/{paper_no}/supplement" in schema["paths"]
        assert "/papers/{paper_no}/history" in schema["paths"]
        checks.append("/docs/openapi 暴露补页和历史接口")

        # Legacy row: version 1, preserved score, open returns body+version.
        paper = expect(httpx.get(f"{b}/papers/B-001", timeout=5, trust_env=False), 200, "get legacy B-001")
        assert paper["version"] == 1 and paper["score"] == 88, paper
        opened = expect(httpx.post(f"{b}/papers/B-001/open", timeout=5, trust_env=False), 200, "open legacy")
        assert opened["version"] == 1 and "LEGACY B-001" in opened["body"], opened
        checks.append("开卷返回正文和 version=1，迁移卷的旧分数仍在")

        cap = expect(httpx.get(f"{b}/capacity", timeout=5, trust_env=False), 200, "capacity")
        assert cap["unscored_count"] == 3 and cap["remaining_quota"] == 0, cap

        # Failed supplement (wrong seen version) changes nothing.
        r = httpx.post(f"{b}/papers/B-001/supplement",
                       json={"seen_version": 99, "body": "SHOULD NOT LAND"},
                       timeout=5, trust_env=False)
        expect(r, 409, "stale supplement")
        paper = httpx.get(f"{b}/papers/B-001?include_body=true", timeout=5, trust_env=False).json()
        assert paper["status"] == "scored" and paper["score"] == 88 and paper["version"] == 1
        assert "SHOULD NOT LAND" not in paper["body"]
        assert httpx.get(f"{b}/capacity", timeout=5, trust_env=False).json()["unscored_count"] == 3

        # Failed supplement (missing full text) changes nothing either.
        r = httpx.post(f"{b}/papers/B-001/supplement", json={"seen_version": 1},
                       timeout=5, trust_env=False)
        expect(r, 422, "empty body supplement")
        paper = httpx.get(f"{b}/papers/B-001", timeout=5, trust_env=False).json()
        assert paper["score"] == 88 and paper["version"] == 1
        assert httpx.get(f"{b}/papers/B-001/history", timeout=5, trust_env=False).json()["count"] == 0
        checks.append("补页失败（版本不符/正文缺失）不改卷面、分数和名额")

        # Headline 1: scored paper can be supplemented while quota is full.
        v2_body = "补齐漏扫页后的 V2 完整全文\n第二页/第三页……"
        r = httpx.post(f"{b}/papers/B-001/supplement",
                       json={"paper_no": "B-001", "seen_version": 1,
                             "full_text": v2_body, "operator": "leader-li"},
                       timeout=5, trust_env=False)
        sup = expect(r, 200, "supplement scored paper at full quota")
        assert sup["version"] == 2 and sup["status"] == "open", sup
        assert sup["score"] is None and sup["withdrawn_score"] == 88, sup
        assert sup["body"] == v2_body
        cap = httpx.get(f"{b}/capacity", timeout=5, trust_env=False).json()
        assert cap["unscored_count"] == 4 and cap["remaining_quota"] == -1, cap
        checks.append("满额（3/3）时已交分 B-001 仍可补页：旧分 88 撤下、变 open，名额变为 4/3")

        # Headline 2: old-version score rejected; missing version rejected too.
        r = httpx.post(f"{b}/papers/B-001/score", json={"score": 90, "version": 1},
                       timeout=5, trust_env=False)
        err = expect(r, 409, "old version score")
        assert err["code"] == "version_conflict", err
        assert err.get("current_version") == 2 and err.get("stale") is True, err

        r = httpx.post(f"{b}/papers/B-001/score", json={"score": 90},
                       timeout=5, trust_env=False)
        expect(r, 409, "version-less score on supplemented paper")

        paper = httpx.get(f"{b}/papers/B-001", timeout=5, trust_env=False).json()
        assert paper["score"] is None and paper["version"] == 2, paper
        checks.append("补页后的旧版交分（version=1）和不带版本交分均被 409 拒绝，分数仍空缺")

        # Current version (string form too) is accepted.
        scored = expect(
            httpx.post(f"{b}/papers/B-001/score", json={"score": 93, "version": "2"},
                       timeout=5, trust_env=False),
            200, "current version score",
        )
        assert scored["score"] == 93 and scored["status"] == "scored"
        checks.append("带当前版本 version=2 的交分成功（兼容字符串版本号）")

        # Second supplement + same-version-once enforcement.
        v3_body = "V3 再次补页后的全文"
        sup2 = expect(
            httpx.post(f"{b}/supplement",
                       json={"paper_no": "B-001", "version": 2, "body": v3_body},
                       timeout=5, trust_env=False),
            200, "second supplement",
        )
        assert sup2["version"] == 3 and sup2["withdrawn_score"] == 93, sup2

        r = httpx.post(f"{b}/papers/B-001/supplement",
                       json={"seen_version": 2, "body": "duplicate"},
                       timeout=5, trust_env=False)
        expect(r, 409, "duplicate same version supplement")
        r = httpx.post(f"{b}/papers/B-001/supplement",
                       json={"seen_version": 1, "body": "stale"},
                       timeout=5, trust_env=False)
        expect(r, 409, "stale version supplement")
        history = httpx.get(f"{b}/papers/B-001/history", timeout=5, trust_env=False).json()
        assert history["count"] == 2, history
        current = httpx.get(f"{b}/papers/B-001?include_body=true", timeout=5, trust_env=False).json()
        assert current["version"] == 3 and current["body"] == v3_body and current["score"] is None
        checks.append("同一答卷的同一版本只能补页一次；重复/过期提交 409 且卷面停在 v3")

        # Old image + score queryable.
        v1 = expect(httpx.get(f"{b}/papers/B-001/versions/1", timeout=5, trust_env=False),
                    200, "version 1")
        assert "LEGACY B-001 原始卷面" in v1["body"] and v1["score"] == 88, v1
        v2 = expect(httpx.get(f"{b}/papers/B-001/versions/2", timeout=5, trust_env=False),
                    200, "version 2")
        assert v2["body"] == v2_body and v2["score"] == 93, v2
        aliases = [
            httpx.get(f"{b}/papers/B-001/supplements", timeout=5, trust_env=False).status_code,
            httpx.get(f"{b}/papers/B-001/revisions", timeout=5, trust_env=False).status_code,
        ]
        assert aliases == [200, 200], aliases
        checks.append("旧卷面与当时分数（88、93）可经 /history、/versions/{n} 及别名查回")

        # End-to-end transfer into the over-quota receiver.
        payload = {"order_no": "TR-SUP-Q1", "paper_nos": ["A-001"], "target_base_url": b}
        order = expect(httpx.post(f"{a}/transfers", json=payload, timeout=8, trust_env=False),
                       200, "transfer at over quota")
        assert order["decision"] == "rejected", order
        rr = order["receiver_response"]
        assert rr["reason"] == "quota_exceeded" and rr["unscored_count_before"] == 4, rr
        assert httpx.get(f"{b}/papers/A-001", timeout=5, trust_env=False).status_code == 404
        checks.append("满额补页后接收新调卷：补页待评卷计入限额，A-001 整批被 quota_exceeded 拒收且未入账")

        # Free two slots at B (B-002/B-003 never supplemented -> no version).
        for no in ("B-002", "B-003"):
            expect(httpx.post(f"{b}/papers/{no}/score", json={"score": 70},
                              timeout=5, trust_env=False), 200, f"score {no}")
        cap = httpx.get(f"{b}/capacity", timeout=5, trust_env=False).json()
        assert cap["unscored_count"] == 2, cap  # B-001 regrade v3 + B-004

        # Same order keeps its first rejection.
        order = expect(httpx.post(f"{a}/transfers", json=payload, timeout=8, trust_env=False),
                       200, "retry rejected order")
        assert order["decision"] == "rejected", order

        # New order fits: 2 + 1 <= 3.
        order2 = expect(
            httpx.post(f"{a}/transfers",
                       json={"order_no": "TR-SUP-Q2", "paper_nos": ["A-001"], "target_base_url": b},
                       timeout=8, trust_env=False),
            200, "new transfer after freeing quota",
        )
        assert order2["decision"] == "accepted", order2
        opened_a1 = expect(httpx.post(f"{b}/papers/A-001/open", timeout=5, trust_env=False),
                          200, "open transferred A-001 at B")
        assert opened_a1["version"] == 1 and "匿名答卷 A-001" in opened_a1["body"]
        checks.append("腾出名额后新单号整批接收 A-001；旧拒收单 TR-SUP-Q1 仍查回首次拒收")

        # Restart B: persistence + continued version gate.
        proc_b = restart_service(proc_b, "point-b-sup", port_b, db_b, env_b)
        processes.append(proc_b)
        paper = httpx.get(f"{b}/papers/B-001", timeout=5, trust_env=False).json()
        assert paper["version"] == 3 and paper["supplement_count"] == 2 and paper["score"] is None
        history = httpx.get(f"{b}/papers/B-001/history", timeout=5, trust_env=False).json()
        assert history["count"] == 2
        assert history["versions"][0]["score"] == 88
        r = httpx.post(f"{b}/papers/B-001/score", json={"score": 50, "version": 2},
                       timeout=5, trust_env=False)
        expect(r, 409, "old version after restart")
        expect(httpx.post(f"{b}/papers/B-001/score", json={"score": 95, "version": 3},
                          timeout=5, trust_env=False), 200, "current version after restart")
        checks.append("接收点重启后 v3/两条补页历史持久化，旧版交分仍拒绝、当前版交分成功")

        # Unsupplemented paper still accepts version-less scoring end to end.
        expect(httpx.post(f"{b}/papers/B-004/score", json={"score": 61},
                          timeout=5, trust_env=False), 200, "legacy version-less score")
        checks.append("未补过页的卷（B-004）仍接受不带版本的交分")

        print("\n补页特性实测全部通过：")
        for item in checks:
            print(f"  ✓ {item}")
        print("\nSUPPLEMENT_CHECK_OK")
        print(f"数据库保留在: {temp_root}")
        return 0
    finally:
        for process in processes:
            stop_service(process)
        if os.getenv("CLEANUP_TEMP") == "1":
            shutil.rmtree(temp_root, ignore_errors=True)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"\nSUPPLEMENT_CHECK_FAILED: {exc}", file=sys.stderr)
        raise
