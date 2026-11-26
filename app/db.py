"""SQLite persistence helpers and deterministic built-in answer sheets."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import Settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS papers (
    paper_no TEXT PRIMARY KEY,
    status TEXT NOT NULL CHECK (
        status IN ('unopened', 'open', 'scored', 'transfer_pending', 'transferred_out')
    ),
    body TEXT NOT NULL,
    -- Optimistic-concurrency revision.  Every successful page supplement
    -- increments version by exactly one; rows created by the old schema are
    -- migrated to version=1.
    version INTEGER NOT NULL DEFAULT 1,
    supplement_count INTEGER NOT NULL DEFAULT 0,
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

CREATE INDEX IF NOT EXISTS idx_papers_status ON papers(status);
CREATE INDEX IF NOT EXISTS idx_papers_transfer_order
    ON papers(transfer_order_no);

-- One row per successful page-supplement ("补页").  The UNIQUE constraint is
-- the durable guarantee that the same (paper, seen/base version) can only be
-- supplemented once.  old_body/old_score_* preserve the retired paper image
-- and the score that was withdrawn, so both remain queryable forever.
CREATE TABLE IF NOT EXISTS paper_supplements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    paper_no TEXT NOT NULL,
    seen_version INTEGER NOT NULL,
    new_version INTEGER NOT NULL,
    old_body TEXT NOT NULL,
    new_body TEXT NOT NULL,
    old_status TEXT NOT NULL,
    old_score REAL,
    old_score_comment TEXT,
    old_grader TEXT,
    old_scored_at TEXT,
    operator TEXT,
    note TEXT,
    created_at TEXT NOT NULL,
    UNIQUE (paper_no, seen_version),
    FOREIGN KEY (paper_no) REFERENCES papers(paper_no)
);

CREATE INDEX IF NOT EXISTS idx_paper_supplements_paper
    ON paper_supplements(paper_no, id);

CREATE TABLE IF NOT EXISTS transfer_orders (
    order_no TEXT PRIMARY KEY,
    target_site TEXT,
    target_base_url TEXT,
    status TEXT NOT NULL CHECK (
        status IN ('pending_receiver', 'accepted', 'rejected', 'completed_locally')
    ),
    request_json TEXT NOT NULL,
    requested_nos_json TEXT NOT NULL,
    candidate_nos_json TEXT NOT NULL,
    kept_nos_json TEXT NOT NULL,
    receiver_decision TEXT,
    receiver_reason TEXT,
    receiver_response_json TEXT,
    last_error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_transfer_orders_status
    ON transfer_orders(status);

CREATE TABLE IF NOT EXISTS transfer_items (
    order_no TEXT NOT NULL,
    paper_no TEXT NOT NULL,
    role TEXT NOT NULL CHECK (role IN ('candidate', 'kept')),
    origin_status TEXT NOT NULL,
    disposition TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (order_no, paper_no),
    FOREIGN KEY (order_no) REFERENCES transfer_orders(order_no)
);

CREATE TABLE IF NOT EXISTS inbound_orders (
    order_no TEXT PRIMARY KEY,
    source_site TEXT NOT NULL,
    decision TEXT NOT NULL CHECK (decision IN ('accepted', 'rejected')),
    reason TEXT,
    request_json TEXT NOT NULL,
    response_json TEXT NOT NULL,
    unscored_count_before INTEGER NOT NULL,
    unscored_count_after INTEGER NOT NULL,
    quota_limit INTEGER,
    created_at TEXT NOT NULL,
    decided_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inbound_items (
    order_no TEXT NOT NULL,
    paper_no TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (order_no, paper_no),
    FOREIGN KEY (order_no) REFERENCES inbound_orders(order_no)
);

CREATE INDEX IF NOT EXISTS idx_inbound_items_paper ON inbound_items(paper_no);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def connect(database_path: str) -> sqlite3.Connection:
    path = Path(database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    # FULL makes a committed receiver decision survive power/process loss.
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def begin_immediate(conn: sqlite3.Connection) -> None:
    conn.execute("BEGIN IMMEDIATE")


def _seed_body(prefix: str, number: int) -> str:
    paper_no = f"{prefix}-{number:03d}"
    variants = [
        (
            "第一题 · 古诗文理解",
            "考生先解释了“明月”在诗中寄托的思乡之情，又结合后两句说明诗人由眼前月色联想到故乡。"
            "作答能抓住意象，但对转折关系的分析略短。",
        ),
        (
            "第二题 · 现代文阅读",
            "答卷概括了主人公三次选择：离开家乡、返回县城、参与社区改造。"
            "文中细节引用较完整，能够说明“道路”在文本中的双层含义。",
        ),
        (
            "第三题 · 语言运用",
            "答案给出了活动主题、实施步骤和宣传方式，步骤包括问卷访谈、展板制作与成果分享。"
            "表达通顺，建议部分可再具体。",
        ),
        (
            "第四题 · 写作",
            "作文以一次夜间抢修串联祖孙两代人的职业选择，材料真实，结构完整。"
            "细节描写集中在工具、雨声和灯光，结尾扣住“责任的传递”。",
        ),
    ]
    title, answer = variants[(number - 1) % len(variants)]
    return (
        f"匿名答卷 {paper_no}\n"
        f"（本卷为系统内置测试答卷，不含考生姓名、考号或单位信息）\n\n"
        f"{title}\n"
        f"作答正文：\n{answer}\n\n"
        f"补充段落 {number}：阅卷点可依据这段文字核对开卷后取得的正文是否完整。"
    )


def seed_if_empty(conn: sqlite3.Connection, settings: Settings) -> None:
    count = conn.execute("SELECT COUNT(*) AS n FROM papers").fetchone()["n"]
    if count or settings.seed_count <= 0:
        return

    timestamp = now_iso()
    rows: list[tuple[Any, ...]] = []
    for number in range(1, settings.seed_count + 1):
        paper_no = f"{settings.seed_prefix}-{number:03d}"
        rows.append(
            (
                paper_no,
                "unopened",
                _seed_body(settings.seed_prefix, number),
                None,
                None,
                None,
                settings.site_id,
                None,
                None,
                None,
                None,
                timestamp,
                timestamp,
            )
        )
    conn.executemany(
        """
        INSERT INTO papers (
            paper_no, status, body, score, score_comment, grader,
            source_site, transfer_order_no, opened_at, scored_at,
            transferred_at, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )


def migrate_sqlite(conn: sqlite3.Connection) -> None:
    """Add post-archive columns to databases created by the initial release.

    The project ships named Docker volumes with real grading data, so startup
    must upgrade an old file in place instead of recreating it.  SQLite supports
    ``ADD COLUMN ... NOT NULL DEFAULT ...`` and fills existing rows with the
    default (version 1 = the original scanned image, never supplemented).
    """

    paper_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(papers)").fetchall()
    }
    if paper_columns:  # table exists; fresh schemas already contain the columns
        if "version" not in paper_columns:
            conn.execute("ALTER TABLE papers ADD COLUMN version INTEGER NOT NULL DEFAULT 1")
        if "supplement_count" not in paper_columns:
            conn.execute(
                "ALTER TABLE papers ADD COLUMN supplement_count INTEGER NOT NULL DEFAULT 0"
            )


def init_db(settings: Settings) -> None:
    conn = connect(settings.database_path)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(SCHEMA)
        migrate_sqlite(conn)
        seed_if_empty(conn, settings)
    finally:
        conn.close()
