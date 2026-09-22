"""脱敏核心算法的纯单元测试。"""
from app.redaction import AUTO, MANUAL, InputSpan, redact, resolve_spans, assign_placeholders


def _span(start, end, priority, category, ordinal, source=MANUAL, rule_id=None):
    return InputSpan(start, end, priority, category, ordinal, source=source, rule_id=rule_id)


def test_basic_stable_placeholders():
    text = "张三向李四借款"
    result = redact(
        text,
        [_span(0, 2, 0, "PERSON", 0), _span(3, 5, 0, "PERSON", 1)],
    )
    assert result.redacted_text == "{{PERSON:01}}向{{PERSON:02}}借款"
    assert all(s.kept for s in result.spans)


def test_same_category_value_reuses_placeholder():
    text = "张三认识张三"
    result = redact(
        text,
        [_span(0, 2, 0, "PERSON", 0), _span(4, 6, 0, "PERSON", 1)],
    )
    assert result.redacted_text == "{{PERSON:01}}认识{{PERSON:01}}"
    assert result.spans[0].placeholder == result.spans[1].placeholder


def test_overlapping_higher_priority_wins():
    # 低优先级大片段 vs 高优先级子片段
    text = "甲方：北京某某科技有限公司"
    result = redact(
        text,
        [
            _span(3, 13, 1, "ORG", 0),    # 北京某某科技有限公司 高优先级
            _span(0, 13, 0, "OTHER", 1),  # 整行 低优先级，重叠丢弃
        ]
    )
    assert result.spans[0].kept
    assert result.spans[0].category == "ORG"
    assert not result.spans[1].kept
    assert result.spans[1].drop_reason == "overlaps_higher_priority"
    assert result.redacted_text == "甲方：{{ORG:01}}"


def test_equal_priority_first_come_first_served():
    text = "abcdef"
    result = redact(
        text,
        [_span(0, 3, 0, "A", 0), _span(2, 5, 0, "B", 1)],
    )
    assert result.spans[0].kept
    assert not result.spans[1].kept


def test_unicode_codepoint_offsets():
    # 含 emoji 与中文，按码位计数
    text = "合同🎉甲方张三"
    assert text[3:5] == "甲方"
    result = redact(text, [_span(5, 7, 0, "PERSON", 0)])
    assert result.redacted_text == "合同🎉甲方{{PERSON:01}}"


def test_nested_spans_both_kept_when_not_overlapping_higher_priority():
    # 两个相邻（非重叠）片段均保留
    text = "0123456789"
    result = redact(text, [_span(0, 2, 5, "A", 0), _span(2, 4, 1, "B", 1)])
    assert result.redacted_text == "{{A:01}}{{B:01}}456789"


def test_resolution_is_deterministic_regardless_of_input_order():
    text = "abcdef"
    low_first = redact(text, [_span(2, 5, 0, "B", 0), _span(0, 3, 5, "A", 1)])
    high_first = redact(text, [_span(0, 3, 5, "A", 0), _span(2, 5, 0, "B", 1)])
    assert low_first.redacted_text == high_first.redacted_text == "{{A:01}}def"


def test_assign_placeholders_width_grows():
    spans = resolve_spans(
        "012345678901",
        [
            _span(0, 1, 0, "X", 0),
            _span(1, 2, 0, "X", 1),
            _span(2, 3, 0, "X", 2),
            _span(3, 4, 0, "X", 3),
            _span(4, 5, 0, "X", 4),
            _span(5, 6, 0, "X", 5),
            _span(6, 7, 0, "X", 6),
            _span(7, 8, 0, "X", 7),
            _span(8, 9, 0, "X", 8),
            _span(9, 10, 0, "X", 9),
        ],
    )
    assign_placeholders(spans)
    assert spans[0].placeholder == "{{X:01}}"
    assert spans[9].placeholder == "{{X:10}}"


def test_manual_wins_when_priority_offsets_length_all_equal():
    text = "abcdef"
    # 同起止、同长度、同优先级；ordinal 让自动片段“先到”，仍应由人工片段胜出
    result = redact(
        text,
        [
            _span(0, 3, 0, "AUTO", 0, source=AUTO, rule_id="r1"),
            _span(0, 3, 0, "MANUAL", 1),
        ],
    )
    kept = [s for s in result.spans if s.kept]
    dropped = [s for s in result.spans if not s.kept]
    assert len(kept) == 1 and kept[0].category == "MANUAL"
    assert kept[0].source == MANUAL and kept[0].rule_id is None
    assert len(dropped) == 1 and dropped[0].source == AUTO
    assert dropped[0].rule_id == "r1"
    assert dropped[0].drop_reason == "overlaps_higher_priority"
    assert result.redacted_text == "{{MANUAL:01}}def"


def test_higher_priority_auto_beats_manual():
    text = "abcdef"
    result = redact(
        text,
        [
            _span(0, 3, 5, "AUTO", 0, source=AUTO, rule_id="r1"),
            _span(0, 3, 0, "MANUAL", 1),
        ],
    )
    kept = [s for s in result.spans if s.kept]
    assert kept[0].category == "AUTO" and kept[0].rule_id == "r1"


def test_auto_span_labels_preserved_through_placeholder_assignment():
    text = "张三张三"
    result = redact(text, [_span(0, 2, 0, "PERSON", 0, source=AUTO, rule_id="p")])
    assert result.redacted_text == "{{PERSON:01}}张三"
    assert result.spans[0].source == AUTO and result.spans[0].rule_id == "p"


# ---------- 片段冲突溯源：fragment_id / decision_rank / blocked_by_fragment_id ----------


