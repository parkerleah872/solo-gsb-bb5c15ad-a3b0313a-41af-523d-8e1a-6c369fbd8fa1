"""自动脱敏策略：创建 / 查询 / 提交合并 / 空匹配与超时的端到端测试。"""
import sqlite3


TEXT = "甲方张三向乙方李四借款人民币10000元，联系电话13800138000。"


async def _create_policy(client, rules, name="测试策略"):
    resp = await client.post("/policies", json={"name": name, "rules": rules})
    assert resp.status_code == 201, resp.text
    return resp.json()


PERSON_RULES = [
    {"rule_id": "person-name", "category": "person", "pattern": "张三|李四", "priority": 5},
    {"rule_id": "money", "category": "money", "pattern": r"人民币\d+元", "priority": 3},
    {"rule_id": "phone", "category": "phone", "pattern": r"1\d{10}", "priority": 2},
]


async def test_policy_create_and_get(client):
    created = await _create_policy(client, PERSON_RULES, name="合同 PII")
    policy_id = created["policy_id"]
    assert len(policy_id) == 32
    assert created["name"] == "合同 PII"
    assert [r["rule_id"] for r in created["rules"]] == [
        "person-name",
        "money",
        "phone",
    ]
    assert created["rules"][0]["category"] == "PERSON"  # 类别规范化大写
    assert created["created_at"]

    got = await client.get(f"/policies/{policy_id}")
    assert got.status_code == 200
    body = got.json()
    assert body["policy_id"] == policy_id
    assert body["rules"] == created["rules"]
    # 不可变：不存在更新接口
    assert (await client.put(f"/policies/{policy_id}", json={})).status_code == 405
    assert (await client.delete(f"/policies/{policy_id}")).status_code == 405


async def test_policy_get_missing_404(client):
    resp = await client.get("/policies/" + "f" * 32)
    assert resp.status_code == 404


async def test_policy_duplicate_rule_id_rejected(client):
    rules = [
        {"rule_id": "r1", "category": "A", "pattern": "a+", "priority": 1},
        {"rule_id": "r1", "category": "B", "pattern": "b+", "priority": 1},
    ]
    resp = await client.post("/policies", json={"rules": rules})
    assert resp.status_code == 422
    assert "rule_id" in resp.json()["detail"]


async def test_policy_regex_compile_error_rejected(client):
    resp = await client.post(
        "/policies",
        json={"rules": [{"rule_id": "bad", "category": "A", "pattern": "(?<unclosed", "priority": 0}]},
    )
    assert resp.status_code == 422
    assert "无法编译" in resp.json()["detail"]


async def test_policy_empty_match_pattern_rejected(client):
    for pattern in ("a*", r"\d?", "(?:foo)?", "(a|)"):
        resp = await client.post(
            "/policies",
            json={"rules": [{"rule_id": "empty", "category": "A", "pattern": pattern, "priority": 0}]},
        )
        assert resp.status_code == 422, pattern
        assert "空匹配" in resp.json()["detail"]


async def test_policy_bad_category_rejected(client):
    resp = await client.post(
        "/policies",
        json={"rules": [{"rule_id": "r", "category": "bad 类别", "pattern": "a+", "priority": 0}]},
    )
    assert resp.status_code == 422


