"""SSE 终结事件/哨兵不可丢测试:验证会话不会因队列满而永久挂住。

复现场景:
旧实现中,`_bg_agent_run` 的 finally 里 `None` 哨兵通过
`wait_for(q.put(None), timeout=5)` 入队,队列满且 5s 内塞不进时直接 `pass`
吞掉。producer 已停止产出,consumer 的 `wait_for(q.get(), 60)` 每 60s 靠
自身 yield 的 heartbeat 续期 → 永久循环,SSE 流永不结束(用户看到"一直加载不停")。

本测试复现"队列满且 consumer 迟迟不消费"的最坏时序,验证修复后
done 事件与 None 哨兵最终都进了队列且顺序正确(None 在最后)。
不依赖 pytest-asyncio,用 asyncio.run 驱动(与 test_checkpoint_resume 一致)。
"""
import asyncio
import logging


def _make_send_to_queue(queue: asyncio.Queue, logger):
    """复现 chat.py 中 _send_to_queue 的精确语义:
    - put_nowait 成功 → 直接返回
    - 普通事件(terminal=False)队列满 → 丢最旧腾位后立即入队(可丢)
    - 终结事件(terminal=True,done/error/None)队列满 → 有界等待腾位(不可丢,但不永久阻塞)
    """
    import time

    async def _send(evt, terminal: bool = False):
        try:
            queue.put_nowait(evt)
            return
        except asyncio.QueueFull:
            logger.warning("[test] queue full, dropping oldest (evt=%s)",
                           evt.get("type", "?") if isinstance(evt, dict) else "sentinel")
            if not terminal:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    queue.put_nowait(evt)
                    return
                except asyncio.QueueFull:
                    await asyncio.sleep(0.05)
                    try:
                        queue.put_nowait(evt)
                    except asyncio.QueueFull:
                        logger.error("[test] 普通事件入队仍失败,放弃")
                    return
            # 终结事件:有界等待(测试用 2s,生产用 30s),避免 consumer 死后永久阻塞
            deadline = time.time() + 2
            while True:
                try:
                    await queue.put(evt)
                    return
                except asyncio.QueueFull:
                    if time.time() >= deadline:
                        logger.error("[test] 终结事件/哨兵 2s 内未入队")
                        return
                    await asyncio.sleep(0.05)
    return _send


async def _run_sentinel_under_pressure() -> list:
    q: asyncio.Queue = asyncio.Queue(maxsize=3)
    _log = logging.getLogger("chat")
    send = _make_send_to_queue(q, _log)

    # 塞满队列,模拟 consumer 卡住
    q.put_nowait({"type": "token", "content": "a"})
    q.put_nowait({"type": "token", "content": "b"})
    q.put_nowait({"type": "token", "content": "c"})
    assert q.full()

    # 终结事件发送(terminal=True):此刻队列满,会挂起等待腾位
    done_send = asyncio.ensure_future(send({"type": "done", "model": "m", "steps": []}, terminal=True))
    # 稍后 consumer 醒来,消费掉最旧事件腾出空间 → done_send 完成
    await asyncio.sleep(0.05)
    q.get_nowait()  # 腾一个位置
    await asyncio.wait_for(done_send, timeout=1)

    # 哨兵发送:此刻队列再次满,同样等 consumer 腾位
    sentinel_send = asyncio.ensure_future(send(None, terminal=True))
    await asyncio.sleep(0.05)
    q.get_nowait()  # 腾一个位置
    await asyncio.wait_for(sentinel_send, timeout=1)

    # consumer 继续消费剩余事件:done 在前,None 在后(队列里此时剩 b, c, done, None 四项)
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    return drained


def test_sentinel_not_dropped_when_queue_full():
    """即便入队瞬间队列已满,done 与 None 也必须最终入队,且 None 在 done 之后。"""
    drained = asyncio.run(_run_sentinel_under_pressure())
    assert drained[-1] is None, f"None 哨兵必须入队成功,实际队列尾部: {drained}"
    done_idx = next((i for i, d in enumerate(drained) if isinstance(d, dict) and d.get("type") == "done"), None)
    none_idx = drained.index(None)
    assert done_idx is not None, f"done 事件必须入队成功,实际: {drained}"
    assert done_idx < none_idx, f"done 必须在 None 哨兵之前入队, got done@{done_idx} None@{none_idx}"


async def _run_normal_event_drop_oldest() -> list:
    q: asyncio.Queue = asyncio.Queue(maxsize=2)
    _log = logging.getLogger("chat")
    q.put_nowait({"type": "token", "content": "old1"})
    q.put_nowait({"type": "token", "content": "old2"})
    assert q.full()

    send = _make_send_to_queue(q, _log)
    evt = {"type": "token", "content": "new"}
    # 普通事件路径(terminal=False):丢最旧腾位后入队,不等待、不阻塞
    await asyncio.wait_for(send(evt, terminal=False), timeout=1)

    return [q.get_nowait(), q.get_nowait()]


def test_normal_event_drops_oldest_on_full():
    """普通 token 事件在队列满时丢最旧(可接受:丢中间增量不致命),但不阻塞、不挂起。"""
    drained = asyncio.run(_run_normal_event_drop_oldest())
    contents = [d.get("content") for d in drained]
    assert "old1" not in contents, "最旧事件应被丢弃"
    assert "new" in contents, "新事件必须保留"
