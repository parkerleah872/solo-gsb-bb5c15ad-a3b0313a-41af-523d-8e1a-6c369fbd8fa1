"""脱敏核心：按原始 Unicode 码位一次性解析重叠片段并生成稳定占位符。

规则（确定性、无随机成分）：
* ``start`` / ``end`` 均为 Unicode 码位偏移，左闭右开，依据 Python 字符串索引；
* 片段按 (优先级降序, start 升序, 长度降序, 人工优先, 提交顺序) 排序；
* 排序靠前的片段先落位，与之重叠的后续片段被丢弃，``drop_reason`` 标为
  ``"overlaps_higher_priority"``（同优先级先到先得）；
* 同一文档内 (类别, 原文) 相同的片段复用同一占位符，序号按首次出现顺序分配。

片段冲突溯源（均为提交内确定、持久化后不再漂移）：
* ``fragment_id``：按**合并后的原始顺序**（人工 ``spans`` 在前、自动命中按规则
  及出现顺序接在其后）从 1 起分配的响应内唯一编号；完全重复的候选也各有独立 ID；
* ``decision_rank``：按上述优先级排序后、依次落位时的决策次序（从 1 起）；
* ``blocked_by_fragment_id``：保留片段恒为 ``None``；丢弃片段指向**当时已落位
  且与之相交的首个片段**（按决策先后，即 ``decision_rank`` 最小者）的
  ``fragment_id``——即真正导致其被拒绝的那一个，而非事后任取一个相交项。

``source`` 区分片段来源：``"manual"`` 为客户端手传片段，``"auto"`` 为策略规则
在原文上自动命中的片段；自动片段通过 ``rule_id`` 标注来源规则。优先级、起止
位置和长度完全相同时，人工片段优先于自动片段。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

CATEGORY_PATTERN = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
# 占位符形如 {{PERSON:01}}，类别仅允许大写字母/数字/下划线，杜绝注入到占位符语法
PLACEHOLDER_PATTERN = re.compile(r"\{\{([A-Z][A-Z0-9_]{0,31}):(\d+)\}\}")

MANUAL = "manual"
AUTO = "auto"

# 排序决胜：0 = 人工片段，1 = 自动片段。人工片段在同等条件下优先落位。
_SOURCE_RANK = {MANUAL: 0, AUTO: 1}


@dataclass(frozen=True)
class InputSpan:
    start: int
    end: int
    priority: int
    category: str
    ordinal: int  # 客户端提交顺序，用于稳定决胜
    source: str = MANUAL
    rule_id: str | None = None


@dataclass
class ResolvedSpan:
    start: int
    end: int
    priority: int
    category: str
    value: str
    placeholder: str
    kept: bool
    drop_reason: str | None = None
    source: str = MANUAL
    rule_id: str | None = None
    fragment_id: int = 0  # 合并后原始顺序的响应内唯一编号（从 1 起）
    decision_rank: int = 0  # 优先级排序后的决策次序（从 1 起）
    blocked_by_fragment_id: int | None = None  # 实际导致拒绝的已落位片段


@dataclass
class RedactionResult:
    redacted_text: str
    spans: list[ResolvedSpan] = field(default_factory=list)


def normalize_category(category: str) -> str:
    return category.strip().upper()


def validate_category(category: str) -> None:
    if not CATEGORY_PATTERN.match(category):
        raise ValueError(
            f"非法类别 {category!r}：需匹配 [A-Z][A-Z0-9_]{{0,31}}（输入会去除首尾空白并转大写）"
        )


def resolve_spans(text: str, inputs: list[InputSpan]) -> list[ResolvedSpan]:
    """校验、排序并一次性消解全部片段重叠。

    溯源编号在此一并确定：``fragment_id`` 按合并后的原始顺序（即 ``ordinal``
    递增）从 1 起编号；``decision_rank`` 按优先级排序后的决策次序从 1 起编号；
    丢弃片段的 ``blocked_by_fragment_id`` 指向落位当时**首个**相交的已落位片段
    （沿决策先后遍历，取最先相交者）。
    """
    ordered = sorted(
        inputs,
        key=lambda s: (
            -s.priority,
            s.start,
            -(s.end - s.start),
            _SOURCE_RANK.get(s.source, 1),
            s.ordinal,
        ),
    )

    # ordinal 在一次提交内唯一且按合并原始顺序（人工在前、自动追加其后）分配；
    # 按 ordinal 排名（从 1 起）即 fragment_id，不依赖其是否连续
    fragment_ids = {
        span.ordinal: rank
        for rank, span in enumerate(sorted(inputs, key=lambda s: s.ordinal), 1)
    }

    # 已落位片段，按决策先后追加；遍历它即等价于取 decision_rank 最小者
    placed: list[tuple[int, int, int]] = []  # (start, end, fragment_id)
    resolved: dict[int, ResolvedSpan] = {}

    for decision_rank, span in enumerate(ordered, 1):
        value = text[span.start:span.end]
        fragment_id = fragment_ids[span.ordinal]
        blocker = next(
            (fid for s, e, fid in placed if span.start < e and s < span.end),
            None,
        )
        if blocker is not None:
            resolved[span.ordinal] = ResolvedSpan(
                start=span.start,
                end=span.end,
                priority=span.priority,
                category=span.category,
                value=value,
                placeholder="",
                kept=False,
                drop_reason="overlaps_higher_priority",
                source=span.source,
                rule_id=span.rule_id,
                fragment_id=fragment_id,
                decision_rank=decision_rank,
                blocked_by_fragment_id=blocker,
            )
            continue
        placed.append((span.start, span.end, fragment_id))
        resolved[span.ordinal] = ResolvedSpan(
            start=span.start,
            end=span.end,
            priority=span.priority,
            category=span.category,
            value=value,
            placeholder="",
            kept=True,
            source=span.source,
            rule_id=span.rule_id,
            fragment_id=fragment_id,
            decision_rank=decision_rank,
            blocked_by_fragment_id=None,
        )

    return [resolved[i] for i in sorted(resolved)]


def assign_placeholders(spans: list[ResolvedSpan]) -> None:
    """为保留片段分配稳定占位符：(类别, 原文) 相同则复用。"""
    mapping: dict[tuple[str, str], str] = {}
    counters: dict[str, int] = {}
    for span in spans:
        if not span.kept:
            continue
        key = (span.category, span.value)
        if key not in mapping:
            counters[span.category] = counters.get(span.category, 0) + 1
            seq = counters[span.category]
            width = max(2, len(str(counters[span.category])))  # 起步两位，后续自动加宽
            mapping[key] = "{{" + f"{span.category}:{seq:0{width}d}" + "}}"
        span.placeholder = mapping[key]


def render(text: str, spans: list[ResolvedSpan]) -> str:
    """自后向前替换，保证前面片段的偏移不失效。"""
    result = text
    for span in sorted(spans, key=lambda s: s.start, reverse=True):
        if span.kept:
            result = result[: span.start] + span.placeholder + result[span.end :]
    return result


def redact(text: str, inputs: list[InputSpan]) -> RedactionResult:
    spans = resolve_spans(text, inputs)
    assign_placeholders(spans)
    return RedactionResult(redacted_text=render(text, spans), spans=spans)
