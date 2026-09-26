# -*- coding: utf-8 -*-
"""数据库层（SQLAlchemy 2.0 + SQLite）：用户、任务记录、链路 trace 事件。

表：
- users：用户账号（bcrypt 密码哈希）；
- task_records：每个生成任务的归属、状态、成片路径（"我的作品"数据源）；
- trace_events：链路可观测事件（每一步的 agent/模型/耗时/状态）。
"""
import json
import time
from pathlib import Path

from sqlalchemy import (Column, DateTime, ForeignKey, Integer, String, Text,
                        create_engine, func)
from sqlalchemy.orm import (DeclarativeBase, Mapped, mapped_column,
                            relationship, sessionmaker)

import config


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[str] = mapped_column(DateTime, server_default=func.now())

    tasks = relationship("TaskRecord", back_populates="user",
                         cascade="all, delete-orphan")


class TaskRecord(Base):
    __tablename__ = "task_records"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str] = mapped_column(String(100), default="")
    status: Mapped[str] = mapped_column(String(16), default="running")  # running/done/failed
    progress: Mapped[int] = mapped_column(Integer, default=0)
    final_video: Mapped[str] = mapped_column(String(256), default="")
    request_json: Mapped[str] = mapped_column(Text, default="{}")
    error_msg: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[str] = mapped_column(DateTime, server_default=func.now())
    updated_at: Mapped[str] = mapped_column(DateTime, server_default=func.now(),
                                            onupdate=func.now())

    user = relationship("User", back_populates="tasks")


class TraceEvent(Base):
    __tablename__ = "trace_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    task_id: Mapped[str] = mapped_column(String(32), index=True)
    ts: Mapped[float] = mapped_column(default=time.time)
    agent: Mapped[str] = mapped_column(String(32), default="")
    step: Mapped[str] = mapped_column(String(64), default="")
    model: Mapped[str] = mapped_column(String(64), default="")
    status: Mapped[str] = mapped_column(String(16), default="ok")  # ok/error
    duration_ms: Mapped[int] = mapped_column(Integer, default=0)
    detail: Mapped[str] = mapped_column(String(500), default="")


# check_same_thread=False：FastAPI 多协程共享连接；SQLite 本地写入串行，足够本项目量级
engine = create_engine(config.DB_URL, connect_args={"check_same_thread": False},
                       future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, future=True)


def init_db() -> None:
    Base.metadata.create_all(engine)


# ---------------------------------------------------------------- 仓库函数
def create_user(session, username: str, password_hash: str) -> User:
    user = User(username=username, password_hash=password_hash)
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def get_user_by_name(session, username: str) -> User | None:
    return session.query(User).filter(User.username == username).one_or_none()


def get_user(session, user_id: int) -> User | None:
    return session.get(User, user_id)


def create_task_record(session, task_id: str, user_id: int, title: str,
                       request: dict) -> TaskRecord:
    rec = TaskRecord(
        task_id=task_id, user_id=user_id, title=title,
        request_json=json.dumps(request, ensure_ascii=False)[:8000],
    )
    session.add(rec)
    session.commit()
    return rec


def get_task_record(session, task_id: str) -> TaskRecord | None:
    return session.query(TaskRecord).filter(TaskRecord.task_id == task_id).one_or_none()


def update_task_record(session, task_id: str, **fields) -> None:
    rec = get_task_record(session, task_id)
    if not rec:
        return
    for k, v in fields.items():
        setattr(rec, k, v)
    session.commit()


def list_user_tasks(session, user_id: int) -> list[TaskRecord]:
    return (session.query(TaskRecord)
            .filter(TaskRecord.user_id == user_id)
            .order_by(TaskRecord.id.desc()).all())


def add_trace_event(session, event: dict) -> None:
    row = TraceEvent(
        task_id=event.get("task_id", ""),
        agent=event.get("agent", ""),
        step=event.get("step", ""),
        model=event.get("model", ""),
        status=event.get("status", "ok"),
        duration_ms=int(event.get("duration_ms", 0)),
        detail=event.get("detail", "")[:500],
    )
    session.add(row)
    session.commit()


def list_trace_events(session, task_id: str) -> list[TraceEvent]:
    return (session.query(TraceEvent)
            .filter(TraceEvent.task_id == task_id)
            .order_by(TraceEvent.id.asc()).all())


def task_to_dict(rec: TaskRecord) -> dict:
    return {
        "task_id": rec.task_id, "title": rec.title, "status": rec.status,
        "progress": rec.progress, "final_video": rec.final_video,
        "error_msg": rec.error_msg, "created_at": str(rec.created_at),
    }


def trace_to_dict(row: TraceEvent) -> dict:
    return {
        "task_id": row.task_id, "agent": row.agent, "step": row.step,
        "model": row.model, "status": row.status,
        "duration_ms": row.duration_ms, "detail": row.detail,
    }
