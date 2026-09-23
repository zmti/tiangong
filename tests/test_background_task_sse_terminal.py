"""后台任务 SSE 订阅:task_done 终结事件不可丢 + 订阅竞态防护测试。

复现场景:
1. 旧 _emit 队列满时所有事件(含 task_done)静默丢弃 → 订阅者拿不到收尾信号,
   subscribe() 的 0.15s 轮询永久循环("后台任务会话中断")。
   修复后:task_done/error 终态事件先排空队列再入队,保证一定送达。
2. 旧 subscribe() 在"任务恰好在两次轮询之间完成且无事件进队"时,
   因持有陈旧 rec 引用而判断不准。修复后每轮重查 rec,并在终态+空队时
   补投一个 task_done 收尾事件再 return,订阅方一定能收到终结信号。
"""
import asyncio
import time

from app.core.tasks import TaskManager


def _make_mgr(tmp=None):
    mgr = TaskManager.__new__(TaskManager)
    import threading
    mgr._lock = threading.Lock()
    mgr._tasks = {}
    mgr._queues = {}
    mgr._cancel_flags = {}
    mgr._running = 0
    mgr._db_path = ":memory:"
    return mgr


def test_emit_delivers_task_done_under_queue_pressure():
    """队列满时 task_done 终结事件必须入队(订阅者靠它终止)。"""
    mgr = _make_mgr()
    # 造一个几乎满的队列(容量 5,塞 4 条普通事件)
    q = asyncio.Queue(maxsize=5)
    mgr._queues["t1"] = q
    for i in range(4):
        q.put_nowait({"type": "token", "content": f"x{i}"})

    async def _run():
        await mgr._emit("t1", {"type": "token", "content": "overflow"})  # 普通事件:丢最旧
        await mgr._emit("t1", {"type": "task_done", "task_id": "t1", "status": "done", "answer": ""})

    asyncio.run(_run())
    drained = []
    while not q.empty():
        drained.append(q.get_nowait())
    types = [d.get("type") for d in drained]
    assert "task_done" in types, f"task_done 必须送达,实际队列: {types}"
    # task_done 必须在队尾(最后送达)
    assert types[-1] == "task_done", f"task_done 应是最后一个事件,实际: {types}"


def test_subscribe_terminates_when_task_completes_between_polls():
    """任务在轮询间隙完成且队列无事件时,订阅者仍必须收到 task_done 并终止。

    用 asyncio.wait_for 兜底:若 subscribe 因竞态陷入永久轮询(旧 bug),
    测试会在 5s 超时后报 TimeoutError,而非挂死整个测试套件。
    """
    mgr = _make_mgr()
    from app.core.tasks import TaskRecord

    rec = TaskRecord(task_id="t2", session_id="s", message="m")
    mgr._tasks["t2"] = rec
    q = asyncio.Queue(maxsize=500)
    mgr._queues["t2"] = q

    async def _run():
        gen = mgr.subscribe("t2")
        # 订阅者开始时任务 running,会先阻塞;我们在它第一次 sleep 期间把任务置终态:
        # 模拟"轮询间隙完成且无事件进队"的极端竞态(旧 bug 在此永久轮询)。
        events = []
        async def _consumer():
            async for evt in gen:
                events.append(evt)
        consumer = asyncio.ensure_future(_consumer())
        await asyncio.sleep(0.05)  # 让订阅者进入第一轮 0.15s sleep
        rec.status = "done"
        rec.answer = "最终答案"
        await consumer  # 订阅者应在下一轮检测到终态并补投 task_done 收尾
        return events

    events = asyncio.run(asyncio.wait_for(_run(), timeout=5))
    types = [e.get("type") for e in events]
    assert "task_done" in types, f"订阅者必须收到 task_done 收尾,实际: {types}"
    assert types[-1] == "task_done", f"task_done 应是最后事件,实际: {types}"
