"""片段冲突溯源的端到端测试：

覆盖：同优先级先到先得、人工/自动跨来源阻断、一个候选同时跨越多个已保留区间、
完全重复候选保持不同 ID；并校验提交响应与文档查询返回一致（持久化后不漂移）。
"""


def _by_id(fragments):
    return {f["fragment_id"]: f for f in fragments}


async def test_submit_and_query_return_trace_fields_without_drift(client):
    text = "0123456789"
    resp = await client.post(
        "/documents",
        json={
            "text": text,
            "spans": [
                {"start": 0, "end": 3, "priority": 0, "category": "A"},  # id1 先落位
                {"start": 3, "end": 6, "priority": 0, "category": "B"},  # id2 相邻保留
                # 同优先级后到，同时撞 id1 / id2：阻断者取决策更早的 id1
                {"start": 2, "end": 5, "priority": 0, "category": "C"},  # id3
                # 与 id1 完全重复的候选：独立 ID，同样被 id1 阻断
                {"start": 0, "end": 3, "priority": 0, "category": "A"},  # id4
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()

    fragments = data["fragments"]
    # 响应按合并后的原始顺序（fragment_id）排列
    assert [f["fragment_id"] for f in fragments] == [1, 2, 3, 4]
    assert len({f["fragment_id"] for f in fragments}) == 4  # 完全重复也各有独立 ID

    submit = _by_id(fragments)
    assert submit[1]["kept"] and submit[1]["blocked_by_fragment_id"] is None
    assert submit[1]["decision_rank"] == 1
    # 决策次序：id1 落位(1) -> id4 完全重复被拒(2) -> id3 被拒(3) -> id2 落位(4)
    assert submit[2]["kept"] and submit[2]["decision_rank"] == 4
    assert submit[3]["kept"] is False
    assert submit[3]["decision_rank"] == 3
    assert submit[3]["blocked_by_fragment_id"] == 1
    assert submit[3]["drop_reason"] == "overlaps_higher_priority"  # 既有语义不变
    assert submit[4]["kept"] is False
    assert submit[4]["decision_rank"] == 2
    assert submit[4]["blocked_by_fragment_id"] == 1
    assert submit[4]["drop_reason"] == "overlaps_higher_priority"

    # 持久化后再次查询：字段与排序均不漂移
    got = await client.get(f"/documents/{data['document_id']}")
    assert got.status_code == 200
    stored = got.json()["fragments"]
    assert [f["fragment_id"] for f in stored] == [1, 2, 3, 4]
    for submitted, row in zip(fragments, stored):
        assert row == submitted


async def test_one_candidate_spanning_multiple_kept_intervals(client):
    text = "0123456789ABCDEF"
    resp = await client.post(
        "/documents",
        json={
            "text": text,
            "spans": [
                {"start": 0, "end": 8, "priority": 1, "category": "C"},  # id1 大跨度低优先级
                {"start": 2, "end": 4, "priority": 9, "category": "A"},  # id2 高优先级先落位
                {"start": 6, "end": 8, "priority": 9, "category": "B"},  # id3 次之落位
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    fragments = _by_id(resp.json()["fragments"])

    assert fragments[2]["kept"] and fragments[2]["decision_rank"] == 1
    assert fragments[3]["kept"] and fragments[3]["decision_rank"] == 2
    spanner = fragments[1]
    assert not spanner["kept"]
    # 与 id2、id3 同时相交；阻断者是决策先后最早的 id2，而非任取一个相交项
    assert spanner["blocked_by_fragment_id"] == 2
    assert spanner["drop_reason"] == "overlaps_higher_priority"
    assert resp.json()["redacted_text"] == "01{{A:01}}45{{B:01}}89ABCDEF"


async def test_cross_source_auto_fragment_blocks_manual(client):
    # 自动候选按合并顺序排在人工之后（fragment_id=2），但优先级更高、先落位
    policy = await client.post(
        "/policies",
        json={
            "rules": [
                {"rule_id": "p", "category": "AUTO_TAG", "pattern": "张三", "priority": 9}
            ]
        },
    )
    assert policy.status_code == 201, policy.text
    policy_id = policy.json()["policy_id"]

    resp = await client.post(
        "/documents",
        json={
            "text": "张三",
            "policy_id": policy_id,
            "spans": [{"start": 0, "end": 2, "priority": 0, "category": "MANUAL_TAG"}],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    fragments = _by_id(data["fragments"])

    assert set(fragments) == {1, 2}  # 人工 id1、自动 id2
    manual, auto = fragments[1], fragments[2]
    assert manual["source"] == "manual" and not manual["kept"]
    assert manual["decision_rank"] == 2
    assert manual["blocked_by_fragment_id"] == 2
    assert auto["source"] == "auto" and auto["rule_id"] == "p"
    assert auto["kept"] and auto["decision_rank"] == 1
    assert auto["blocked_by_fragment_id"] is None
    assert "{{AUTO_TAG:01}}" in data["redacted_text"]

    # 查询接口返回相同溯源字段
    got = await client.get(f"/documents/{data['document_id']}")
    assert got.status_code == 200
    stored = _by_id(got.json()["fragments"])
    assert stored[1]["blocked_by_fragment_id"] == 2
    assert stored[2]["blocked_by_fragment_id"] is None
    assert stored[1]["decision_rank"] == 2 and stored[2]["decision_rank"] == 1


async def test_cross_source_manual_tie_blocks_auto(client):
    # 同起止、同长度、同优先级：人工先落位，自动候选记录阻断者为人工片段
    policy = await client.post(
        "/policies",
        json={
            "rules": [
                {"rule_id": "p", "category": "AUTO_TAG", "pattern": "张三", "priority": 5}
            ]
        },
    )
    policy_id = policy.json()["policy_id"]
    resp = await client.post(
        "/documents",
        json={
            "text": "张三",
            "policy_id": policy_id,
            "spans": [{"start": 0, "end": 2, "priority": 5, "category": "MANUAL_TAG"}],
        },
    )
    assert resp.status_code == 201, resp.text
    fragments = _by_id(resp.json()["fragments"])
    assert fragments[1]["kept"] and fragments[1]["source"] == "manual"
    auto = fragments[2]
    assert not auto["kept"] and auto["source"] == "auto"
    assert auto["blocked_by_fragment_id"] == 1
    assert auto["drop_reason"] == "overlaps_higher_priority"


async def test_all_kept_fragments_have_null_blocker_and_distinct_ranks(client):
    text = "abcdefgh"
    resp = await client.post(
        "/documents",
        json={
            "text": text,
            "spans": [
                {"start": 0, "end": 2, "priority": 3, "category": "A"},
                {"start": 2, "end": 4, "priority": 1, "category": "B"},
                {"start": 4, "end": 6, "priority": 2, "category": "C"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    fragments = resp.json()["fragments"]
    assert all(f["kept"] for f in fragments)
    assert all(f["blocked_by_fragment_id"] is None for f in fragments)
    # decision_rank 是 1..N 的排列（高优先级 [0,2) 最先）
    ranks = sorted(f["decision_rank"] for f in fragments)
    assert ranks == [1, 2, 3]
    first = next(f for f in fragments if f["fragment_id"] == 1)
    assert first["decision_rank"] == 1
