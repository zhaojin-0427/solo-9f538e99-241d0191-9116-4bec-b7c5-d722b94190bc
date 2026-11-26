"""Request schemas.

The aliases intentionally cover the common English and Chinese field names a
caller/harness may use, while the documented fields remain short and clear.
"""

from __future__ import annotations

from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class OpenRequest(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        json_schema_extra={"example": {"paper_no": "A-001"}},
    )

    paper_no: str = Field(
        ...,
        validation_alias=AliasChoices(
            "paper_no", "paperNo", "paper_id", "paperId", "paper_number",
            "paperNumber", "id", "卷号",
        ),
    )


class ScoreRequest(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        json_schema_extra={
            "example": {"score": 88, "comment": "要点完整，表达通顺", "grader": "teacher-1"}
        },
    )

    paper_no: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "paper_no", "paperNo", "paper_id", "paperId", "paper_number",
            "paperNumber", "id", "卷号",
        ),
    )
    score: float = Field(
        ...,
        validation_alias=AliasChoices("score", "points", "point", "value", "grade", "分数"),
    )
    comment: str | None = Field(
        default=None,
        validation_alias=AliasChoices("comment", "score_comment", "note", "评语"),
    )
    grader: str | None = Field(
        default=None,
        validation_alias=AliasChoices("grader", "grader_id", "teacher", "阅卷员"),
    )
    # Optimistic version the grader saw.  Required (and must equal the current
    # version) once a paper has been supplemented; optional otherwise.
    version: Any | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "version",
            "paper_version",
            "current_version",
            "scoring_version",
            "score_version",
            "seen_version",
            "base_version",
            "expected_version",
            "revision",
            "rev",
            "版本",
            "当前版本",
            "所见版本",
        ),
    )


class SupplementRequest(BaseModel):
    """Leader-only page completion ("补页") request.

    The client reports the version of the paper image it was looking at
    (optimistic concurrency) and submits the complete post-scan full text.
    Field aliases intentionally cover the common English/Chinese names.
    """

    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        json_schema_extra={
            "example": {
                "paper_no": "B-001",
                "seen_version": 1,
                "body": "补齐漏扫页之后的完整正文……",
                "operator": "leader-li",
            }
        },
    )

    paper_no: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "paper_no", "paperNo", "paper_id", "paperId", "paper_number",
            "paperNumber", "id", "卷号",
        ),
    )
    seen_version: Any | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "seen_version",
            "seenVersion",
            "seen_revision",
            "base_version",
            "baseVersion",
            "based_on_version",
            "expected_version",
            "expectedVersion",
            "current_version",
            "paper_version",
            "from_version",
            "version",
            "revision",
            "rev",
            "所见版本",
            "版本",
        ),
    )
    body: Any | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "body",
            "full_body",
            "fullBody",
            "full_text",
            "fullText",
            "full_content",
            "fullContent",
            "new_full_text",
            "complete_text",
            "completed_body",
            "completedBody",
            "completed_text",
            "complete_body",
            "entire_body",
            "whole_text",
            "updated_body",
            "new_body",
            "newBody",
            "answer_body",
            "answerBody",
            "content",
            "text",
            "全文",
            "正文",
            "补齐后全文",
        ),
    )
    # Hedge for callers that send only the newly found pages instead of the
    # whole completed text.  When body/full_text is present it wins.
    added_pages: Any | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "added_pages",
            "addedPages",
            "new_pages",
            "pages",
            "missing_pages",
            "supplement_pages",
            "补页内容",
            "漏扫页",
        ),
    )
    operator: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "operator", "leader", "submitted_by", "submittedBy", "by", "user", "组长", "操作人",
        ),
    )
    note: str | None = Field(
        default=None,
        validation_alias=AliasChoices("note", "reason", "comment", "remark", "备注", "说明"),
    )


class TransferRequest(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        extra="ignore",
        json_schema_extra={
            "example": {
                "order_no": "TR-DEMO-001",
                "paper_nos": ["A-001", "A-002", "A-003"],
                "target_base_url": "http://grading-b:8000",
                "target_site": "point-b",
            }
        },
    )

    order_no: str = Field(
        ...,
        validation_alias=AliasChoices(
            "order_no", "orderNo", "order_id", "orderId", "transfer_no",
            "transferNo", "batch_no", "batchNo", "单号", "调卷单号",
        ),
    )
    paper_nos: Any = Field(
        ...,
        validation_alias=AliasChoices(
            "paper_nos", "paperNos", "paper_ids", "paperIds",
            "paper_numbers", "paperNumbers", "卷号", "papers",
        ),
    )
    target_base_url: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "target_base_url", "targetBaseUrl", "target_url", "targetUrl",
            "receiver_url", "receiverUrl", "destination_url", "destinationUrl",
        ),
    )
    target_site: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "target_site", "targetSite", "destination", "receiver_site", "接收点",
        ),
    )


class ReceiverPaper(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    paper_no: str = Field(
        ...,
        validation_alias=AliasChoices(
            "paper_no", "paperNo", "paper_id", "paperId", "paper_number", "卷号",
        ),
    )
    body: str | None = Field(
        default=None,
        validation_alias=AliasChoices("body", "content", "answer_body", "text", "正文"),
    )


class ReceiveTransferRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    order_no: str = Field(
        ...,
        validation_alias=AliasChoices(
            "order_no", "orderNo", "order_id", "orderId", "transfer_no", "单号",
        ),
    )
    source_site: str | None = Field(
        default=None,
        validation_alias=AliasChoices("source_site", "sourceSite", "origin_site", "来源点"),
    )
    paper_nos: Any | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "paper_nos", "paperNos", "paper_ids", "paperIds", "paper_numbers", "卷号",
        ),
    )
    papers: Any | None = Field(
        default=None,
        validation_alias=AliasChoices("papers", "items", "答卷"),
    )
