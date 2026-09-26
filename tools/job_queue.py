# -*- coding: utf-8 -*-
"""任务队列 + 并发控制：固定数量的 worker 协程从 asyncio.Queue 消费生成任务。

- submit()：任务入队，立即返回（超过并发上限的任务排队等待，不占用连接）；
- worker_count 个消费者循环拉取并执行 handler，天然把并发限制在 worker_count；
- 单机零外部依赖。横向扩展到多机时，可把本队列替换为 arq/Celery + Redis，
  上层接口（submit / 状态查询）保持不变。
"""
import asyncio
import logging

logger = logging.getLogger("job_queue")


class JobQueue:
    def __init__(self, worker_count: int, handler) -> None:
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker_count = worker_count
        self._handler = handler
        self._workers: list[asyncio.Task] = []
        self._running = False

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._workers = [
            asyncio.create_task(self._worker(i + 1)) for i in range(self._worker_count)
        ]
        logger.info("任务队列已启动：%s 个 worker", self._worker_count)

    async def submit(self, ctx) -> None:
        await self._queue.put(ctx)

    def backlog(self) -> int:
        """排队中的任务数（不含正在执行的）。"""
        return self._queue.qsize()

    async def _worker(self, no: int) -> None:
        while self._running:
            ctx = await self._queue.get()
            try:
                logger.info("worker%s 开始执行任务 %s（排队剩余 %s）",
                            no, ctx.task_id, self._queue.qsize())
                await self._handler(ctx)
            except Exception:  # noqa: BLE001 - worker 不能被单个任务打死
                logger.exception("worker%s 执行任务 %s 时异常", no, ctx.task_id)
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        self._running = False
        for w in self._workers:
            w.cancel()
        # 让 worker 在取消异常中优雅退出
        await asyncio.gather(*self._workers, return_exceptions=True)
        logger.info("任务队列已停止")