async def test_submit_with_policy_generates_auto_fragments(client):
    policy = await _create_policy(client, PERSON_RULES)
    resp = await client.post(
        "/documents",
        json={"text": TEXT, "policy_id": policy["policy_id"]},
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert data["redacted_text"] == (
        "甲方{{PERSON:01}}向乙方{{PERSON:02}}借款{{MONEY:01}}，"
        "联系电话{{PHONE:01}}。"
    )
    kept = {f["start"]: f for f in data["fragments"] if f["kept"]}
    assert kept[2]["source"] == "auto"
    assert kept[2]["rule_id"] == "person-name"
    assert kept[11]["rule_id"] == "money"
    assert all(f["source"] == "auto" for f in data["fragments"])

    # 查询接口同样带来源标注
    q = await client.get(f"/documents/{data['document_id']}")
    assert q.status_code == 200
    assert all(f["source"] == "auto" for f in q.json()["fragments"])

    # 恢复明细也标注 source / rule_id
    r = await client.post(
        "/restore",
        json={"token": data["restore_token"], "placeholders": ["{{PERSON:01}}"]},
    )
    occ = r.json()["restored"][0]["occurrences"][0]
    assert occ["source"] == "auto"
    assert occ["rule_id"] == "person-name"


async def test_submit_merges_manual_and_auto_with_labels(client):
    policy = await _create_policy(client, PERSON_RULES)
    # 手传一个自动规则本会命中的片段（张三 [2,4)），类别不同
    resp = await client.post(
        "/documents",
        json={
            "text": TEXT,
            "policy_id": policy["policy_id"],
            "spans": [
                {"start": 2, "end": 4, "priority": 5, "category": "NAME"},
            ],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    assert "{{NAME:01}}" in data["redacted_text"]
    by_key = {(f["start"], f["end"], f["source"]): f for f in data["fragments"]}
    manual = by_key[(2, 4, "manual")]
    auto = by_key[(7, 9, "auto")]
    assert manual["kept"] and manual["source"] == "manual" and manual["rule_id"] is None
    assert auto["kept"] and auto["source"] == "auto" and auto["rule_id"] == "person-name"


async def test_manual_wins_when_priority_offsets_length_equal(client):
    # 自动规则恰好命中 [2,4) 张三，优先级与人工片段相同（5）：人工优先落位
    rules = [{"rule_id": "p", "category": "AUTO_TAG", "pattern": "张三", "priority": 5}]
    policy = await _create_policy(client, rules)
    resp = await client.post(
        "/documents",
        json={
            "text": TEXT,
            "policy_id": policy["policy_id"],
            "spans": [{"start": 2, "end": 4, "priority": 5, "category": "MANUAL_TAG"}],
        },
    )
    data = resp.json()
    assert resp.status_code == 201, resp.text
    frags = [f for f in data["fragments"] if f["start"] == 2 and f["end"] == 4]
    kept = [f for f in frags if f["kept"]]
    dropped = [f for f in frags if not f["kept"]]
    assert len(kept) == 1 and kept[0]["source"] == "manual"
    assert kept[0]["category"] == "MANUAL_TAG"
    assert len(dropped) == 1 and dropped[0]["source"] == "auto"
    assert dropped[0]["rule_id"] == "p"
    assert dropped[0]["drop_reason"] == "overlaps_higher_priority"
    assert "{{MANUAL_TAG:01}}" in data["redacted_text"]


async def test_higher_priority_auto_beats_manual(client):
    rules = [{"rule_id": "p", "category": "AUTO_TAG", "pattern": "张三", "priority": 9}]
    policy = await _create_policy(client, rules)
    resp = await client.post(
        "/documents",
        json={
            "text": TEXT,
            "policy_id": policy["policy_id"],
            "spans": [{"start": 2, "end": 4, "priority": 5, "category": "MANUAL_TAG"}],
        },
    )
    data = resp.json()
    assert resp.status_code == 201, resp.text
    frags = [f for f in data["fragments"] if f["start"] == 2 and f["end"] == 4]
    kept = [f for f in frags if f["kept"]][0]
    assert kept["source"] == "auto" and kept["category"] == "AUTO_TAG"
    assert "{{AUTO_TAG:01}}" in data["redacted_text"]


async def test_auto_matching_uses_unicode_codepoints(client):
    rules = [{"rule_id": "e", "category": "EMOJI", "pattern": "🎉+", "priority": 1}]
    policy = await _create_policy(client, rules)
    text = "合同🎉甲方"
    resp = await client.post(
        "/documents",
        json={"text": text, "policy_id": policy["policy_id"]},
    )
    assert resp.status_code == 201, resp.text
    frag = next(f for f in resp.json()["fragments"] if f["kept"])
    # 码位偏移：🎉 位于 [2,3)
    assert (frag["start"], frag["end"], frag["rule_id"]) == (2, 3, "e")
    assert resp.json()["redacted_text"] == "合同{{EMOJI:01}}甲方"


async def test_submit_unknown_policy_404(client):
    resp = await client.post(
        "/documents",
        json={"text": TEXT, "policy_id": "0" * 32},
    )
    assert resp.status_code == 404


async def test_submit_requires_spans_or_policy(client):
    resp = await client.post("/documents", json={"text": TEXT})
    assert resp.status_code == 422


async def test_rule_runtime_empty_match_aborts_submit(client):
    # 创建期检查空串无法暴露（* 作用在非空断言之后），运行时在 "bbb" 前产生空命中
    rules = [{"rule_id": "r", "category": "A", "pattern": r"(?<=b)a*", "priority": 1}]
    policy = await _create_policy(client, rules)
    resp = await client.post(
        "/documents",
        json={"text": "bbb", "policy_id": policy["policy_id"]},
    )
    assert resp.status_code == 422
    assert "空匹配" in resp.json()["detail"]


async def test_rule_timeout_aborts_submit_and_persists_nothing(client, settings):
    settings.rule_timeout = 0.1
    # 经典灾难性回溯：22 个 "a" 无 b 可匹配，回溯约 1.6 秒，必然超过 0.1 秒
    evil = {"rule_id": "evil", "category": "A", "pattern": r"(a+)+b", "priority": 1}
    policy = await _create_policy(client, [evil])

    # 直接用底层 sqlite 连接计数，确认超时时没有任何文档落库
    db_path = settings.db_path
    before = _count_documents(db_path)
    resp = await client.post(
        "/documents",
        json={"text": "a" * 22, "policy_id": policy["policy_id"]},
    )
    assert resp.status_code == 422
    assert "超过" in resp.json()["detail"]
    after = _count_documents(db_path)
    assert before == after == 0

    # 超时杀掉工作进程后自动重建：正常策略提交不受影响（服务仍可用）
    good = await _create_policy(
        client, [{"rule_id": "ok", "category": "OK", "pattern": "a+", "priority": 1}]
    )
    ok = await client.post(
        "/documents",
        json={"text": "aaa", "policy_id": good["policy_id"]},
    )
    assert ok.status_code == 201, ok.text


async def test_too_many_auto_matches_aborts_submit(client):
    rules = [{"rule_id": "r", "category": "A", "pattern": "a", "priority": 1}]
    policy = await _create_policy(client, rules)
    # 自动命中数（含人工片段占用名额）超过 MAX_SPANS（5000）
    resp = await client.post(
        "/documents",
        json={"text": "a" * 6000, "policy_id": policy["policy_id"]},
    )
    assert resp.status_code == 422
    assert "上限" in resp.json()["detail"]


async def test_policy_reused_placeholder_spans_share_placeholder(client):
    rules = [{"rule_id": "p", "category": "PERSON", "pattern": "张三", "priority": 1}]
    policy = await _create_policy(client, rules)
    text = "张三认识张三"
    # 自动命中两处 + 人工在第一处同位置同优先级（人工优先，自动丢弃）
    resp = await client.post(
        "/documents",
        json={
            "text": text,
            "policy_id": policy["policy_id"],
            "spans": [{"start": 0, "end": 2, "priority": 1, "category": "person"}],
        },
    )
    assert resp.status_code == 201, resp.text
    data = resp.json()
    # (PERSON, 张三) 人工与自动同类别同原文 -> 复用同一占位符
    assert data["redacted_text"] == "{{PERSON:01}}认识{{PERSON:01}}"
    kept = [f for f in data["fragments"] if f["kept"]]
    assert len(kept) == 2
    assert kept[0]["source"] == "manual"
    assert kept[1]["source"] == "auto" and kept[1]["rule_id"] == "p"


def _count_documents(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
    finally:
        conn.close()
