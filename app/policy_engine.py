"""自动脱敏策略的规则编译与超时受控匹配。

设计要点：

* 规则使用 Python 内置 ``re`` 模块，默认 Unicode 语义，直接在服务端持有的
  Unicode 原文（码位序列）上扫描，得到的 ``start()/end()`` 即码位偏移；
* **空匹配一律拒绝**：编译后若模式在空串上即可匹配（或运行时产生零宽度命中），
  视为非法规则 / 非法命中，避免插入长度为 0 的脱敏片段；
* **单规则执行超时**：正则匹配在独立的**工作进程**中执行。``re`` 的回溯在
  C 层进行且不释放 GIL，进程内的线程无法可靠计时、更无法中断灾难性回溯；
  因此用一个常驻子进程经管道接收任务，超时即 ``SIGKILL`` 杀掉并在下次调用时
  重建。任一规则超时，本次提交立即得到 422 且不写入文档。
"""
from __future__ import annotations

import asyncio
import contextlib
import multiprocessing as mp
import re
import threading
from dataclasses import dataclass

from app.services.errors import ServiceError


class RuleCompileError(ServiceError):
    """规则正则无法编译或必然产生空匹配（创建策略时返回 422）。"""

    status_code = 422


class RuleTimeoutError(ServiceError):
    """单条规则匹配超时（提交文档时返回 422）。"""

    status_code = 422


class EmptyMatchError(ServiceError):
    """规则在原文上产生了空匹配（提交文档时返回 422）。"""

    status_code = 422


class TooManyMatchesError(ServiceError):
    """自动命中数量超过上限（提交文档时返回 422）。"""

    status_code = 422


@dataclass(frozen=True)
class RuleSpec:
    rule_id: str
    category: str
    pattern: str
    priority: int
    ordinal: int = 0

    def compile(self) -> "CompiledRule":
        return compile_rule(self)


@dataclass
class CompiledRule:
    spec: RuleSpec
    regex: re.Pattern[str]


@dataclass(frozen=True)
class AutoMatch:
    start: int
    end: int
    rule: RuleSpec


def compile_rule(spec: RuleSpec) -> CompiledRule:
    """编译规则并拒绝可产生空匹配的模式。"""
    try:
        regex = re.compile(spec.pattern)
    except re.error as exc:
        raise RuleCompileError(
            f"规则 {spec.rule_id!r} 的正则无法编译：{exc}", 422
        ) from exc
    if regex.match("") is not None:
        raise RuleCompileError(
            f"规则 {spec.rule_id!r} 的正则 {spec.pattern!r} 可产生空匹配，已拒绝"
            "（请改用至少消费一个码位的模式）",
            422,
        )
    return CompiledRule(spec=spec, regex=regex)


def compile_rules(specs: list[RuleSpec]) -> list[CompiledRule]:
    return [compile_rule(s) for s in specs]


# ============================ 超时执行：独立工作进程 ============================


def _worker_loop(conn) -> None:  # pragma: no cover - 在子进程中运行
    """子进程入口：循环接收 (pattern, text)，回传 ("ok", [(s,e),...]) 或错误。"""
    while True:
        try:
            message = conn.recv()
        except (EOFError, OSError):
            return
        if message is None:
            conn.close()
            return
        pattern, text = message
        try:
            matches = [(m.start(), m.end()) for m in re.finditer(pattern, text)]
            conn.send(("ok", matches))
        except Exception as exc:  # noqa: BLE001 - 回传错误信息给主进程
            conn.send(("error", f"{type(exc).__name__}: {exc}"))


class ProcessRuleRunner:
    """常驻子进程执行正则匹配；发送-接收整体加锁，超时即杀进程并在下次重建。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ctx = mp.get_context("spawn")
        self._proc: mp.Process | None = None
        self._conn = None

    def _ensure_worker(self) -> None:
        if self._proc is not None and self._proc.is_alive():
            return
        parent_conn, child_conn = self._ctx.Pipe(duplex=True)
        proc = self._ctx.Process(
            target=_worker_loop, args=(child_conn,), name="redaction-rule-worker"
        )
        proc.daemon = True
        proc.start()
        child_conn.close()  # 父进程只保留 parent 端
        self._proc = proc
        self._conn = parent_conn

    def run(self, pattern: str, text: str, timeout: float) -> list[tuple[int, int]]:
        with self._lock:
            self._ensure_worker()
            assert self._conn is not None and self._proc is not None
            try:
                self._conn.send((pattern, text))
            except (BrokenPipeError, OSError) as exc:
                self._discard_worker()
                raise ServiceError(f"规则执行进程通信失败：{exc}", 500) from exc

            # poll 由连接对象在内核态等待并释放 GIL，不依赖事件循环调度；
            # 超时后工作进程直接被杀死，不存在“回溯无法中断”的残留问题。
            if not self._conn.poll(timeout):
                self._discard_worker()
                raise RuleTimeoutError("规则执行超时", 422)

            try:
                kind, payload = self._conn.recv()
            except EOFError as exc:
                self._discard_worker()
                raise ServiceError("规则执行进程意外退出", 500) from exc
            # 响应已就绪即视为本次成功（进程恰在回传后退出不影响结果）；
            # 仅在进程已死时回收，避免下一次请求用到坏连接。
            if not self._proc.is_alive():
                self._discard_worker()

        if kind == "error":
            raise RuleCompileError(f"规则执行报错：{payload}", 422)
        return payload

    def _discard_worker(self) -> None:
        proc, conn = self._proc, self._conn
        self._proc, self._conn = None, None
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()
        if proc is not None:
            with contextlib.suppress(Exception):
                proc.kill()
            proc.join(timeout=5)

    def close(self) -> None:
        with self._lock:
            if (
                self._conn is not None
                and self._proc is not None
                and self._proc.is_alive()
            ):
                with contextlib.suppress(Exception):
                    self._conn.send(None)
            self._discard_worker()


# 进程级单例：整个应用共用一个常驻工作进程，懒启动
_RUNNER: ProcessRuleRunner | None = None
_RUNNER_LOCK = threading.Lock()


def get_runner() -> ProcessRuleRunner:
    global _RUNNER
    with _RUNNER_LOCK:
        if _RUNNER is None:
            _RUNNER = ProcessRuleRunner()
        return _RUNNER


async def apply_rules(
    text: str,
    rules: list[CompiledRule],
    timeout: float,
    max_matches: int,
    runner: ProcessRuleRunner,
) -> list[AutoMatch]:
    """顺序执行全部规则，汇总非空命中。任一规则超时 / 空命中即整体失败。"""
    matches: list[AutoMatch] = []
    for rule in rules:
        try:
            found = await asyncio.to_thread(
                runner.run, rule.spec.pattern, text, timeout
            )
        except RuleTimeoutError as exc:
            raise RuleTimeoutError(
                f"规则 {rule.spec.rule_id!r} 执行超过 {timeout:g} 秒，已拒绝本次"
                "提交（文档未写入）",
                422,
            ) from exc
        for start, end in found:
            if start == end:
                # 创建时未暴露的零宽度可能（如零宽断言配合 * / ?）在此兜底
                raise EmptyMatchError(
                    f"规则 {rule.spec.rule_id!r} 产生了空匹配（{start}），"
                    "已拒绝本次提交（文档未写入）",
                    422,
                )
            matches.append(AutoMatch(start=start, end=end, rule=rule.spec))
        if len(matches) > max_matches:
            raise TooManyMatchesError(
                f"自动命中片段数超过上限 {max_matches}，已拒绝本次提交（文档未写入）",
                422,
            )
    return matches