def test_fragment_ids_follow_merged_original_order_and_kept_not_blocked():
    # 人工候选 ordinal 在前、自动候选接其后；返回按 fragment_id 顺序
    text = "abcdefghij"
    result = redact(
        text,
        [
            _span(0, 2, 0, "A", 0),
            _span(4, 6, 0, "B", 1, source=AUTO, rule_id="r"),
            _span(8, 10, 0, "C", 2),
        ],
    )
    assert [s.fragment_id for s in result.spans] == [1, 2, 3]
    assert all(s.blocked_by_fragment_id is None for s in result.spans)
    # 决策次序恰好也是落位顺序（彼此不相交，按 start 排序）
    assert [s.decision_rank for s in sorted(result.spans, key=lambda s: s.fragment_id)] == [
        1,
        2,
        3,
    ]


def test_decision_rank_follows_priority_sort_not_original_order():
    # 低优先级先提交（fragment_id=1），但高优先级后提交者先落位（decision_rank=1）
    text = "abcdef"
    result = redact(
        text,
        [
            _span(2, 5, 0, "LOW", 0),
            _span(0, 3, 5, "HIGH", 1),
        ],
    )
    by_id = {s.fragment_id: s for s in result.spans}
    assert by_id[1].priority == 0 and by_id[1].decision_rank == 2
    assert by_id[2].priority == 5 and by_id[2].decision_rank == 1


def test_exact_duplicate_candidates_keep_distinct_fragment_ids():
    text = "abcdef"
    result = redact(
        text,
        [
            _span(0, 3, 0, "A", 0),
            _span(0, 3, 0, "A", 1),  # 与前一个完全重复
        ],
    )
    ids = [s.fragment_id for s in result.spans]
    assert ids == [1, 2]
    assert len(set(ids)) == 2
    kept, dropped = result.spans
    assert kept.kept and kept.blocked_by_fragment_id is None
    assert not dropped.kept
    assert dropped.blocked_by_fragment_id == 1
    assert dropped.drop_reason == "overlaps_higher_priority"  # 既有语义不变


def test_blocker_is_first_decision_among_multiple_kept_intervals():
    # 丢弃片段跨越两个已保留区间：相交者中 decision_rank 最小者才是阻断者，
    # 而不是 start 更小 / 事后任取的相交项
    text = "0123456789ABCDEF"
    result = redact(
        text,
        [
            _span(0, 8, 1, "FIRST", 0),   # 低优先级大区间
            _span(2, 4, 9, "EARLY", 1),   # 高优先级、start 较小
            _span(6, 8, 9, "LATER", 2),   # 高优先级、start 较大
        ],
    )
    by_id = {s.fragment_id: s for s in result.spans}
    assert by_id[2].kept and by_id[2].decision_rank == 1  # 最高优先级最先落位
    assert by_id[3].kept and by_id[3].decision_rank == 2
    big = by_id[1]
    assert not big.kept
    # 与 2、3 同时相交，但 2 的决策次序更靠前
    assert big.blocked_by_fragment_id == 2


def test_blocker_choice_ignores_start_when_decision_order_differs():
    # 反过来：start 更小的片段决策次序更靠后，阻断者仍取决策最早者
    text = "0123456789"
    result = redact(
        text,
        [
            _span(6, 9, 5, "EARLY_DECISION", 0),  # 高优先级但 start 大，先落位
            _span(0, 3, 3, "LATE_SMALL_START", 1),  # 低优先级 start 小，随后落位
            _span(1, 8, 1, "SPANNER", 2),  # 跨越两者
        ],
    )
    by_id = {s.fragment_id: s for s in result.spans}
    spanner = by_id[3]
    assert not spanner.kept
    assert spanner.blocked_by_fragment_id == 1  # [6,9) 决策最早，尽管 start 更大


def test_equal_priority_blocker_is_first_placed():
    # 同优先级先到先得：两个已落位片段相交于后到者，阻断者是先落位的那个
    text = "0123456789"
    result = redact(
        text,
        [
            _span(0, 3, 0, "A", 0),
            _span(3, 6, 0, "B", 1),  # 与 A 相邻，均保留
            _span(2, 5, 0, "C", 2),  # 同时撞 A、B；A 决策更早
        ],
    )
    by_id = {s.fragment_id: s for s in result.spans}
    assert by_id[1].kept and by_id[2].kept
    assert not by_id[3].kept
    assert by_id[3].blocked_by_fragment_id == 1


def test_cross_source_auto_fragment_can_block_manual_and_ids_distinct():
    # 自动候选按合并顺序排在人工之后（fragment_id 更大），但高优先级使其先落位
    text = "abcdef"
    result = redact(
        text,
        [
            _span(0, 3, 0, "MANUAL_TAG", 0),
            _span(0, 3, 9, "AUTO_TAG", 1, source=AUTO, rule_id="r1"),
        ],
    )
    by_id = {s.fragment_id: s for s in result.spans}
    assert set(by_id) == {1, 2}
    auto, manual = by_id[2], by_id[1]
    assert auto.kept and auto.decision_rank == 1
    assert not manual.kept and manual.decision_rank == 2
    assert manual.blocked_by_fragment_id == 2
    assert manual.drop_reason == "overlaps_higher_priority"


def test_cross_source_manual_wins_tie_and_auto_records_blocker():
    text = "abcdef"
    result = redact(
        text,
        [
            _span(0, 3, 0, "AUTO_TAG", 0, source=AUTO, rule_id="r1"),
            _span(0, 3, 0, "MANUAL_TAG", 1),
        ],
    )
    by_id = {s.fragment_id: s for s in result.spans}
    assert by_id[1].source == AUTO and not by_id[1].kept
    assert by_id[1].blocked_by_fragment_id == 2
    assert by_id[2].kept and by_id[2].blocked_by_fragment_id is None
