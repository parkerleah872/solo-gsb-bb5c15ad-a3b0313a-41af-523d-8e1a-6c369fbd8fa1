"""自动脱敏策略的创建与查询。

策略一经创建即**不可变**：仅提供创建 / 查询接口，不提供修改与删除；规则按
创建时的提交顺序存储。策略只保存不敏感的正则规则，与文档原文物理无关。
"""
from __future__ import annotations

import uuid

import aiosqlite

from app.policy_engine import RuleSpec, compile_rules
from app.redaction import validate_category
from app.schemas import PolicyCreateRequest
from app.services.errors import NotFoundError, ServiceError
from app.services.timeutil import from_iso, to_iso, utcnow


class PolicyService:
    def __init__(self, db: aiosqlite.Connection) -> None:
        self.db = db

    async def create(self, req: PolicyCreateRequest) -> dict:
        rule_ids = [r.rule_id for r in req.rules]
        if len(set(rule_ids)) != len(rule_ids):
            dupes = sorted({rid for rid in rule_ids if rule_ids.count(rid) > 1})
            raise ServiceError(f"rule_id 在同一策略内必须唯一：{dupes}", 422)

        specs: list[RuleSpec] = []
        for i, r in enumerate(req.rules):
            try:
                validate_category(r.category)
            except ValueError as exc:
                raise ServiceError(str(exc), 422) from exc
            specs.append(
                RuleSpec(
                    rule_id=r.rule_id,
                    category=r.category,
                    pattern=r.pattern,
                    priority=r.priority,
                    ordinal=i,
                )
            )
        # 编译期校验：非法正则 / 空匹配模式在创建时即拒绝（422）
        compile_rules(specs)

        policy_id = uuid.uuid4().hex
        created_at = utcnow()
        async with self.db.cursor() as cur:
            await cur.execute(
                "INSERT INTO policies (id, name, created_at) VALUES (?, ?, ?)",
                (policy_id, req.name, to_iso(created_at)),
            )
            await cur.executemany(
                "INSERT INTO policy_rules"
                " (policy_id, rule_id, category, pattern, priority, ordinal)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                [
                    (policy_id, s.rule_id, s.category, s.pattern, s.priority, s.ordinal)
                    for s in specs
                ],
            )
            await self.db.commit()

        return {
            "policy_id": policy_id,
            "name": req.name,
            "created_at": created_at,
            "rules": [
                {
                    "rule_id": s.rule_id,
                    "category": s.category,
                    "pattern": s.pattern,
                    "priority": s.priority,
                }
                for s in specs
            ],
        }

    async def get(self, policy_id: str) -> dict:
        async with self.db.execute(
            "SELECT id, name, created_at FROM policies WHERE id = ?",
            (policy_id,),
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise NotFoundError("脱敏策略不存在")
        async with self.db.execute(
            "SELECT rule_id, category, pattern, priority FROM policy_rules"
            " WHERE policy_id = ? ORDER BY ordinal",
            (policy_id,),
        ) as cur:
            rules = await cur.fetchall()
        return {
            "policy_id": row["id"],
            "name": row["name"],
            "created_at": from_iso(row["created_at"]),
            "rules": [
                {
                    "rule_id": r["rule_id"],
                    "category": r["category"],
                    "pattern": r["pattern"],
                    "priority": r["priority"],
                }
                for r in rules
            ],
        }

    async def load_specs(self, policy_id: str) -> list[RuleSpec]:
        """供文档提交使用：加载策略的全部规则（按创建顺序）。"""
        async with self.db.execute(
            "SELECT rule_id, category, pattern, priority, ordinal FROM policy_rules"
            " WHERE policy_id = ? ORDER BY ordinal",
            (policy_id,),
        ) as cur:
            rows = await cur.fetchall()
        if not rows:
            # 区分“策略不存在”与“无规则策略”（后者创建接口已禁止）
            async with self.db.execute(
                "SELECT 1 FROM policies WHERE id = ?", (policy_id,)
            ) as cur:
                if await cur.fetchone() is None:
                    raise NotFoundError("脱敏策略不存在")
        return [
            RuleSpec(
                rule_id=r["rule_id"],
                category=r["category"],
                pattern=r["pattern"],
                priority=r["priority"],
                ordinal=r["ordinal"],
            )
            for r in rows
        ]
