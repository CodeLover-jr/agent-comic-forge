# -*- coding: utf-8 -*-
"""FastAPI 入口：认证、任务提交（队列化）、SSE 实时日志/trace、成片静态服务。

启动：uvicorn main:app --host 127.0.0.1 --port 8000
"""
import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

import config
import db
import auth as auth_mod
from agents import orchestrator
from state import TaskContext
from tools import tracer
from tools.job_queue import JobQueue

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logger = logging.getLogger("main")


# --------------------------------------------------------------------------- 请求模型
class Character(BaseModel):
    name: str = Field(..., max_length=20)
    gender: str = "男"
    age: str = ""
    personality: str = ""
    appearance: str = ""


class Scene(BaseModel):
    name: str = Field(..., max_length=30)
    atmosphere: str = ""


class GenerateRequest(BaseModel):
    plot: str = Field(..., min_length=1, max_length=500, description="剧情梗概 1~500 字")
    characters: list[Character] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    style: str = "国漫"
    aspect: str = "9:16"


class AuthRequest(BaseModel):
    username: str = Field(..., min_length=2, max_length=32)
    password: str = Field(..., min_length=6, max_length=64)


# --------------------------------------------------------------------------- 内存态任务
class TaskStore:
    def __init__(self) -> None:
        self.tasks: dict[str, TaskContext] = {}

    def create(self, req: GenerateRequest, user_id: int) -> TaskContext:
        task_id = uuid.uuid4().hex[:12]
        out_dir = config.OUTPUT_DIR / task_id
        ctx = TaskContext(task_id=task_id, user_input=req.model_dump(), out_dir=out_dir)
        ctx.user_id = user_id  # 归属用户（用于权限校验）
        self.tasks[task_id] = ctx
        return ctx

    def get(self, task_id: str) -> TaskContext | None:
        return self.tasks.get(task_id)


store = TaskStore()


# --------------------------------------------------------------------------- trace 桥
class TraceBridge:
    """把工具埋点事件：SSE 实时推送 + 落库 trace_events。"""

    def __init__(self, ctx: TaskContext) -> None:
        self.ctx = ctx

    def record(self, event: dict) -> None:
        event["task_id"] = self.ctx.task_id
        self.ctx._emit({"type": "trace", **event})
        try:
            with db.SessionLocal() as session:
                db.add_trace_event(session, event)
        except Exception:  # noqa: BLE001 - 观测落库失败不影响主流程
            logger.warning("trace 落库失败", exc_info=True)


async def _run_task(ctx: TaskContext) -> None:
    """后台执行整条流水线（在队列 worker 中被调用），统一收尾。"""
    token = tracer.bind_sink(TraceBridge(ctx))
    try:
        await orchestrator.run(ctx)
        if ctx.final_video and Path(ctx.final_video).exists():
            url = f"/output/{ctx.task_id}/{Path(ctx.final_video).name}"
            ctx.log("系统", f"成片已生成：{url}")
            ctx.finish(url)
            with db.SessionLocal() as session:
                db.update_task_record(session, ctx.task_id,
                                      status="done", progress=100, final_video=url)
        else:
            msg = "流水线结束但未生成成片（请检查各环节日志与密钥配置）"
            ctx.fail(msg)
            with db.SessionLocal() as session:
                db.update_task_record(session, ctx.task_id,
                                      status="failed", error_msg=msg)
    except Exception as e:  # noqa: BLE001
        logger.exception("task %s failed", ctx.task_id)
        ctx.fail(f"任务异常终止：{e}")
        with db.SessionLocal() as session:
            db.update_task_record(session, ctx.task_id,
                                  status="failed", error_msg=str(e)[:400])
    finally:
        tracer.reset_sink(token)


# --------------------------------------------------------------------------- 生命周期
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    queue = JobQueue(config.MAX_CONCURRENT_TASKS, _run_task)
    await queue.start()
    app.state.queue = queue
    yield
    await queue.stop()


