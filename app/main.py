"""FastAPI entry point for one independently restartable grading point."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any

from fastapi import Body, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from . import service
from .config import Settings
from .db import connect, init_db
from .errors import ApiError
from .schemas import (
    OpenRequest,
    ReceiveTransferRequest,
    ScoreRequest,
    SupplementRequest,
    TransferRequest,
)

settings = Settings.from_env()
init_db(settings)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    # Reconcile orders left pending by a lost HTTP response / process restart.
    service.start_background_recovery(settings)
    yield


app = FastAPI(
    title=f"联考阅卷服务 · {settings.site_id}",
    version="1.1.0",
    description=(
        "两个阅卷点各自运行本服务并使用独立 SQLite。\n\n"
        "1. 在 **Papers** 中调用开卷接口取得匿名答卷正文和 `version`；\n"
        "2. 调用交分接口提交分数；补过页的卷必须带与当前版本匹配的 `version`；\n"
        "3. 组长可用 **Supplements** 按卷号 + 所见版本提交补齐后的全文，"
        "原分数撤下、卷留本点重新待评，旧卷面和当时分数可在补页历史查询；\n"
        "4. 组长用原单号和一批卷号发起调卷；\n"
        "5. 已开卷/已交分的卷留在原点，未开卷的候选卷先暂停开卷，"
        "接收点整批收下后才在原点标记转出；明确拒收后恢复开卷资格；\n"
        "6. 回执丢失或服务重启后，用同一单号重试，接收点按单号幂等返回原结果。"
    ),
    lifespan=lifespan,
)


@app.exception_handler(ApiError)
async def api_error_handler(_request: Request, exc: ApiError) -> JSONResponse:
    content: dict[str, Any] = {
        "ok": False,
        "code": exc.code,
        "error": {
            "code": exc.code,
            "message": exc.message,
            "details": exc.details,
        },
        # FastAPI-style convenience field.
        "detail": exc.message,
    }
    # Surface scalar/structured details at top level too, so callers can read
    # e.g. body["current_version"] without digging through error.details.
    for key, value in (exc.details or {}).items():
        if key not in content:
            content[key] = value
    return JSONResponse(status_code=exc.status_code, content=content)


@app.get("/", tags=["system"], summary="服务说明")
def root() -> dict[str, Any]:
    return {
        "service": "joint-exam-grading",
        "site_id": settings.site_id,
        "docs": "/docs",
        "health": "/health",
        "papers": "/papers",
        "transfers": "/transfers",
        "capacity": "/capacity",
        "default_target_base_url": settings.default_target_base_url,
    }


@app.get("/health", tags=["system"], summary="健康检查和当前限额")
def health() -> dict[str, Any]:
    conn = connect(settings.database_path)
    try:
        conn.execute("SELECT 1").fetchone()
    finally:
        conn.close()
    return {"status": "ok", "site_id": settings.site_id, **service.capacity(settings)}


@app.get("/capacity", tags=["system"], summary="接收点未交分卷数与限额")
def get_capacity() -> dict[str, Any]:
    return service.capacity(settings)


@app.get("/papers", tags=["papers"], summary="列出内置/已交接答卷")
def get_papers(
    status: str | None = Query(default=None, description="按状态过滤"),
    include_body: bool = Query(default=False, description="是否同时返回正文"),
    limit: int = Query(default=500, ge=1, le=1000),
) -> dict[str, Any]:
    items = service.list_papers(settings, status, include_body, limit)
    return {"site_id": settings.site_id, "count": len(items), "papers": items}


@app.get("/papers/{paper_no}", tags=["papers"], summary="查询单卷状态")
def get_paper(
    paper_no: str,
    include_body: bool = Query(default=False, description="已开卷后可设 true 查看正文"),
    version: str | None = Query(
        default=None, description="传入历史版本号时返回该版正文及当时分数"
    ),
    include_history: bool = Query(default=False, description="true 时附带补页历史和版本快照"),
    history: bool = Query(default=False, description="include_history 的短别名"),
) -> dict[str, Any]:
    if version is not None and str(version).strip() != "":
        return service.get_paper_version(settings, paper_no, version)
    result = service.get_paper(settings, paper_no, include_body=include_body)
    if include_history or history:
        history_data = service.get_paper_history(settings, paper_no)
        result["history"] = history_data["history"]
        result["supplements"] = history_data["supplements"]
        result["versions"] = history_data["versions"]
        result["supplement_count"] = history_data["supplement_count"]
    return result


@app.get("/papers/{paper_no}/body", tags=["papers"], include_in_schema=False)
def get_paper_body(paper_no: str) -> dict[str, Any]:
    return service.get_paper(settings, paper_no, include_body=True)


@app.post(
    "/papers/{paper_no}/open",
    tags=["grading"],
    summary="开卷并取得匿名答卷正文",
)
@app.post("/open/{paper_no}", tags=["grading"], include_in_schema=False)
def open_paper_by_path(paper_no: str) -> dict[str, Any]:
    return service.open_paper(settings, paper_no)


@app.post("/open", tags=["grading"], summary="用请求体中的卷号开卷")
def open_paper_by_body(payload: OpenRequest) -> dict[str, Any]:
    return service.open_paper(settings, payload.paper_no)


def _submit_score(
    paper_no: str | None,
    payload: ScoreRequest | None,
    score_query: float | None,
    comment_query: str | None,
    grader_query: str | None,
    version_query: Any = None,
) -> dict[str, Any]:
    resolved_no = paper_no or (payload.paper_no if payload else None)
    if not resolved_no:
        raise ApiError(422, "missing_paper_no", "必须提供卷号")
    score = payload.score if payload is not None else score_query
    if score is None:
        raise ApiError(422, "missing_score", "必须提供分数 score")
    comment = payload.comment if payload is not None and payload.comment is not None else comment_query
    grader = payload.grader if payload is not None and payload.grader is not None else grader_query
    claimed_version = (
        payload.version
        if payload is not None and getattr(payload, "version", None) is not None
        else version_query
    )
    return service.submit_score(
        settings, resolved_no, score, comment, grader, claimed_version=claimed_version
    )


@app.post(
    "/papers/{paper_no}/score",
    tags=["grading"],
    summary="提交分数（补页后的卷必须带当前 version；支持 JSON body 或 ?score=&version=）",
)
@app.post("/papers/{paper_no}/scores", tags=["grading"], include_in_schema=False)
@app.post("/scores/{paper_no}", tags=["grading"], include_in_schema=False)
def score_paper_by_path(
    paper_no: str,
    payload: ScoreRequest | None = Body(default=None),
    score: float | None = Query(default=None),
    comment: str | None = Query(default=None),
    grader: str | None = Query(default=None),
    version: str | None = Query(default=None, description="阅卷时所见版本；补过页的卷必填"),
    seen_version: str | None = Query(default=None, description="version 的别名"),
    paper_version: str | None = Query(default=None, description="version 的别名"),
) -> dict[str, Any]:
    version_query = version or seen_version or paper_version
    return _submit_score(paper_no, payload, score, comment, grader, version_query)


@app.post("/score", tags=["grading"], summary="用请求体提交分数")
@app.post("/scores", tags=["grading"], summary="交分（别名）")
def score_paper_by_body(payload: ScoreRequest) -> dict[str, Any]:
    return _submit_score(None, payload, None, None, None, None)


# ---------------------------------------------------------------------------
# Page supplements (组长补页) and version/history queries
# ---------------------------------------------------------------------------


def _submit_supplement(
    paper_no: str | None,
    payload: SupplementRequest | None,
    version_query: Any = None,
    body_query: str | None = None,
    full_text_query: str | None = None,
    operator_query: str | None = None,
    note_query: str | None = None,
) -> dict[str, Any]:
    resolved_no = (paper_no or (payload.paper_no if payload else None) or "").strip()
    if not resolved_no:
        raise ApiError(422, "missing_paper_no", "必须提供卷号 paper_no")

    if payload is not None and payload.seen_version is not None:
        seen_version: Any = payload.seen_version
    else:
        seen_version = version_query

    if payload is not None and payload.body is not None:
        full_text: Any = payload.body
    elif body_query is not None:
        full_text = body_query
    else:
        full_text = full_text_query

    added_pages = payload.added_pages if payload is not None else None
    operator = (
        payload.operator
        if payload is not None and payload.operator
        else operator_query
    )
    note = payload.note if payload is not None and payload.note else note_query

    return service.supplement_paper(
        settings,
        resolved_no,
        seen_version_raw=seen_version,
        full_text=full_text,
        operator=operator,
        note=note,
        added_pages=added_pages,
    )


@app.post(
    "/papers/{paper_no}/supplement",
    tags=["supplements"],
    summary="组长补页：按卷号和所见版本提交补齐后的全文（旧分撤下、重新待评）",
)
@app.put("/papers/{paper_no}/supplement", tags=["supplements"], include_in_schema=False)
@app.patch("/papers/{paper_no}/supplement", tags=["supplements"], include_in_schema=False)
@app.post("/papers/{paper_no}/supplements", tags=["supplements"], include_in_schema=False)
@app.put("/papers/{paper_no}/supplements", tags=["supplements"], include_in_schema=False)
@app.post("/papers/{paper_no}/supplement-pages", tags=["supplements"], include_in_schema=False)
@app.post("/papers/{paper_no}/supplement_pages", tags=["supplements"], include_in_schema=False)
@app.post("/papers/{paper_no}/add-pages", tags=["supplements"], include_in_schema=False)
@app.post("/papers/{paper_no}/pages", tags=["supplements"], include_in_schema=False)
@app.post("/papers/{paper_no}/complete", tags=["supplements"], include_in_schema=False)
@app.post("/papers/{paper_no}/rescan", tags=["supplements"], include_in_schema=False)
@app.patch("/papers/{paper_no}", tags=["supplements"], include_in_schema=False)
@app.put("/papers/{paper_no}", tags=["supplements"], include_in_schema=False)
@app.post("/papers/{paper_no}", tags=["supplements"], include_in_schema=False)
def supplement_paper_by_path(
    paper_no: str,
    payload: SupplementRequest | None = Body(default=None),
    version: str | None = Query(default=None, description="组长所见版本，等价 seen_version"),
    seen_version: str | None = Query(default=None),
    base_version: str | None = Query(default=None),
    body: str | None = Query(default=None, description="补齐后的全文"),
    full_text: str | None = Query(default=None, description="补齐后的全文（别名）"),
    text: str | None = Query(default=None, description="补齐后的全文（别名）"),
    operator: str | None = Query(default=None),
    leader: str | None = Query(default=None),
    note: str | None = Query(default=None),
) -> dict[str, Any]:
    version_query = version or seen_version or base_version
    body_query = body if body is not None else full_text
    if body_query is None:
        body_query = text
    operator_query = operator or leader
    return _submit_supplement(
        paper_no, payload, version_query, body_query, None, operator_query, note
    )


@app.post(
    "/supplement",
    tags=["supplements"],
    summary="用请求体按卷号+所见版本补页",
)
@app.post("/supplements", tags=["supplements"], summary="补页（别名）")
@app.post("/supplement-pages", tags=["supplements"], include_in_schema=False)
@app.post("/leader/supplement", tags=["supplements"], include_in_schema=False)
@app.post("/leader/papers/{paper_no}/supplement", tags=["supplements"], include_in_schema=False)
@app.post("/papers/supplement", tags=["supplements"], include_in_schema=False)
def supplement_paper_by_body(
    payload: SupplementRequest, paper_no: str | None = None
) -> dict[str, Any]:
    # On the /leader/papers/{paper_no}/supplement alias paper_no comes from
    # the path; _submit_supplement lets the path win over the body field.
    return _submit_supplement(paper_no, payload)


@app.get(
    "/papers/{paper_no}/history",
    tags=["supplements"],
    summary="查补页历史：每版旧卷面和当时被撤下的分数",
)
@app.get("/papers/{paper_no}/supplements", tags=["supplements"], summary="查卷的补页记录")
@app.get("/papers/{paper_no}/supplement", tags=["supplements"], include_in_schema=False)
@app.get("/papers/{paper_no}/revisions", tags=["supplements"], summary="查卷的历史版本（别名）")
@app.get("/papers/{paper_no}/versions", tags=["supplements"], summary="列出全部卷面版本")
@app.get("/papers/{paper_no}/supplement/history", tags=["supplements"], include_in_schema=False)
def get_paper_history(
    paper_no: str,
    version: str | None = Query(default=None, description="传入版本号则只返回该版"),
) -> dict[str, Any]:
    if version is not None and str(version).strip() != "":
        return service.get_paper_version(settings, paper_no, version)
    return service.get_paper_history(settings, paper_no)


@app.get("/papers/{paper_no}/scores", tags=["supplements"], summary="查该卷历次补页撤下的分数")
@app.get("/papers/{paper_no}/score-history", tags=["supplements"], include_in_schema=False)
@app.get("/papers/{paper_no}/score_history", tags=["supplements"], include_in_schema=False)
def get_paper_score_history(paper_no: str) -> dict[str, Any]:
    history = service.get_paper_history(settings, paper_no)
    score_records = [
        {
            "version": item["seen_version"],
            "score": item["old_score"],
            "score_comment": item["old_score_comment"],
            "grader": item["old_grader"],
            "scored_at": item["old_scored_at"],
            "withdrawn_at": item["created_at"],
            "supplement_id": item["id"],
        }
        for item in history["supplements"]
    ]
    history["score_records"] = score_records
    history["scores"] = score_records
    return history


@app.get(
    "/papers/{paper_no}/versions/{version}",
    tags=["supplements"],
    summary="按版本号查某一版卷面正文和当时分数",
)
@app.get("/papers/{paper_no}/history/{version}", tags=["supplements"], include_in_schema=False)
@app.get("/papers/{paper_no}/revisions/{version}", tags=["supplements"], include_in_schema=False)
def get_paper_version(paper_no: str, version: str) -> dict[str, Any]:
    return service.get_paper_version(settings, paper_no, version)


@app.get("/versions/{paper_no}/{version}", tags=["supplements"], include_in_schema=False)
def get_paper_version_alt(paper_no: str, version: str) -> dict[str, Any]:
    return service.get_paper_version(settings, paper_no, version)


@app.get("/supplements", tags=["supplements"], include_in_schema=False)
def list_supplements_by_query(
    paper_no: str = Query(..., description="卷号"),
) -> dict[str, Any]:
    return service.get_paper_history(settings, paper_no)


@app.post(
    "/transfers",
    tags=["transfers"],
    summary="凭单号和一批卷号发起/重试调卷",
)
@app.post("/transfer", tags=["transfers"], include_in_schema=False)
@app.post("/api/transfers", tags=["transfers"], include_in_schema=False)
@app.post("/api/transfer", tags=["transfers"], include_in_schema=False)
def create_transfer(payload: TransferRequest) -> dict[str, Any]:
    return service.create_and_dispatch(
        settings,
        order_no=payload.order_no,
        raw_paper_nos=payload.paper_nos,
        target_base_url=payload.target_base_url,
        target_site=payload.target_site,
    )


@app.get("/transfers", tags=["transfers"], summary="列出原点调卷单")
def list_transfers(status: str | None = Query(default=None)) -> dict[str, Any]:
    items = service.list_transfers(settings, status)
    return {"site_id": settings.site_id, "count": len(items), "transfers": items}


@app.post("/transfers/recover-pending", tags=["transfers"], summary="立即恢复所有回执未知的调卷单")
@app.post("/transfers/recover", tags=["transfers"], include_in_schema=False)
def recover_pending() -> dict[str, Any]:
    return service.recover_pending(settings)


@app.post(
    "/transfers/{order_no}/retry",
    tags=["transfers"],
    summary="用原单号重试未完成的调卷",
)
@app.post("/transfer/{order_no}/retry", tags=["transfers"], include_in_schema=False)
def retry_transfer(order_no: str) -> dict[str, Any]:
    return service.retry_transfer(settings, order_no)


@app.get(
    "/transfers/{order_no}",
    tags=["transfers"],
    summary="查询原点调卷单和转走/留下卷号",
)
@app.get("/transfer/{order_no}", tags=["transfers"], include_in_schema=False)
def get_transfer(order_no: str) -> dict[str, Any]:
    return service.get_transfer(settings, order_no)


@app.post(
    "/internal/transfers",
    tags=["receiver-internal"],
    summary="接收点受理 HTTP 交接（按单号幂等）",
)
@app.post("/internal/transfer", tags=["receiver-internal"], include_in_schema=False)
@app.post("/receive/transfers", tags=["receiver-internal"], include_in_schema=False)
def receive_transfer(payload: ReceiveTransferRequest) -> dict[str, Any]:
    return service.receive_transfer(settings, payload)


@app.get(
    "/internal/transfers/{order_no}",
    tags=["receiver-internal"],
    summary="按原单号查回接收点的首次接收/拒收结果",
)
def get_inbound_transfer(order_no: str) -> dict[str, Any]:
    return service.get_inbound_order(settings, order_no)
