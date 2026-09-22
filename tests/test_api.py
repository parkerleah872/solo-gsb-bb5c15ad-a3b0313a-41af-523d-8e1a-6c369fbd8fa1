"""端到端 API 测试：提交 / 查询 / 一次性恢复 / 过期。"""
import asyncio

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import app


TEXT = "甲方：北京某某科技有限公司，授权代表张三，联系北京某某科技有限公司。"
# 码位：甲0 方1 ：2 北3 京4 某5 某6 科7 技8 有9 限10 公11 司12 ，13 ...


def _payload(ttl=None, token_ttl=None):
    spans = [
        {"start": 3, "end": 13, "priority": 1, "category": "org"},
        # 重复出现的同一 (类别, 原文)，应复用占位符
        {"start": 23, "end": 33, "priority": 1, "category": "ORG"},
        # 张三 高优先级人名
        {"start": 18, "end": 20, "priority": 5, "category": "person"},
        # 低优先级大跨度，与上面全部重叠 -> 丢弃
        {"start": 3, "end": 33, "priority": 0, "category": "BLOCK"},
    ]
    body: dict = {"text": TEXT, "spans": spans}
    if ttl is not None:
        body["ttl"] = ttl
    return body


async def test_submit_query_and_restore_full_flow(client):
    resp = await client.post("/documents", json=_payload())
    assert resp.status_code == 201, resp.text
    data = resp.json()

    assert data["redacted_text"] == (
        "甲方：{{ORG:01}}，授权代表{{PERSON:01}}，联系{{ORG:01}}。"
    )
    # 类别规范化为大写
    kept = [f for f in data["fragments"] if f["kept"]]
    dropped = [f for f in data["fragments"] if not f["kept"]]
    assert len(kept) == 3
    assert len(dropped) == 1
    assert dropped[0]["drop_reason"] == "overlaps_higher_priority"

    token = data["restore_token"]
    assert len(token) == 64
    doc_id = data["document_id"]

    # 查询接口返回脱敏文本，不含原文
    q = await client.get(f"/documents/{doc_id}")
    assert q.status_code == 200
    assert "北京" not in q.json()["redacted_text"]

    # 一次性恢复：指定两个占位符
    r = await client.post(
        "/restore",
        json={"token": token, "placeholders": ["{{ORG:01}}", "{{PERSON:01}}"]},
    )
    assert r.status_code == 200, r.text
    rdata = r.json()
    values = {item["placeholder"]: item["value"] for item in rdata["restored"]}
    assert values["{{ORG:01}}"] == "北京某某科技有限公司"
    assert values["{{PERSON:01}}"] == "张三"
    org = next(i for i in rdata["restored"] if i["placeholder"] == "{{ORG:01}}")
    assert len(org["occurrences"]) == 2  # 复用占位符的两处出现
    new_token = rdata["restore_token"]
    assert new_token != token

    # 旧令牌已作废
    reused = await client.post(
        "/restore",
        json={"token": token, "placeholders": ["{{PERSON:01}}"]},
    )
    assert reused.status_code == 401

    # 新令牌可以继续使用
    again = await client.post(
        "/restore",
        json={"token": new_token, "placeholders": ["{{PERSON:01}}"]},
    )
    assert again.status_code == 200
    assert again.json()["restored"][0]["value"] == "张三"


async def test_restore_unknown_placeholder_rejected(client):
    data = (await client.post("/documents", json=_payload())).json()
    resp = await client.post(
        "/restore",
        json={"token": data["restore_token"], "placeholders": ["{{MONEY:99}}"]},
    )
    assert resp.status_code == 400


async def test_restore_bad_token(client):
    await client.post("/documents", json=_payload())
    resp = await client.post(
        "/restore",
        json={"token": "deadbeef" * 8, "placeholders": ["{{ORG:01}}"]},
    )
    assert resp.status_code == 401


async def test_get_missing_document_404(client):
    resp = await client.get("/documents/" + "0" * 32)
    assert resp.status_code == 404


async def test_invalid_span_offsets_422(client):
    body = {
        "text": "abc",
        "spans": [{"start": 1, "end": 9, "priority": 0, "category": "X"}],
    }
    resp = await client.post("/documents", json=body)
    assert resp.status_code == 422


async def test_invalid_category_rejected(client):
    body = {
        "text": "abc",
        "spans": [{"start": 0, "end": 1, "priority": 0, "category": "bad 类别"}],
    }
    resp = await client.post("/documents", json=body)
    assert resp.status_code == 422


async def test_document_expiry_lazy_delete(settings):
    settings.default_ttl = 1
    # lifespan 在首次进入时才执行，先改 ttl 再启动应用
    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            data = (await client.post("/documents", json=_payload())).json()
            doc_id = data["document_id"]

            await asyncio.sleep(1.1)
            resp = await client.get(f"/documents/{doc_id}")
            assert resp.status_code == 410

            # 文档过期后令牌恢复同样失败
            r = await client.post(
                "/restore",
                json={"token": data["restore_token"], "placeholders": ["{{ORG:01}}"]},
            )
            assert r.status_code in (401, 410)

    get_settings.cache_clear()


async def test_health(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
