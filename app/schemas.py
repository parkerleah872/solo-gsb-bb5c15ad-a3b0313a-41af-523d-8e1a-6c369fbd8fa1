"""请求 / 响应数据模型。"""
from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator

MAX_TEXT_LEN = 2_000_000
MAX_SPANS = 5_000
MAX_TTL_REQ = 7 * 86400
MAX_PATTERN_LEN = 2_000
MAX_POLICY_RULES = 200


class SpanIn(BaseModel):
    start: int = Field(..., ge=0, description="起始 Unicode 码位偏移（含）")
    end: int = Field(..., ge=0, description="结束 Unicode 码位偏移（不含）")
    priority: int = Field(0, ge=0, le=10_000, description="数值越大优先级越高")
    category: str = Field(..., max_length=32)

    @field_validator("category")
    @classmethod
    def _category(cls, v: str) -> str:
        return v.strip().upper()


class SubmitRequest(BaseModel):
    text: str = Field(..., min_length=1, max_length=MAX_TEXT_LEN)
    # 提供 policy_id 时允许只靠自动规则、不手传片段
    spans: list[SpanIn] = Field(default_factory=list, max_length=MAX_SPANS)
    ttl: int | None = Field(None, ge=1, le=MAX_TTL_REQ, description="映射有效期（秒）")
    policy_id: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="自动脱敏策略 ID；命中规则生成的自动片段与 spans 合并消解",
    )

    @field_validator("policy_id")
    @classmethod
    def _policy_id(cls, v: str | None) -> str | None:
        return v.strip() or None if v is not None else None


class FragmentOut(BaseModel):
    fragment_id: int = Field(
        ...,
        ge=1,
        description="响应内唯一编号，按合并后的原始顺序（人工 spans 在前、自动候选接其后）从 1 起分配；持久化后不漂移",
    )
    decision_rank: int = Field(
        ...,
        ge=1,
        description="按优先级排序后依次落位的决策次序（从 1 起）",
    )
    blocked_by_fragment_id: int | None = Field(
        None,
        description="保留片段为 null；丢弃片段指向当时已落位且实际导致拒绝的首个片段的 fragment_id",
    )
    placeholder: str
    category: str
    start: int
    end: int
    priority: int
    kept: bool
    drop_reason: str | None = None
    source: Literal["manual", "auto"] = Field(
        "manual", description="manual=客户端手传片段；auto=策略规则自动生成"
    )
    rule_id: str | None = Field(
        None, description="source=auto 时标注生成该片段的规则；manual 时为 null"
    )


class SubmitResponse(BaseModel):
    document_id: str
    redacted_text: str
    fragments: list[FragmentOut]
    restore_token: str = Field(..., description="一次性恢复令牌，仅本次响应返回明文，请妥善保存")
    expires_at: datetime
    token_expires_at: datetime


class DocumentResponse(BaseModel):
    document_id: str
    redacted_text: str
    fragments: list[FragmentOut]
    expires_at: datetime


class RestoreRequest(BaseModel):
    token: str = Field(..., min_length=1, max_length=128)
    placeholders: list[str] = Field(..., min_length=1, max_length=100)


class RestoredOccurrence(BaseModel):
    start: int
    end: int
    priority: int
    source: Literal["manual", "auto"] = "manual"
    rule_id: str | None = None


class RestoredItem(BaseModel):
    placeholder: str
    category: str
    value: str
    occurrences: list[RestoredOccurrence]


class RestoreResponse(BaseModel):
    restored: list[RestoredItem]
    restore_token: str = Field(..., description="轮换后的新一次性令牌")
    token_expires_at: datetime


# ---------- 自动脱敏策略 ----------


class PolicyRuleIn(BaseModel):
    rule_id: str = Field(
        ...,
        min_length=1,
        max_length=64,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$",
        description="策略内唯一的规则标识，策略创建后不可变",
    )
    category: str = Field(..., max_length=32)
    pattern: str = Field(
        ...,
        min_length=1,
        max_length=MAX_PATTERN_LEN,
        description="Python 正则（Unicode 语义，作用于码位原文）；禁止产生空匹配",
    )
    priority: int = Field(0, ge=0, le=10_000, description="数值越大优先级越高")

    @field_validator("category")
    @classmethod
    def _category(cls, v: str) -> str:
        return v.strip().upper()


class PolicyCreateRequest(BaseModel):
    name: str = Field("", max_length=128, description="便于辨识的可选名称")
    rules: list[PolicyRuleIn] = Field(
        ..., min_length=1, max_length=MAX_POLICY_RULES
    )


class PolicyRuleOut(BaseModel):
    rule_id: str
    category: str
    pattern: str
    priority: int


class PolicyResponse(BaseModel):
    policy_id: str
    name: str
    created_at: datetime
    rules: list[PolicyRuleOut]
