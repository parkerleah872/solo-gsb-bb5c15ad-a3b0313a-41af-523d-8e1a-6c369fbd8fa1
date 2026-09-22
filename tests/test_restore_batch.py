"""批量恢复接口：单事务全量校验、整体回滚、按请求顺序返回。"""
import asyncio

from asgi_lifespan import LifespanManager
from httpx import ASGITransport, AsyncClient

from app.config import get_settings
from app.main import app

TEXT_A = "甲方：北京某某科技有限公司，授权代表张三。"
TEXT_B = "乙方：上海某某贸易有限公司，授权代表李四。"


async def _submit(client, text, name):
    start = text.index(name)
    body = {
        "text": text,
        "spans": [
            {
                "start": start,
                "end": start + len(name),
                "priority": 1,
                "category": "person",
            }
        ],
    }
    resp = await client.post("/documents", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _item(token, *placeholders):
    return {"token": token, "placeholders": list(placeholders)}


async def test_batch_restore_success_in_request_order(client):
    a = await _submit(client, TEXT_A, "张三")
    b = await _submit(client, TEXT_B, "李四")

    resp = await client.post(
        "/restore/batch",
        json={
            "items": [
                _item(a["restore_token"], "{{PERSON:01}}"),
                _item(b["restore_token"], "{{PERSON:01}}"),
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 2

    # 按请求顺序返回，各项互不影响
    assert results[0]["restored"][0]["value"] == "张三"
    assert results[0]["restored"][0]["placeholder"] == "{{PERSON:01}}"
    assert results[1]["restored"][0]["value"] == "李四"
    for item, old in zip(results, (a, b)):
        assert item["restore_token"] != old["restore_token"]
        assert item["token_expires_at"]
    assert results[0]["restore_token"] != results[1]["restore_token"]

    # 旧令牌全部作废
    for old in (a, b):
        reused = await client.post(
            "/restore",
            json={"token": old["restore_token"], "placeholders": ["{{PERSON:01}}"]},
        )
        assert reused.status_code == 401

    # 轮换后的新令牌可继续使用
    again = await client.post(
        "/restore",
        json={"token": results[0]["restore_token"], "placeholders": ["{{PERSON:01}}"]},
    )
    assert again.status_code == 200
    assert again.json()["restored"][0]["value"] == "张三"


async def test_batch_single_item(client):
    a = await _submit(client, TEXT_A, "张三")
    resp = await client.post(
        "/restore/batch",
        json={"items": [_item(a["restore_token"], "{{PERSON:01}}")]},
    )
    assert resp.status_code == 200, resp.text
    results = resp.json()["results"]
    assert len(results) == 1
    assert results[0]["restored"][0]["value"] == "张三"


async def test_batch_rollback_when_any_token_invalid(client):
    a = await _submit(client, TEXT_A, "张三")
    b = await _submit(client, TEXT_B, "李四")

    resp = await client.post(
        "/restore/batch",
        json={
            "items": [
                _item(a["restore_token"], "{{PERSON:01}}"),
                _item("deadbeef" * 8, "{{PERSON:01}}"),
            ]
        },
    )
    assert resp.status_code == 401
    # 不泄露其他项的恢复值
    assert "张三" not in resp.text and "李四" not in resp.text

    # 整体回滚：两项令牌均未被作废，仍可单独恢复
    for doc, name in ((a, "张三"), (b, "李四")):
        r = await client.post(
            "/restore",
            json={"token": doc["restore_token"], "placeholders": ["{{PERSON:01}}"]},
        )
        assert r.status_code == 200
        assert r.json()["restored"][0]["value"] == name


async def test_batch_rollback_when_placeholder_unknown(client):
    a = await _submit(client, TEXT_A, "张三")
    b = await _submit(client, TEXT_B, "李四")

    resp = await client.post(
        "/restore/batch",
        json={
            "items": [
                _item(a["restore_token"], "{{PERSON:01}}"),
                _item(b["restore_token"], "{{ORG:99}}"),
            ]
        },
    )
    assert resp.status_code == 400
    assert "张三" not in resp.text

    # 回滚：两个令牌都保持未用
    for doc in (a, b):
        r = await client.post(
            "/restore",
            json={"token": doc["restore_token"], "placeholders": ["{{PERSON:01}}"]},
        )
        assert r.status_code == 200


async def test_batch_rejects_used_token(client):
    a = await _submit(client, TEXT_A, "张三")
    b = await _submit(client, TEXT_B, "李四")
    first = await client.post(
        "/restore",
        json={"token": a["restore_token"], "placeholders": ["{{PERSON:01}}"]},
    )
    assert first.status_code == 200

    resp = await client.post(
        "/restore/batch",
        json={
            "items": [
                _item(a["restore_token"], "{{PERSON:01}}"),
                _item(b["restore_token"], "{{PERSON:01}}"),
            ]
        },
    )
    assert resp.status_code == 401
    # b 的令牌不受牵连
    r = await client.post(
        "/restore",
        json={"token": b["restore_token"], "placeholders": ["{{PERSON:01}}"]},
    )
    assert r.status_code == 200


async def test_batch_rejects_duplicate_token(client):
    a = await _submit(client, TEXT_A, "张三")
    resp = await client.post(
        "/restore/batch",
        json={
            "items": [
                _item(a["restore_token"], "{{PERSON:01}}"),
                _item(a["restore_token"], "{{PERSON:01}}"),
            ]
        },
    )
    assert resp.status_code == 400
    # 令牌未被消费
    r = await client.post(
        "/restore",
        json={"token": a["restore_token"], "placeholders": ["{{PERSON:01}}"]},
    )
    assert r.status_code == 200


async def test_batch_size_limits(client):
    resp = await client.post("/restore/batch", json={"items": []})
    assert resp.status_code == 422

    items = [_item(f"{i:064x}", "{{PERSON:01}}") for i in range(21)]
    resp = await client.post("/restore/batch", json={"items": items})
    assert resp.status_code == 422


async def test_batch_item_requires_explicit_placeholders(client):
    a = await _submit(client, TEXT_A, "张三")
    resp = await client.post(
        "/restore/batch",
        json={"items": [_item(a["restore_token"])]},
    )
    assert resp.status_code == 422

    resp = await client.post(
        "/restore/batch",
        json={"items": [_item(a["restore_token"], "not-a-placeholder")]},
    )
    assert resp.status_code == 422


async def test_batch_expired_token_rolls_back(settings):
    settings.default_ttl = 1
    settings.token_ttl = 1
    # lifespan 在首次进入时才执行，先改 ttl 再启动应用
    async with LifespanManager(app):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            a = await _submit(client, TEXT_A, "张三")
            b = await _submit(client, TEXT_B, "李四")

            await asyncio.sleep(1.1)
            resp = await client.post(
                "/restore/batch",
                json={
                    "items": [
                        _item(a["restore_token"], "{{PERSON:01}}"),
                        _item(b["restore_token"], "{{PERSON:01}}"),
                    ]
                },
            )
            assert resp.status_code == 401
            assert "张三" not in resp.text and "李四" not in resp.text

    get_settings.cache_clear()