app = FastAPI(title="AI漫剧一键生成平台", version="2.0.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# --------------------------------------------------------------------------- 认证接口
@app.post("/api/auth/register")
async def api_register(req: AuthRequest) -> JSONResponse:
    with db.SessionLocal() as session:
        if db.get_user_by_name(session, req.username):
            raise HTTPException(status_code=409, detail="用户名已被注册")
        user = db.create_user(session, req.username,
                              auth_mod.hash_password(req.password))
        token = auth_mod.create_access_token(user.id, user.username)
    return JSONResponse({"access_token": token, "username": req.username})


@app.post("/api/auth/login")
async def api_login(req: AuthRequest) -> JSONResponse:
    with db.SessionLocal() as session:
        user = db.get_user_by_name(session, req.username)
        if not user or not auth_mod.verify_password(req.password, user.password_hash):
            raise HTTPException(status_code=401, detail="用户名或密码错误")
        token = auth_mod.create_access_token(user.id, user.username)
    return JSONResponse({"access_token": token, "username": user.username})


@app.get("/api/auth/me")
async def api_me(user: db.User = Depends(auth_mod.get_current_user)) -> JSONResponse:
    return JSONResponse({"id": user.id, "username": user.username})


# --------------------------------------------------------------------------- 生成接口
@app.post("/api/generate")
async def api_generate(req: GenerateRequest,
                       user: db.User = Depends(auth_mod.get_current_user)) -> JSONResponse:
    if not req.characters:
        raise HTTPException(status_code=400, detail="至少添加一个角色")
    if not req.scenes:
        raise HTTPException(status_code=400, detail="至少添加一个场景")
    if req.aspect not in config.ASPECT_SIZES:
        raise HTTPException(status_code=400, detail="画幅仅支持 9:16 或 16:9")

    ctx = store.create(req, user.id)
    with db.SessionLocal() as session:
        db.create_task_record(session, ctx.task_id, user.id,
                              req.plot[:24], req.model_dump())
    ctx.log("系统", "任务已提交，等待队列调度...")
    backlog = app.state.queue.backlog()
    ctx.log("系统", (f"前面还有 {backlog} 个任务排队" if backlog else "队列空闲，立即开始"))
    await app.state.queue.submit(ctx)
    return JSONResponse({"task_id": ctx.task_id, "message": "任务已提交"})


# --------------------------------------------------------------------------- 我的作品
@app.get("/api/tasks")
async def api_list_tasks(user: db.User = Depends(auth_mod.get_current_user)) -> JSONResponse:
    with db.SessionLocal() as session:
        rows = db.list_user_tasks(session, user.id)
        return JSONResponse([db.task_to_dict(r) for r in rows])


def _check_owner(task_id: str, user: db.User) -> TaskContext:
    """取任务并校验归属；进程重启后内存无 ctx 时回退查库。"""
    ctx = store.get(task_id)
    if ctx is not None:
        if getattr(ctx, "user_id", None) != user.id:
            raise HTTPException(status_code=403, detail="无权访问该任务")
        return ctx
    with db.SessionLocal() as session:
        rec = db.get_task_record(session, task_id)
        if not rec:
            raise HTTPException(status_code=404, detail="任务不存在")
        if rec.user_id != user.id:
            raise HTTPException(status_code=403, detail="无权访问该任务")
    return None  # 仅库里有记录（服务重启后）


@app.get("/api/tasks/{task_id}")
async def api_status(task_id: str,
                     user: db.User = Depends(auth_mod.get_current_user)) -> JSONResponse:
    ctx = _check_owner(task_id, user)
    if ctx is not None:
        return JSONResponse(ctx.status_payload())
    with db.SessionLocal() as session:
        return JSONResponse(db.task_to_dict(db.get_task_record(session, task_id)))


@app.get("/api/tasks/{task_id}/trace")
async def api_trace(task_id: str,
                    user: db.User = Depends(auth_mod.get_current_user)) -> JSONResponse:
    _check_owner(task_id, user)
    with db.SessionLocal() as session:
        rows = db.list_trace_events(session, task_id)
        return JSONResponse([db.trace_to_dict(r) for r in rows])


@app.post("/api/tasks/{task_id}/pause")
async def api_pause_task(task_id: str,
                         user: db.User = Depends(auth_mod.get_current_user)):
    """暂停任务：协作式挂起，当前步骤完成后在安全点等待，不产生脏数据。"""
    ctx = _check_owner(task_id, user)
    if ctx is None:
        raise HTTPException(status_code=400, detail="任务已结束或不在运行中，无法暂停")
    if ctx.status != "running":
        raise HTTPException(status_code=400, detail="任务未在运行中")
    ctx.pause()
    return {"ok": True, "paused": True}


@app.post("/api/tasks/{task_id}/resume")
async def api_resume_task(task_id: str,
                          user: db.User = Depends(auth_mod.get_current_user)):
    """继续已暂停的任务。"""
    ctx = _check_owner(task_id, user)
    if ctx is None:
        raise HTTPException(status_code=400, detail="任务已结束，无法继续")
    if not ctx.is_paused:
        raise HTTPException(status_code=400, detail="任务未处于暂停状态")
    ctx.resume()
    return {"ok": True, "resumed": True}


@app.get("/api/tasks/{task_id}/events")
async def api_events(task_id: str, token: str = "") -> StreamingResponse:
    """SSE 事件流：log / progress / trace / done / error。

    EventSource 无法自定义请求头，故 JWT 通过 ?token= 传入。
    """
    if not token:
        raise HTTPException(status_code=401, detail="缺少 token")
    payload = auth_mod.decode_token(token)
    user_id = int(payload["sub"])

    def _owner_check() -> TaskContext | None:
        ctx = store.get(task_id)
        if ctx is not None:
            if getattr(ctx, "user_id", None) != user_id:
                raise HTTPException(status_code=403, detail="无权访问该任务")
            return ctx
        with db.SessionLocal() as session:
            rec = db.get_task_record(session, task_id)
            if not rec:
                raise HTTPException(status_code=404, detail="任务不存在")
            if rec.user_id != user_id:
                raise HTTPException(status_code=403, detail="无权访问该任务")
        return None

    ctx = _owner_check()

    async def gen_live():
        q: asyncio.Queue = asyncio.Queue()
        ctx.subscribe(q)
        for e in ctx.events:  # 先重放已发生事件
            yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
        while True:
            try:
                e = await asyncio.wait_for(q.get(), timeout=90)
            except asyncio.TimeoutError:
                if ctx.status in ("done", "failed"):
                    break
                continue
            yield f"data: {json.dumps(e, ensure_ascii=False)}\n\n"
            if e.get("type") in ("done", "error"):
                break

    async def gen_replay():
        # 服务重启后内存无 ctx：用库中的 trace + 终态重放
        with db.SessionLocal() as session:
            rec = db.get_task_record(session, task_id)
            for row in db.list_trace_events(session, task_id):
                yield f"data: {json.dumps({'type': 'trace', **db.trace_to_dict(row)}, ensure_ascii=False)}\n\n"
        if rec.status == "done":
            yield f"data: {json.dumps({'type': 'done', 'video_url': rec.final_video})}\n\n"
        else:
            yield f"data: {json.dumps({'type': 'error', 'message': rec.error_msg})}\n\n"

    gen = gen_live if ctx is not None else gen_replay
    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )


# 成片 / 中间产物静态服务
app.mount("/output", StaticFiles(directory=str(config.OUTPUT_DIR)), name="output")


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(config.FRONTEND_DIR / "index.html")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=config.HOST, port=config.PORT)
