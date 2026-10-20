"""工单 035 验收 3 的永久版：拒绝词表是一个**封闭集合**，不是一族各自维持一致的字符串。

这里的断言全部**表驱动**——期望值来自两个独立真相源：枚举手自己的成员、以及
`tests/guard/corpus.py` 里那批必拒语料的期望 `rule_id`（它是对外契约，工单 003 起就在，条数随语料涨）。
本文件一处都没有把码名再抄一遍，所以 036/037 往枚举里加新码时只需要动枚举与语料两处。
"""

from __future__ import annotations

import ast
import json
import re
from enum import Enum
from pathlib import Path

import pytest

from app.services import sql_guard
from app.services.sql_guard import GuardRuleId, SqlGuardError
from tests.guard.corpus import DEMO_TABLES, REJECT_CASES

_EXPECTED_VALUES = {rule for _, rule in REJECT_CASES}


def test_语料期望值全是枚举成员且每枚成员都被语料点过名() -> None:
    """双向无孤儿：一枚没有语料钉住的码 = 那条拒绝分支今天没人证过；
    语料里一枚不在枚举的期望值 = 有人写了裸字符串。两种都当场红。"""
    members = {m.value for m in GuardRuleId}
    assert _EXPECTED_VALUES - members == set()
    assert members - _EXPECTED_VALUES == set()


def test_枚举手是str子类且成员转成字符串出的是值不是成员名() -> None:
    """3.12 的 `Enum.__str__` 会让 `f"[{rule_id}] {message}"` 变成
    `'[GuardRuleId.multi_statement] …'`——那是 `SqlGuardError` 的正文与 `chat_messages`
    里用户可见的留痕。纯类型重构一不留意就顺手改掉文案，所以这条单独钉住。"""
    assert issubclass(GuardRuleId, str)
    for member in GuardRuleId:
        assert str(member) == member.value
        assert f"{member}" == member.value
        assert f"[{member}]" == f"[{member.value}]"


def test_落库形状不变_json序列化出的还是那枚值() -> None:
    """`guard_result` 是 JSONB，今天存的就是裸码；本片只改类型，不改那一列的字节。"""
    assert json.dumps({"code": GuardRuleId.multi_statement}) == '{"code": "multi_statement"}'
    error = SqlGuardError(GuardRuleId.multi_statement, "一次只允许一条语句")
    assert error.violation.code == "multi_statement"
    assert str(error) == "[multi_statement] 一次只允许一条语句"


def test_成员名就是对外值所以改名必然被看见() -> None:
    """值域是接口不是内部名。要求 `value == name`，等于让"重命名成员"这件事本身
    撞上上面那条与语料的双向断言，不可能悄悄改到对外值。"""
    for member in GuardRuleId:
        assert member.value == member.name
        assert re.fullmatch(r"[a-z][a-z0-9_]*", member.value), member


def test_守卫源码里每一个拒绝点的首参都是枚举成员() -> None:
    """机械检查：`SqlGuardError(...)` 与 `Violation(...)` 的首参**不许是裸字符串**。
    这条是"以后有人新写一支 `SqlGuardError("unsupported_construct", …)`"的唯一防线——
    mypy 管不到字符串本身，也管不到不在它覆盖范围内的写法。"""
    tree = ast.parse(Path(sql_guard.__file__).read_text(encoding="utf-8"))
    bare = [
        (node.lineno, node.func.id, node.args[0].value)  # type: ignore[union-attr]
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"SqlGuardError", "Violation"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    ]
    assert bare == []


def test_码的字面量在源码里只出现在枚举手自己身上() -> None:
    """上一条只看调用点首参，这一条把 `_scan_raw_text` 那张 `(pattern, code, hint)` 三元组表
    也管住：三元组里的 `code` 是 `SqlGuardError(code, …)` 的来源，它若还是裸字符串，
    封闭集合就在自己家里漏了一格。全文件扫一遍，任何等于某枚码的字符串字面量都必须
    住在 `GuardRuleId` 的类体内——想再维护一份平行清单也维护不了。"""
    tree = ast.parse(Path(sql_guard.__file__).read_text(encoding="utf-8"))
    enum_lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "GuardRuleId":
            assert node.end_lineno is not None
            enum_lines = set(range(node.lineno, node.end_lineno + 1))
    assert enum_lines, "sql_guard.py 里没有 GuardRuleId 这个类"

    values = {m.value for m in GuardRuleId}
    leaked = [
        (node.lineno, node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and node.value in values
        and node.lineno not in enum_lines
    ]
    assert leaked == []


def test_抛与不抛两条接缝共用同一枚成员() -> None:
    """`check()` 命中即抛、`guard()` 收集全部违规（工单 003 定的两条接缝）。
    同一支 SQL 在两条接缝上必须给出**同一个成员**，否则归因统计会分裂成两套。"""
    sql = next(s for s, r in REJECT_CASES if r == "multi_statement")
    kwargs: dict[str, object] = {
        "allowed": DEMO_TABLES,
        "dialect": "mysql",
        "default_schema": "ai_web_demo",
    }
    with pytest.raises(SqlGuardError) as ei:
        sql_guard.check(sql, **kwargs)  # type: ignore[arg-type]
    assert isinstance(ei.value.rule_id, GuardRuleId)
    assert ei.value.rule_id is GuardRuleId.multi_statement

    collected = sql_guard.guard(sql, **kwargs)  # type: ignore[arg-type]
    assert isinstance(collected.violations[0].code, GuardRuleId)
    assert collected.violations[0].code is GuardRuleId.multi_statement


def test_词表可穷举且能按值反查() -> None:
    """防一种偷懒写法：`multi_statement: ClassVar[str] = "…"` 也能过前面几条文本层面的断言，
    但它不可穷举、不能反查——"这套系统一共有哪些拒绝原因"那个问题依旧没有答案。"""
    assert issubclass(GuardRuleId, Enum)
    assert GuardRuleId("multi_statement") is GuardRuleId.multi_statement
    assert len(list(GuardRuleId)) == len(_EXPECTED_VALUES)
