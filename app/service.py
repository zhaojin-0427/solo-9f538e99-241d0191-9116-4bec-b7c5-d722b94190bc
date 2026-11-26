"""Core grading and transfer use cases.

The important invariant is that an origin paper is durably moved to
``transfer_pending`` *before* the HTTP call to the receiver.  It therefore
cannot be opened while the receiver outcome is unknown.  Receiver decisions
are keyed by the caller's order number, making retries safe after a lost
response or an origin restart.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from typing import Any

import httpx

from .config import Settings
from .db import begin_immediate, connect, now_iso
from .errors import ApiError

OPEN_LIKE_STATUSES = {"open", "scored"}
UNAVAILABLE_AT_ORIGIN = {"transfer_pending", "transferred_out"}
TERMINAL_ORDER_STATUSES = {"accepted", "rejected", "completed_locally"}


# ---------------------------------------------------------------------------
# Version / text normalization
# ---------------------------------------------------------------------------


def normalize_version(value: Any) -> int | None:
    """Accept integers, numeric strings and ``"v2"``-style tokens.

    ``None``/empty means "client did not send a version".  Unparseable values
    are a 422 rather than silently being treated as the current revision.
    """

    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ApiError(422, "invalid_version", "版本号必须是整数")
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    match = re.fullmatch(r"[vVrR]?\s*(\d+)", text)
    if not match:
        raise ApiError(
            422,
            "invalid_version",
            f"无法识别的版本号 {value!r}，应为从 1 开始的整数",
            {"value": value},
        )
    return int(match.group(1))


def coerce_full_text(value: Any) -> str | None:
    """Tolerate strings, lists of pages and small dict payloads."""

    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, (list, tuple, set)):
        parts = [coerce_full_text(item) for item in value]
        return "\n\n".join(part for part in parts if part)
    if isinstance(value, dict):
        for key in ("full_text", "fullText", "body", "content", "text", "pages", "正文", "全文"):
            if key in value:
                text = coerce_full_text(value[key])
                if text is not None:
                    return text
        return _json_dumps(value)
    return str(value)


def _resolve_new_body(full_text: Any, added_pages: Any, old_body: str) -> str | None:
    new_body = coerce_full_text(full_text)
    if new_body is not None:
        return new_body
    extra = coerce_full_text(added_pages)
    if extra is not None and extra.strip():
        base = old_body or ""
        if base and not base.endswith("\n"):
            base += "\n"
        return f"{base}\n【补页】\n{extra}" if base.strip() else extra
    return None


def _row_version(row: Any) -> int:
    try:
        return int(row["version"])
    except (IndexError, KeyError, TypeError, ValueError):
        return 1


def _row_supplement_count(row: Any) -> int:
    try:
        return int(row["supplement_count"] or 0)
    except (IndexError, KeyError, TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_loads(raw: str | None, default: Any) -> Any:
    if not raw:
        return default
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return default


def normalize_paper_nos(value: Any) -> list[str]:
    """Accept arrays, comma/newline text, numbers, or objects with an ID key."""

    if value is None:
        return []

    raw_values: list[Any]
    if isinstance(value, str):
        raw_values = [part for chunk in value.replace("，", ",").split(",") for part in chunk.splitlines()]
        # Whitespace-separated batches are also accepted.
        expanded: list[Any] = []
        for item in raw_values:
            expanded.extend(item.split())
        raw_values = expanded
    elif isinstance(value, (list, tuple, set)):
        raw_values = list(value)
    else:
        raw_values = [value]

    result: list[str] = []
    seen: set[str] = set()

    def add(raw: Any) -> None:
        if raw is None:
            return
        if isinstance(raw, dict):
            raw = (
                raw.get("paper_no")
                or raw.get("paperNo")
                or raw.get("paper_id")
                or raw.get("paperId")
                or raw.get("id")
                or raw.get("卷号")
            )
        text = str(raw).strip()
        if not text:
            return
        if text not in seen:
            seen.add(text)
            result.append(text)

    for item in raw_values:
        add(item)

    return result


def _row_to_paper(row: Any, include_body: bool = False) -> dict[str, Any]:
    data = dict(row)
    if not include_body:
        data.pop("body", None)
    version = int(data.get("version") or 1)
    supplement_count = int(data.get("supplement_count") or 0)
    supplemented = version > 1 or supplement_count > 0
    status = data.get("status")
    data["version"] = version
    data["current_version"] = version
    data["paper_version"] = version
    data["revision"] = version
    data["rev"] = version
    data["supplement_count"] = supplement_count
    data["supplemented"] = supplemented
    # A supplemented paper must be re-graded against the new full image.
    data["version_required"] = supplemented
    data["regrade_pending"] = bool(supplemented and status == "open")
    data["needs_regrade"] = data["regrade_pending"]
    data["awaiting_grade"] = status in {"open", "unopened"} and status != "scored"
    if status == "scored":
        data["grading_status"] = "scored"
    elif data["regrade_pending"]:
        data["grading_status"] = "pending_regrade"
    else:
        data["grading_status"] = "pending_grade"
    return data


def capacity(settings: Settings) -> dict[str, Any]:
    conn = connect(settings.database_path)
    try:
        row = conn.execute(
            """
            SELECT COUNT(*) AS n
            FROM papers
            WHERE status NOT IN ('scored', 'transferred_out')
            """
        ).fetchone()
        unscored = int(row["n"])
        limit = settings.max_unscored
        remaining = None if limit is None else limit - unscored
        return {
            "site_id": settings.site_id,
            "unscored_count": unscored,
            "max_unscored": limit,
            "remaining_quota": remaining,
            "unlimited": limit is None,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Paper opening and scoring
# ---------------------------------------------------------------------------


def list_papers(
    settings: Settings,
    status_filter: str | None = None,
    include_body: bool = False,
    limit: int = 500,
) -> list[dict[str, Any]]:
    conn = connect(settings.database_path)
    try:
        safe_limit = max(1, min(int(limit or 500), 1000))
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM papers WHERE status = ? ORDER BY paper_no LIMIT ?",
                (status_filter, safe_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM papers ORDER BY paper_no LIMIT ?",
                (safe_limit,),
            ).fetchall()
        return [_row_to_paper(row, include_body=include_body) for row in rows]
    finally:
        conn.close()


def get_paper(settings: Settings, paper_no: str, include_body: bool = False) -> dict[str, Any]:
    conn = connect(settings.database_path)
    try:
        row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
        if row is None:
            raise ApiError(404, "paper_not_found", f"找不到卷号 {paper_no}", {"paper_no": paper_no})
        return _row_to_paper(row, include_body=include_body)
    finally:
        conn.close()


def open_paper(settings: Settings, paper_no: str) -> dict[str, Any]:
    paper_no = paper_no.strip()
    conn = connect(settings.database_path)
    try:
        begin_immediate(conn)
        row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
        if row is None:
            raise ApiError(404, "paper_not_found", f"找不到卷号 {paper_no}", {"paper_no": paper_no})

        if row["status"] in UNAVAILABLE_AT_ORIGIN:
            raise ApiError(
                409,
                "paper_transfer_paused",
                f"卷号 {paper_no} 已因调卷暂停开卷或已转走，原点不能再开卷",
                {
                    "paper_no": paper_no,
                    "status": row["status"],
                    "transfer_order_no": row["transfer_order_no"],
                },
            )

        already_open = row["status"] in {"open", "scored"}
        timestamp = now_iso()
        if row["status"] == "unopened":
            conn.execute(
                """
                UPDATE papers
                SET status = 'open', opened_at = ?, updated_at = ?
                WHERE paper_no = ?
                """,
                (timestamp, timestamp, paper_no),
            )
        conn.commit()

        row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
        result = _row_to_paper(row, include_body=True)
        result["already_open"] = already_open
        result["message"] = "已开卷，正文见 body 字段" if not already_open else "该卷此前已开卷，返回原正文"
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def submit_score(
    settings: Settings,
    paper_no: str,
    score: float,
    comment: str | None = None,
    grader: str | None = None,
    claimed_version: Any = None,
) -> dict[str, Any]:
    paper_no = paper_no.strip()
    try:
        score = float(score)
    except (TypeError, ValueError):
        raise ApiError(422, "invalid_score", "分数必须是数字") from None

    if score != score or score in (float("inf"), float("-inf")):
        raise ApiError(422, "invalid_score", "分数不能是 NaN 或无穷大")
    if score < settings.score_min or score > settings.score_max:
        raise ApiError(
            422,
            "score_out_of_range",
            f"分数需在 {settings.score_min}–{settings.score_max} 之间",
            {"min": settings.score_min, "max": settings.score_max},
        )

    claimed = normalize_version(claimed_version)

    conn = connect(settings.database_path)
    try:
        begin_immediate(conn)
        row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
        if row is None:
            raise ApiError(404, "paper_not_found", f"找不到卷号 {paper_no}", {"paper_no": paper_no})

        if row["status"] in UNAVAILABLE_AT_ORIGIN:
            raise ApiError(
                409,
                "paper_not_at_origin",
                f"卷号 {paper_no} 正在调卷或已转走，不能在原点交分",
                {
                    "paper_no": paper_no,
                    "status": row["status"],
                    "transfer_order_no": row["transfer_order_no"],
                },
            )

        current_version = _row_version(row)
        supplemented = current_version > 1 or _row_supplement_count(row) > 0
        # Version contract:
        # * never-supplemented papers accept the legacy version-less score;
        # * supplemented papers require the current version exactly;
        # * whenever a version IS sent it must match the current one.
        if claimed is None and supplemented:
            raise ApiError(
                409,
                "version_conflict",
                (
                    f"卷号 {paper_no} 已补页到版本 {current_version}，"
                    "必须提交与当前版本匹配的 version 后才能交分"
                ),
                {
                    "paper_no": paper_no,
                    "reason": "version_required",
                    "current_version": current_version,
                    "submitted_version": None,
                    "version_required": True,
                },
            )
        if claimed is not None and claimed != current_version:
            stale = claimed < current_version
            raise ApiError(
                409,
                "version_conflict",
                (
                    f"交分所依据的版本 {claimed} 不是当前版本 {current_version}"
                    + ("（旧版卷面和当时分数已归入历史）" if stale else "")
                    + "，请按最新正文重新评阅后用当前版本交分"
                ),
                {
                    "paper_no": paper_no,
                    "reason": "stale_version" if stale else "version_mismatch",
                    "current_version": current_version,
                    "seen_version": claimed,
                    "submitted_version": claimed,
                    "stale": stale,
                    "version_required": supplemented,
                },
            )

        timestamp = now_iso()
        previously_scored = row["status"] == "scored"
        # Lenient workflow: a direct score call also reveals/opens the paper.
        conn.execute(
            """
            UPDATE papers
            SET status = 'scored',
                score = ?,
                score_comment = ?,
                grader = COALESCE(?, grader),
                opened_at = COALESCE(opened_at, ?),
                scored_at = COALESCE(scored_at, ?),
                updated_at = ?
            WHERE paper_no = ?
            """,
            (score, comment, grader, timestamp, timestamp, timestamp, paper_no),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
        result = _row_to_paper(row, include_body=False)
        result["previously_scored"] = previously_scored
        result["message"] = "分数已提交" + ("；原分数已更新" if previously_scored else "")
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Page supplements ("补页"): versioned full text + withdrawn-score archive
# ---------------------------------------------------------------------------


def _supplement_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    seen = int(data["seen_version"])
    new_version = int(data["new_version"])
    # Discoverability aliases for callers/tests expecting different names.
    data["base_version"] = seen
    data["old_version"] = seen
    data["version"] = new_version
    data["result_version"] = new_version
    data["previous_version"] = seen
    data["submitted_at"] = data.get("created_at")
    data["timestamp"] = data.get("created_at")
    data["old_body"] = data.get("old_body")
    data["previous_body"] = data.get("old_body")
    data["body_before"] = data.get("old_body")
    data["retired_body"] = data.get("old_body")
    data["new_body"] = data.get("new_body")
    data["body_after"] = data.get("new_body")
    data["current_body"] = data.get("new_body")
    # "当时的分数" exposed under every likely key.
    data["score"] = data.get("old_score")
    data["old_score"] = data.get("old_score")
    data["withdrawn_score"] = data.get("old_score")
    data["score_at_supplement_time"] = data.get("old_score")
    data["score_snapshot"] = {
        "score": data.get("old_score"),
        "score_comment": data.get("old_score_comment"),
        "grader": data.get("old_grader"),
        "scored_at": data.get("old_scored_at"),
        "status": data.get("old_status"),
    }
    data["before_snapshot"] = {
        "version": seen,
        "status": data.get("old_status"),
        "body": data.get("old_body"),
        "score": data.get("old_score"),
        "score_comment": data.get("old_score_comment"),
        "grader": data.get("old_grader"),
        "scored_at": data.get("old_scored_at"),
    }
    data["after_snapshot"] = {"version": new_version, "body": data.get("new_body"), "status": "open"}
    return data


def supplement_paper(
    settings: Settings,
    paper_no: str | None,
    seen_version_raw: Any = None,
    full_text: Any = None,
    operator: str | None = None,
    note: str | None = None,
    added_pages: Any = None,
) -> dict[str, Any]:
    """Apply a leader page-completion to an open/scored paper.

    Runs entirely in one ``BEGIN IMMEDIATE`` transaction together with the
    archive insert, score withdrawal and status/quota-affecting transition,
    so any failure leaves body, score and quota untouched.
    """

    if paper_no is None or not str(paper_no).strip():
        raise ApiError(422, "missing_paper_no", "必须提供卷号 paper_no")
    paper_no = str(paper_no).strip()

    conn = connect(settings.database_path)
    try:
        begin_immediate(conn)
        row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
        if row is None:
            raise ApiError(404, "paper_not_found", f"找不到卷号 {paper_no}", {"paper_no": paper_no})

        if row["status"] in UNAVAILABLE_AT_ORIGIN:
            raise ApiError(
                409,
                "paper_not_at_origin",
                f"卷号 {paper_no} 正在调卷或已转走，当前阅卷点不能补页",
                {
                    "paper_no": paper_no,
                    "status": row["status"],
                    "transfer_order_no": row["transfer_order_no"],
                },
            )

        if row["status"] not in {"open", "scored"}:
            raise ApiError(
                409,
                "supplement_not_allowed",
                f"只有已开卷或已交分的答卷才能补页；卷号 {paper_no} 当前状态为 {row['status']}",
                {"paper_no": paper_no, "status": row["status"]},
            )

        current_version = _row_version(row)
        seen_version = normalize_version(seen_version_raw)
        # Omitting a version is tolerated and means "I act on the current image".
        if seen_version is None:
            seen_version = current_version

        # Durable same-version-once rule, checked inside the write txn.
        duplicate = conn.execute(
            "SELECT id, new_version, created_at FROM paper_supplements "
            "WHERE paper_no = ? AND seen_version = ?",
            (paper_no, seen_version),
        ).fetchone()
        if duplicate is not None:
            raise ApiError(
                409,
                "version_conflict",
                f"卷号 {paper_no} 的版本 {seen_version} 已经补过页；同一答卷的同一版本只能补页一次",
                {
                    "paper_no": paper_no,
                    "reason": "supplement_already_applied",
                    "code_alias": "supplement_version_already_applied",
                    "seen_version": seen_version,
                    "current_version": current_version,
                    "existing_supplement_id": duplicate["id"],
                    "supplemented_at": duplicate["created_at"],
                },
            )

        if seen_version != current_version:
            raise ApiError(
                409,
                "version_conflict",
                (
                    f"所见版本 {seen_version} 与当前版本 {current_version} 不一致；"
                    "请重新开卷取得最新正文和版本后再补页"
                ),
                {
                    "paper_no": paper_no,
                    "reason": "stale_version" if seen_version < current_version else "version_mismatch",
                    "seen_version": seen_version,
                    "current_version": current_version,
                },
            )

        # State/version checks win over payload validation so a replay of an
        # already-applied version is always a 409.
        new_body = _resolve_new_body(full_text, added_pages, row["body"])
        if new_body is None or not str(new_body).strip():
            raise ApiError(
                422,
                "missing_full_text",
                "必须通过 body/full_text（或 added_pages）提供补齐后的全文",
                {"paper_no": paper_no},
            )

        timestamp = now_iso()
        new_version = current_version + 1
        old_body = row["body"]
        old_score = row["score"]
        old_score_comment = row["score_comment"]
        old_grader = row["grader"]
        old_scored_at = row["scored_at"]
        old_status = row["status"]

        try:
            cursor = conn.execute(
                """
                INSERT INTO paper_supplements (
                    paper_no, seen_version, new_version,
                    old_body, new_body, old_status,
                    old_score, old_score_comment, old_grader, old_scored_at,
                    operator, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    paper_no,
                    seen_version,
                    new_version,
                    old_body,
                    new_body,
                    old_status,
                    old_score,
                    old_score_comment,
                    old_grader,
                    old_scored_at,
                    operator,
                    note,
                    timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Race backstop for the UNIQUE(paper_no, seen_version) constraint.
            raise ApiError(
                409,
                "version_conflict",
                f"卷号 {paper_no} 的版本 {seen_version} 已经补过页；同一答卷的同一版本只能补页一次",
                {
                    "paper_no": paper_no,
                    "reason": "supplement_already_applied",
                    "seen_version": seen_version,
                    "current_version": current_version,
                },
            ) from exc

        supplement_id = cursor.lastrowid

        # Withdraw the score and return the paper to ``open`` (= present at
        # this grading point, awaiting re-grade).  That status is already part
        # of the unscored quota count used by /capacity and intake.
        conn.execute(
            """
            UPDATE papers
            SET body = ?,
                version = ?,
                supplement_count = supplement_count + 1,
                status = 'open',
                score = NULL,
                score_comment = NULL,
                grader = NULL,
                scored_at = NULL,
                updated_at = ?
            WHERE paper_no = ?
            """,
            (new_body, new_version, timestamp, paper_no),
        )
        conn.commit()

        row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
        supp_row = conn.execute(
            "SELECT * FROM paper_supplements WHERE id = ?", (supplement_id,)
        ).fetchone()
        result = _row_to_paper(row, include_body=True)
        supplement = _supplement_dict(supp_row)
        result["supplement"] = supplement
        result["supplement_id"] = supplement_id
        result["seen_version"] = seen_version
        result["base_version"] = seen_version
        result["old_version"] = current_version
        result["previous_version"] = current_version
        result["new_version"] = new_version
        result["result_version"] = new_version
        result["withdrawn_score"] = old_score
        result["old_score"] = old_score
        result["score_withdrawn"] = old_score is not None
        result["message"] = (
            "补页成功：全文已更新到版本 "
            f"{new_version}，原分数已撤下，答卷留在本阅卷点重新待评"
            + ("（原分数 " + str(old_score) + " 可在补页历史中查询）" if old_score is not None else "")
        )
        return result
    except ApiError:
        conn.rollback()
        raise
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _fetch_paper_or_404(conn: Any, paper_no: str) -> Any:
    row = conn.execute("SELECT * FROM papers WHERE paper_no = ?", (paper_no,)).fetchone()
    if row is None:
        raise ApiError(404, "paper_not_found", f"找不到卷号 {paper_no}", {"paper_no": paper_no})
    return row


def get_paper_history(settings: Settings, paper_no: str) -> dict[str, Any]:
    paper_no = paper_no.strip()
    conn = connect(settings.database_path)
    try:
        paper = _fetch_paper_or_404(conn, paper_no)
        rows = conn.execute(
            "SELECT * FROM paper_supplements WHERE paper_no = ? ORDER BY seen_version ASC, id ASC",
            (paper_no,),
        ).fetchall()
        supplements = [_supplement_dict(row) for row in rows]
        current_version = _row_version(paper)

        # Reconstruct every version image: a supplement row with
        # seen_version=v stores exactly the body/score that version v had.
        versions: list[dict[str, Any]] = []
        for item in supplements:
            versions.append(
                {
                    "paper_no": paper_no,
                    "version": item["seen_version"],
                    "body": item["old_body"],
                    "score": item["old_score"],
                    "score_comment": item["old_score_comment"],
                    "grader": item["old_grader"],
                    "scored_at": item["old_scored_at"],
                    "status_before_supplement": item["old_status"],
                    "superseded_by_version": item["new_version"],
                    "supplemented_at": item["created_at"],
                    "is_current": False,
                    "current": False,
                }
            )
        versions.append(
            {
                "paper_no": paper_no,
                "version": current_version,
                "body": paper["body"],
                "score": paper["score"],
                "score_comment": paper["score_comment"],
                "grader": paper["grader"],
                "scored_at": paper["scored_at"],
                "status": paper["status"],
                "is_current": True,
                "current": True,
            }
        )

        return {
            "paper_no": paper_no,
            "site_id": settings.site_id,
            "current_version": current_version,
            "version": current_version,
            "supplement_count": len(supplements),
            "count": len(supplements),
            # Primary documented key plus aliases.
            "supplements": supplements,
            "history": supplements,
            "revisions": supplements,
            "records": supplements,
            "items": supplements,
            "versions": versions,
            "paper": _row_to_paper(paper, include_body=False),
        }
    finally:
        conn.close()


def get_paper_version(settings: Settings, paper_no: str, version_raw: Any) -> dict[str, Any]:
    paper_no = paper_no.strip()
    version = normalize_version(version_raw)
    if version is None:
        raise ApiError(422, "invalid_version", "必须在路径中提供整数版本号")

    conn = connect(settings.database_path)
    try:
        paper = _fetch_paper_or_404(conn, paper_no)
        current_version = _row_version(paper)

        if version == current_version:
            return {
                "paper_no": paper_no,
                "version": current_version,
                "body": paper["body"],
                "score": paper["score"],
                "score_comment": paper["score_comment"],
                "grader": paper["grader"],
                "scored_at": paper["scored_at"],
                "status": paper["status"],
                "is_current": True,
                "current": True,
            }

        row = conn.execute(
            "SELECT * FROM paper_supplements WHERE paper_no = ? AND seen_version = ?",
            (paper_no, version),
        ).fetchone()
        if row is None:
            # Maybe the requested number is a version *created* by a supplement.
            row = conn.execute(
                "SELECT * FROM paper_supplements WHERE paper_no = ? AND new_version = ?",
                (paper_no, version),
            ).fetchone()
            if row is None:
                raise ApiError(
                    404,
                    "version_not_found",
                    f"卷号 {paper_no} 没有版本 {version} 的卷面记录",
                    {"paper_no": paper_no, "version": version, "current_version": current_version},
                )
            return {
                "paper_no": paper_no,
                "version": version,
                "body": row["new_body"],
                # The score carried by that image before its next supplement,
                # when available via the matching seen_version row.
                "score": None,
                "created_by_supplement_id": row["id"],
                "created_at": row["created_at"],
                "is_current": False,
                "current": False,
            }

        return {
            "paper_no": paper_no,
            "version": version,
            "body": row["old_body"],
            "score": row["old_score"],
            "old_score": row["old_score"],
            "score_comment": row["old_score_comment"],
            "grader": row["old_grader"],
            "scored_at": row["old_scored_at"],
            "status_before_supplement": row["old_status"],
            "superseded_by_version": row["new_version"],
            "supplemented_at": row["created_at"],
            "is_current": False,
            "current": False,
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Origin-side transfer order
# ---------------------------------------------------------------------------


def _transfer_dict(conn: Any, row: Any) -> dict[str, Any]:
    data = dict(row)
    requested = _json_loads(data.pop("request_json", None), {})
    requested_nos = _json_loads(data.pop("requested_nos_json", None), [])
    candidate_nos = _json_loads(data.pop("candidate_nos_json", None), [])
    kept_nos = _json_loads(data.pop("kept_nos_json", None), [])
    receiver_response = _json_loads(data.pop("receiver_response_json", None), None)

    status = data["status"]
    if status == "accepted":
        transferred = list(candidate_nos)
        kept_final = list(kept_nos)
        paused: list[str] = []
        restored: list[str] = []
    elif status == "rejected":
        transferred = []
        restored = list(candidate_nos)
        # Preserve the leader's batch ordering in the combined "kept" list.
        kept_set = set(kept_nos) | set(candidate_nos)
        kept_final = [no for no in requested_nos if no in kept_set]
        paused = []
    elif status == "completed_locally":
        transferred = []
        kept_final = list(kept_nos)
        paused = []
        restored = []
    else:
        transferred = []
        kept_final = list(kept_nos)
        paused = list(candidate_nos)
        restored = []

    item_rows = conn.execute(
        "SELECT paper_no, role, origin_status, disposition, position FROM transfer_items WHERE order_no = ? ORDER BY position",
        (data["order_no"],),
    ).fetchall()
    items = [dict(item) for item in item_rows]

    result = {
        **data,
        "request": requested,
        "requested_paper_nos": requested_nos,
        "candidate_paper_nos": candidate_nos,
        "kept_open_paper_nos": kept_nos,
        "receiver_response": receiver_response,
        "transferred_paper_nos": transferred,
        "kept_at_origin_paper_nos": kept_final,
        "paused_paper_nos": paused,
        "restored_paper_nos": restored,
        "items": items,
        # Short aliases used by the README and convenient for assertions.
        "transferred": transferred,
        "kept_at_origin": kept_final,
        "transferred_ids": transferred,
        "kept_ids": kept_final,
        "kept_at_origin_ids": kept_final,
        "转走卷号": transferred,
        "留在原点卷号": kept_final,
        "order_id": data["order_no"],
    }
    if status == "accepted":
        result["decision"] = "accepted"
    elif status == "rejected":
        result["decision"] = "rejected"
    elif status == "completed_locally":
        result["decision"] = "not_needed"
    else:
        result["decision"] = "pending"
    return result


def get_transfer(settings: Settings, order_no: str) -> dict[str, Any]:
    conn = connect(settings.database_path)
    try:
        row = conn.execute("SELECT * FROM transfer_orders WHERE order_no = ?", (order_no,)).fetchone()
        if row is None:
            raise ApiError(404, "transfer_not_found", f"找不到调卷单号 {order_no}", {"order_no": order_no})
        return _transfer_dict(conn, row)
    finally:
        conn.close()


def list_transfers(settings: Settings, status_filter: str | None = None) -> list[dict[str, Any]]:
    conn = connect(settings.database_path)
    try:
        if status_filter:
            rows = conn.execute(
                "SELECT * FROM transfer_orders WHERE status = ? ORDER BY created_at DESC, order_no",
                (status_filter,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM transfer_orders ORDER BY created_at DESC, order_no",
            ).fetchall()
        return [_transfer_dict(conn, row) for row in rows]
    finally:
        conn.close()


def create_transfer(
    settings: Settings,
    order_no: str,
    raw_paper_nos: Any,
    target_base_url: str | None = None,
    target_site: str | None = None,
) -> tuple[dict[str, Any], bool]:
    order_no = order_no.strip()
    paper_nos = normalize_paper_nos(raw_paper_nos)
    if not order_no:
        raise ApiError(422, "invalid_order_no", "单号不能为空")
    if not paper_nos:
        raise ApiError(422, "invalid_paper_nos", "卷号列表不能为空")

    target_url = (target_base_url or settings.default_target_base_url or "").strip().rstrip("/") or None
    normalized_request = {
        "order_no": order_no,
        "paper_nos": paper_nos,
        "target_base_url": target_url,
        "target_site": target_site,
    }

    conn = connect(settings.database_path)
    created = False
    try:
        begin_immediate(conn)
        existing = conn.execute(
            "SELECT * FROM transfer_orders WHERE order_no = ?", (order_no,)
        ).fetchone()
        if existing is not None:
            old_requested = _json_loads(existing["requested_nos_json"], [])
            if set(old_requested) != set(paper_nos):
                detail = _transfer_dict(conn, existing)
                raise ApiError(
                    409,
                    "order_no_conflict",
                    f"单号 {order_no} 已记录，但卷号批次与首次请求不一致；请使用原批次或新单号",
                    {"existing_order": detail},
                )
            if target_url and existing["target_base_url"] and target_url != existing["target_base_url"]:
                detail = _transfer_dict(conn, existing)
                raise ApiError(
                    409,
                    "order_target_conflict",
                    f"单号 {order_no} 已指定其他接收点",
                    {"existing_order": detail},
                )
            conn.commit()
            return _transfer_dict(conn, existing), False

        placeholders = ",".join("?" for _ in paper_nos)
        rows = conn.execute(
            f"SELECT * FROM papers WHERE paper_no IN ({placeholders})", paper_nos
        ).fetchall()
        by_no = {row["paper_no"]: row for row in rows}
        missing = [no for no in paper_nos if no not in by_no]
        if missing:
            raise ApiError(
                404,
                "paper_not_found",
                "批次中存在本原点没有的卷号，整批未受理",
                {"missing_paper_nos": missing},
            )

        busy = [
            {"paper_no": no, "status": by_no[no]["status"], "order_no": by_no[no]["transfer_order_no"]}
            for no in paper_nos
            if by_no[no]["status"] in UNAVAILABLE_AT_ORIGIN
        ]
        if busy:
            raise ApiError(
                409,
                "paper_already_transferring",
                "批次中已有卷被其他调卷单暂停或转走，整批未受理",
                {"papers": busy},
            )

        candidate_nos: list[str] = []
        kept_nos: list[str] = []
        item_records: list[tuple[Any, ...]] = []
        timestamp = now_iso()
        for position, no in enumerate(paper_nos):
            row = by_no[no]
            if row["status"] == "unopened":
                candidate_nos.append(no)
                conn.execute(
                    """
                    UPDATE papers
                    SET status = 'transfer_pending',
                        transfer_order_no = ?,
                        updated_at = ?
                    WHERE paper_no = ? AND status = 'unopened'
                    """,
                    (order_no, timestamp, no),
                )
                role = "candidate"
                disposition = "paused_before_receiver"
            else:
                # open or scored: already opened papers stay at the origin.
                kept_nos.append(no)
                role = "kept"
                disposition = "stay_at_origin"

            item_records.append((order_no, no, role, row["status"], disposition, position))

        if candidate_nos and not target_url:
            raise ApiError(
                400,
                "missing_target",
                "存在未开卷、需要转走的卷，但未提供 target_base_url，且服务未配置 DEFAULT_TARGET_BASE_URL",
                {"candidate_paper_nos": candidate_nos},
            )

        local_only = not candidate_nos
        conn.execute(
            """
            INSERT INTO transfer_orders (
                order_no, target_site, target_base_url, status,
                request_json, requested_nos_json, candidate_nos_json,
                kept_nos_json, receiver_decision, receiver_reason,
                receiver_response_json, last_error,
                created_at, updated_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_no,
                target_site,
                target_url,
                "completed_locally" if local_only else "pending_receiver",
                _json_dumps(normalized_request),
                _json_dumps(paper_nos),
                _json_dumps(candidate_nos),
                _json_dumps(kept_nos),
                "not_needed" if local_only else None,
                "all_selected_papers_already_opened" if local_only else None,
                None,
                None,
                timestamp,
                timestamp,
                timestamp if local_only else None,
            ),
        )
        conn.executemany(
            """
            INSERT INTO transfer_items (
                order_no, paper_no, role, origin_status,
                disposition, position
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            item_records,
        )
        conn.commit()
        created = True
        row = conn.execute("SELECT * FROM transfer_orders WHERE order_no = ?", (order_no,)).fetchone()
        return _transfer_dict(conn, row), created
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _receiver_payload(settings: Settings, order: dict[str, Any]) -> dict[str, Any]:
    candidate_nos = order["candidate_paper_nos"]
    if not candidate_nos:
        return {
            "order_no": order["order_no"],
            "source_site": settings.site_id,
            "target_site": order.get("target_site"),
            "paper_nos": [],
            "papers": [],
        }

    conn = connect(settings.database_path)
    try:
        placeholders = ",".join("?" for _ in candidate_nos)
        rows = conn.execute(
            f"SELECT paper_no, body FROM papers WHERE paper_no IN ({placeholders})",
            candidate_nos,
        ).fetchall()
        bodies = {row["paper_no"]: row["body"] for row in rows}
    finally:
        conn.close()

    return {
        "order_no": order["order_no"],
        "source_site": settings.site_id,
        "target_site": order.get("target_site"),
        "paper_nos": candidate_nos,
        "papers": [{"paper_no": no, "body": bodies.get(no, "")} for no in candidate_nos],
    }


def _record_origin_transport_error(settings: Settings, order_no: str, message: str) -> None:
    conn = connect(settings.database_path)
    try:
        conn.execute(
            "UPDATE transfer_orders SET last_error = ?, updated_at = ? WHERE order_no = ?",
            (message[:2000], now_iso(), order_no),
        )
    finally:
        conn.close()


def finalize_origin_from_receiver(
    settings: Settings,
    order_no: str,
    receiver_result: dict[str, Any],
) -> dict[str, Any]:
    decision = receiver_result.get("decision") or ("accepted" if receiver_result.get("accepted") else "rejected")
    if decision not in {"accepted", "rejected"}:
        raise ApiError(502, "bad_receiver_response", "接收点返回了无法识别的决定", {"receiver_response": receiver_result})

    conn = connect(settings.database_path)
    try:
        begin_immediate(conn)
        row = conn.execute("SELECT * FROM transfer_orders WHERE order_no = ?", (order_no,)).fetchone()
        if row is None:
            raise ApiError(404, "transfer_not_found", f"找不到调卷单号 {order_no}")
        if row["status"] in TERMINAL_ORDER_STATUSES:
            conn.commit()
            return _transfer_dict(conn, row)

        candidate_nos = _json_loads(row["candidate_nos_json"], [])
        receiver_nos = normalize_paper_nos(receiver_result.get("paper_nos"))
        if receiver_nos and set(receiver_nos) != set(candidate_nos):
            raise ApiError(
                502,
                "receiver_batch_mismatch",
                "接收点确认的卷号与原点待转批次不一致；为安全起见继续暂停开卷",
                {"expected": candidate_nos, "receiver": receiver_nos},
            )

        timestamp = now_iso()
        if decision == "accepted":
            conn.execute(
                """
                UPDATE papers
                SET status = 'transferred_out',
                    transferred_at = ?,
                    updated_at = ?
                WHERE transfer_order_no = ?
                  AND status = 'transfer_pending'
                """,
                (timestamp, timestamp, order_no),
            )
            conn.execute(
                """
                UPDATE transfer_items
                SET disposition = 'transferred'
                WHERE order_no = ? AND role = 'candidate'
                """,
                (order_no,),
            )
            status = "accepted"
            reason = receiver_result.get("reason")
        else:
            conn.execute(
                """
                UPDATE papers
                SET status = 'unopened',
                    transfer_order_no = NULL,
                    updated_at = ?
                WHERE transfer_order_no = ?
                  AND status = 'transfer_pending'
                """,
                (timestamp, order_no),
            )
            conn.execute(
                """
                UPDATE transfer_items
                SET disposition = 'returned_to_pool'
                WHERE order_no = ? AND role = 'candidate'
                """,
                (order_no,),
            )
            status = "rejected"
            reason = receiver_result.get("reason") or "rejected_by_receiver"

        conn.execute(
            """
            UPDATE transfer_orders
            SET status = ?,
                receiver_decision = ?,
                receiver_reason = ?,
                receiver_response_json = ?,
                last_error = NULL,
                updated_at = ?,
                completed_at = ?
            WHERE order_no = ?
            """,
            (
                status,
                decision,
                reason,
                _json_dumps(receiver_result),
                timestamp,
                timestamp,
                order_no,
            ),
        )
        conn.commit()
        row = conn.execute("SELECT * FROM transfer_orders WHERE order_no = ?", (order_no,)).fetchone()
        return _transfer_dict(conn, row)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def attempt_receiver(settings: Settings, order_no: str) -> dict[str, Any]:
    order = get_transfer(settings, order_no)
    if order["status"] in TERMINAL_ORDER_STATUSES:
        return order

    target_url = order.get("target_base_url") or settings.default_target_base_url
    if not target_url:
        raise ApiError(400, "missing_target", "没有可用的接收点地址")

    payload = _receiver_payload(settings, order)
    url = target_url.rstrip("/") + "/internal/transfers"
    try:
        with httpx.Client(timeout=settings.http_timeout, trust_env=False) as client:
            response = client.post(url, json=payload)
    except httpx.HTTPError as exc:
        message = f"调用接收点失败，回执状态未知：{type(exc).__name__}: {exc}"
        _record_origin_transport_error(settings, order_no, message)
        latest = get_transfer(settings, order_no)
        raise ApiError(
            503,
            "transfer_receipt_uncertain",
            "接收点调用失败或回执丢失；接收决定可能已经入账。卷号继续暂停开卷，请用原单号重试。",
            {"order": latest, "transport_error": str(exc)},
        ) from exc

    if response.status_code != 200:
        message = f"接收点返回 HTTP {response.status_code}: {response.text[:1000]}"
        _record_origin_transport_error(settings, order_no, message)
        latest = get_transfer(settings, order_no)
        # A 4xx could be a validation bug.  We deliberately do not restore the
        # papers unless the receiver durably records an explicit rejection.
        raise ApiError(
            502,
            "receiver_not_confirmed",
            "接收点未给出可入账的明确接收/拒收结果；卷号继续暂停开卷，可修复后凭原单号重试",
            {"order": latest, "receiver_status_code": response.status_code, "receiver_body": response.text[:2000]},
        )

    try:
        receiver_result = response.json()
    except ValueError as exc:
        message = f"接收点返回了非 JSON 回执：{response.text[:1000]}"
        _record_origin_transport_error(settings, order_no, message)
        latest = get_transfer(settings, order_no)
        raise ApiError(
            502,
            "bad_receiver_response",
            "接收点回执无法解析；为避免重复入账，请凭原单号重试",
            {"order": latest},
        ) from exc

    return finalize_origin_from_receiver(settings, order_no, receiver_result)


def create_and_dispatch(
    settings: Settings,
    order_no: str,
    raw_paper_nos: Any,
    target_base_url: str | None = None,
    target_site: str | None = None,
) -> dict[str, Any]:
    order, _created = create_transfer(settings, order_no, raw_paper_nos, target_base_url, target_site)
    if order["status"] in TERMINAL_ORDER_STATUSES:
        return order
    return attempt_receiver(settings, order_no)


def retry_transfer(settings: Settings, order_no: str) -> dict[str, Any]:
    # 404 if unknown.
    get_transfer(settings, order_no)
    return attempt_receiver(settings, order_no)


def recover_pending(settings: Settings) -> dict[str, Any]:
    conn = connect(settings.database_path)
    try:
        rows = conn.execute(
            "SELECT order_no FROM transfer_orders WHERE status = 'pending_receiver' ORDER BY created_at"
        ).fetchall()
        order_nos = [row["order_no"] for row in rows]
    finally:
        conn.close()

    recovered: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for order_no in order_nos:
        try:
            recovered.append(retry_transfer(settings, order_no))
        except ApiError as exc:
            errors.append({"order_no": order_no, "code": exc.code, "message": exc.message})
        except Exception as exc:  # background loop must stay alive
            errors.append({"order_no": order_no, "code": "recovery_error", "message": str(exc)})
    return {"pending_found": len(order_nos), "recovered": recovered, "errors": errors}


def start_background_recovery(settings: Settings) -> None:
    if not settings.recovery_enabled:
        return

    import threading

    def loop() -> None:
        # Give uvicorn/Docker networking a brief moment, then reconcile.
        time.sleep(0.5)
        while True:
            try:
                recover_pending(settings)
            except Exception:
                # The next interval will retry.  Avoid busy-looping.
                pass
            time.sleep(max(0.5, settings.recovery_interval_seconds))

    thread = threading.Thread(target=loop, name="transfer-recovery", daemon=True)
    thread.start()


# ---------------------------------------------------------------------------
# Receiver-side durable idempotent intake
# ---------------------------------------------------------------------------


def _normalize_receiver_batch(req: Any) -> tuple[list[str], dict[str, str]]:
    bodies: dict[str, str] = {}
    nos_from_objects: list[str] = []

    raw_papers = getattr(req, "papers", None)
    if isinstance(raw_papers, str):
        # A comma/newline batch can be accepted directly when paper_nos is absent.
        string_nos = normalize_paper_nos(raw_papers)
        explicit_nos = normalize_paper_nos(getattr(req, "paper_nos", None))
        return explicit_nos or string_nos, bodies
    if raw_papers:
        normalized_objects: list[dict[str, Any]] = []
        for item in raw_papers:
            if isinstance(item, dict):
                normalized_objects.append(item)
            elif isinstance(item, str):
                try:
                    parsed = json.loads(item)
                    if isinstance(parsed, dict):
                        normalized_objects.append(parsed)
                        continue
                except json.JSONDecodeError:
                    pass
                normalized_objects.append({"paper_no": item})
        for item in normalized_objects:
            no = normalize_paper_nos([item])
            if no:
                paper_no = no[0]
                nos_from_objects.append(paper_no)
                body = item.get("body") or item.get("content") or item.get("answer_body") or item.get("text")
                if body is not None:
                    bodies[paper_no] = str(body)

    explicit_nos = normalize_paper_nos(getattr(req, "paper_nos", None))
    if explicit_nos:
        paper_nos = explicit_nos
    else:
        paper_nos = normalize_paper_nos(nos_from_objects)

    # Preserve body map for all normalized IDs.
    for item in raw_papers or []:
        if isinstance(item, dict):
            nos = normalize_paper_nos([item])
            if nos:
                body = item.get("body") or item.get("content") or item.get("answer_body") or item.get("text")
                if body is not None:
                    bodies[nos[0]] = str(body)

    return paper_nos, bodies


def receive_transfer(settings: Settings, req: Any) -> dict[str, Any]:
    order_no = req.order_no.strip()
    source_site = (getattr(req, "source_site", None) or "unknown-origin").strip()
    paper_nos, bodies = _normalize_receiver_batch(req)
    request_snapshot = {
        "order_no": order_no,
        "source_site": source_site,
        "paper_nos": paper_nos,
        "papers": [{"paper_no": no, "has_body": no in bodies} for no in paper_nos],
    }

    conn = connect(settings.database_path)
    try:
        begin_immediate(conn)

        existing = conn.execute(
            "SELECT response_json FROM inbound_orders WHERE order_no = ?", (order_no,)
        ).fetchone()
        if existing is not None:
            conn.commit()
            response = _json_loads(existing["response_json"], {})
            response["idempotent_replay"] = True
            response["replayed"] = True
            return response

        unscored_before = int(
            conn.execute(
                "SELECT COUNT(*) AS n FROM papers WHERE status NOT IN ('scored', 'transferred_out')"
            ).fetchone()["n"]
        )
        limit = settings.max_unscored

        def reject(reason: str, message: str, conflicts: list[str] | None = None) -> dict[str, Any]:
            response = {
                "order_no": order_no,
                "accepted": False,
                "decision": "rejected",
                "status": "rejected",
                "reason": reason,
                "message": message,
                "paper_nos": paper_nos,
                "unscored_count_before": unscored_before,
                "unscored_count_after": unscored_before,
                "max_unscored": limit,
                "conflicts": conflicts or [],
                "idempotent_replay": False,
            }
            timestamp = now_iso()
            conn.execute(
                """
                INSERT INTO inbound_orders (
                    order_no, source_site, decision, reason,
                    request_json, response_json,
                    unscored_count_before, unscored_count_after,
                    quota_limit, created_at, decided_at
                ) VALUES (?, ?, 'rejected', ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    order_no,
                    source_site,
                    reason,
                    _json_dumps(request_snapshot),
                    _json_dumps(response),
                    unscored_before,
                    unscored_before,
                    limit,
                    timestamp,
                    timestamp,
                ),
            )
            for position, paper_no in enumerate(paper_nos):
                conn.execute(
                    "INSERT OR IGNORE INTO inbound_items(order_no, paper_no, position) VALUES (?, ?, ?)",
                    (order_no, paper_no, position),
                )
            conn.commit()
            return response

        if not paper_nos:
            return reject("empty_batch", "接收批次没有有效卷号")

        placeholders = ",".join("?" for _ in paper_nos)
        existing_paper_rows = conn.execute(
            f"SELECT paper_no FROM papers WHERE paper_no IN ({placeholders})", paper_nos
        ).fetchall()
        conflicts = [row["paper_no"] for row in existing_paper_rows]
        if conflicts:
            return reject("paper_already_exists", "接收点已有同号答卷，为避免覆盖而整批拒收", conflicts)

        if limit is not None and unscored_before + len(paper_nos) > limit:
            return reject(
                "quota_exceeded",
                (
                    f"接收点尚未交分卷数为 {unscored_before}，限额 {limit}；"
                    f"本批 {len(paper_nos)} 卷无法整批收下"
                ),
            )

        timestamp = now_iso()
        unscored_after = unscored_before + len(paper_nos)
        response = {
            "order_no": order_no,
            "accepted": True,
            "decision": "accepted",
            "status": "accepted",
            "reason": None,
            "message": f"整批 {len(paper_nos)} 卷已入账，可以开卷",
            "paper_nos": paper_nos,
            "unscored_count_before": unscored_before,
            "unscored_count_after": unscored_after,
            "max_unscored": limit,
            "remaining_quota": None if limit is None else limit - unscored_after,
            "idempotent_replay": False,
        }
        conn.execute(
            """
            INSERT INTO inbound_orders (
                order_no, source_site, decision, reason,
                request_json, response_json,
                unscored_count_before, unscored_count_after,
                quota_limit, created_at, decided_at
            ) VALUES (?, ?, 'accepted', NULL, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                order_no,
                source_site,
                _json_dumps(request_snapshot),
                _json_dumps(response),
                unscored_before,
                unscored_after,
                limit,
                timestamp,
                timestamp,
            ),
        )

        for position, paper_no in enumerate(paper_nos):
            body = bodies.get(paper_no)
            if not body:
                body = (
                    f"接收点入账卷 {paper_no}\n"
                    "HTTP 交接未包含正文；卷号和调卷记录已入账。"
                )
            conn.execute(
                """
                INSERT INTO papers (
                    paper_no, status, body, score, score_comment, grader,
                    source_site, transfer_order_no, opened_at, scored_at,
                    transferred_at, created_at, updated_at
                ) VALUES (?, 'unopened', ?, NULL, NULL, NULL, ?, ?, NULL, NULL, NULL, ?, ?)
                """,
                (paper_no, body, source_site, order_no, timestamp, timestamp),
            )
            conn.execute(
                "INSERT INTO inbound_items(order_no, paper_no, position) VALUES (?, ?, ?)",
                (order_no, paper_no, position),
            )

        conn.commit()
        return response
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_inbound_order(settings: Settings, order_no: str) -> dict[str, Any]:
    conn = connect(settings.database_path)
    try:
        row = conn.execute("SELECT * FROM inbound_orders WHERE order_no = ?", (order_no,)).fetchone()
        if row is None:
            raise ApiError(404, "inbound_order_not_found", f"接收点没有单号 {order_no} 的记录")
        response = _json_loads(row["response_json"], {})
        response["stored_decision"] = row["decision"]
        response["source_site"] = row["source_site"]
        response["created_at"] = row["created_at"]
        response["decided_at"] = row["decided_at"]
        return response
    finally:
        conn.close()
