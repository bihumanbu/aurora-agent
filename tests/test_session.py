"""Session 管理测试：多窗口独立、互不串扰、创建/获取/删除、会话恢复。"""

import asyncio

import pytest

from aurora.runtime.context import BucketedContext
from aurora.runtime.messages import Message, Role
from aurora.runtime.session import Session, SessionManager


def _msg(role: Role, content: str) -> Message:
    return Message(role=role, content=content)


def test_create_session():
    mgr = SessionManager()
    s = mgr.create("窗口A")
    assert s.session_id
    assert s.name == "窗口A"
    assert s in mgr.sessions()


def test_auto_generated_name():
    mgr = SessionManager()
    s1 = mgr.create()
    s2 = mgr.create()
    assert s1.name != s2.name


def test_sessions_independent_history():
    mgr = SessionManager()
    a = mgr.create("A")
    b = mgr.create("B")
    a.context.append(_msg(Role.USER, "A 的问题"))
    assert "A 的问题" not in [m.content for m in b.context.build_messages()]


def test_get_and_missing():
    mgr = SessionManager()
    s = mgr.create("A")
    assert mgr.get(s.session_id) is s
    with pytest.raises(KeyError):
        mgr.get("missing")


def test_remove():
    mgr = SessionManager()
    s = mgr.create("A")
    mgr.remove(s.session_id)
    assert len(mgr.sessions()) == 0


def test_remove_missing_ignored():
    mgr = SessionManager()
    mgr.remove("missing")  # 不抛错


def test_session_default_context():
    s = Session("default")
    assert isinstance(s.context, BucketedContext)


def test_session_resume_keeps_state():
    mgr = SessionManager()
    s = mgr.create("恢复")
    s.context.append(_msg(Role.USER, "记得这个"))
    # 通过 manager 重新取到同一对象 → 状态仍在
    s2 = mgr.get(s.session_id)
    texts = [m.content for m in s2.context.build_messages()]
    assert "记得这个" in texts


def test_async_manager_create():
    async def main():
        mgr = SessionManager()
        s = await mgr.acreate("异步")
        return s.name

    assert asyncio.run(main()) == "异步"


def test_session_names_unique():
    mgr = SessionManager()
    mgr.create("同名")
    mgr.create("同名")  # 允许同名？应保证唯一名
    names = [s.name for s in mgr.sessions()]
    assert len(names) == len(set(names))


def test_session_name_fills_gap_after_remove():
    """删掉 #2 后再建同名，应占 #2 而不是 #4（_unique_name 关键行为）。

    这正是 #115 「刷新一次序号 +1」的前置防线：保证后端不会无限递增。
    """
    mgr = SessionManager()
    a = mgr.create("窗口")
    b = mgr.create("窗口")                 # → 窗口#2
    _ = mgr.create("窗口")                 # → 窗口#3
    mgr.remove(b.session_id)                # 删 #2
    c = mgr.create("窗口")
    assert c.name == "窗口#2"              # 占空位，序号不增长


def test_session_name_no_gap_keeps_incrementing():
    """无空位时正常递增到下一个 #N（前置条件）。"""
    mgr = SessionManager()
    mgr.create("窗口")
    mgr.create("窗口")
    c = mgr.create("窗口")
    assert c.name == "窗口#3"