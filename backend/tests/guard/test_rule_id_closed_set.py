"""工单 035 验收 3 的永久版：拒绝词表是一个**封闭集合**，不是一族各自维持一致的字符串。

这里的断言全部**表驱动**——期望值来自两个独立真相源：枚举手自己的成员、以及"谁把这枚码钉住了"
（`tests/guard/corpus.py` 里必拒语料的期望 `rule_id`，加上 `tests/guard/` 各用例文件对
`GuardRuleId.<成员>` 的属性引用）。本文件一处都没有把码名再抄成一份清单，所以 036/037 往枚举里
加新码时只需要动枚举与**钉住它的那一处**（语料或用例）。

> **as-built(P5-036)：判据从"必须是语料"放宽成"语料或用例"**，因为出现了第一枚**不可能有语料**的码
> `regeneration_drift`：它防的是守卫自己的代码，今天没有任何一支输入能触发它
> （实测 18 条必放行语料漂移候选 0）。硬往 `REJECT_CASES` 塞一条，要么是一条永远走不到的死条目，
> 要么得靠 monkeypatch 才过——那是伪造证据。放宽的**代价要说清**：属性引用只证明
> "有用例点了这枚码的名"，不证明那条分支真被走过；
> 所以这一档的码要另配一条**失败路径用例**（`test_regeneration_invariant.py` 里的注入那一发就是），
> 光被点名不算钉住。语料那一档仍然保持 035 的强度：期望值是被 `test_reject_corpus` 逐条断言的。
>
> **同片收尾时这一档被补严过**（双轴审查指出第一版的空洞）：① 只认**断言里**的属性引用，
> 写在注释／docstring／游离赋值里的点名不算；② 扫描**不预先过滤成员名**——第一版那句
> `node.attr in members` 让"拼错或删了成员名"这一类在构造上永远红不了，现在非成员的名字由
> `test_用例点名的成员全在枚举里` 单独红。两道都不改变"值域不可穷举即红"的强度，只是把
> "点过名"这个词的定义收窄到它本来该指的东西。
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

_CORPUS_VALUES = {rule for _, rule in REJECT_CASES}


def _referenced_in_asserts() -> set[str]:
    """`tests/guard/` 每一个 `GuardRuleId.<成员>` 属性引用，**只看断言里那一些**。

    两个收窄都是机械判据自己要的强度：
    ① 只认 `ast.Assert` 里的引用——写在注释、docstring 或一句游离赋值里的点名不算钉住，
       钉住的意思是"有一条永久用例断到它"；
    ② **不预先过滤成员名**（036 的双轴审查指出的洞：一旦写成 `node.attr in members`，
       这个集合在构造上就不可能产出非成员值，"点到名却不在枚举"那半句判据就成了空话）。
       这里原样收属性名的字符串，非成员值留给下面那条双向断言去红。
    """
    found: set[str] = set()
    for path in sorted(Path(__file__).parent.glob("test_*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for stmt in ast.walk(tree):
            if not isinstance(stmt, ast.Assert):
                continue
            for node in ast.walk(stmt):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id == "GuardRuleId"
                ):
                    found.add(node.attr)
    return found


_REFERENCED = _referenced_in_asserts()
_MEMBERS = {e.name for e in GuardRuleId}
# 只有真的是成员的那些名字，才参与"这枚码有没有钉住处"的计算；非成员的名字由上面那条单独管。
_PINNED_VALUES = _CORPUS_VALUES | {
    getattr(GuardRuleId, name).value for name in _REFERENCED & _MEMBERS
}


def test_用例点名的成员全在枚举里() -> None:
    """上面那条双向断言管不到的一种形状：用例点了一枚**不存在**的成员名。

    属性引用是运行时才炸的（`AttributeError`），而这里要的是**归因**——"有人删了成员、
    或把成员名拼错了"必须红在本用例上，而不是红在那条恰好用到它的用例的导入期。
    """
    members = {e.name for e in GuardRuleId}
    assert _REFERENCED - members == set()


def test_每枚成员都有钉住处且钉住处都是真码() -> None:
    """双向无孤儿：一枚既没语料也没用例钉住的码 = 那条拒绝分支今天没人证过；
    钉住处里出现不在枚举的值 = 有人写了裸字符串或删了成员。两种都当场红。

    钉住处可以是语料期望值（035 的强度：被 `test_reject_corpus` 逐条断言），
    也可以是用例断言里的属性引用（036 起新增的那一档，理由与本文件开头那条 as-built 注）。
    """
    members = {m.value for m in GuardRuleId}
    assert _PINNED_VALUES - members == set()
    assert members - _PINNED_VALUES == set()


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
    assert len(list(GuardRuleId)) == len(_PINNED_VALUES)
