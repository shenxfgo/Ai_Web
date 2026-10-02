"""实现切换缝的三格机械检查（工单 031 验收 5，spec 故事 21）。

这一片真正怕的不是"融合算错"，而是**缝长歪**：向量概念（实现类名、`ef_search`、维度字面）一旦
渗进调用方，`pipeline` 就得按实现分叉，而 P2 那句"换实现只改一行"变成"换实现要改三处，漏一处
仍然绿"。这种债靠肉眼读代码是抓不住的——它此刻什么都不坏，所以什么都不报。

期望值口径：工单 031 验收 5 的字面（"`grep` 证明 `pipeline.py` 与 `search_preview()` 里没有任何
`Hybrid`/`ef_search`/维度字面"）。这里扫的是 **AST 里的引用**而不是文本子串，因为文本分不开
"注释里提一句"和"代码里 new 一个"：`pipeline.py:191` 那条注释写的正是"P4 要换 Hybrid"，按子串
扫它当场红，而红的是一条**正确**的注释——这种红会把用例训练成被绕过的对象。

一条对工单字面的偏离摆在明处（同时进交付记录）：工单写的是"换 `_RETRIEVER` 赋值"，而 P4 之后
同一份实现要按请求带 `ef_search`，模块级单例装不下，所以那处赋值换成了工厂函数
`_retriever_for()`。"只改一行"仍然成立——改的是该函数体里那一行构造，第三条用例钉的就是
"全仓这样的构造点只有一处"。
"""

from __future__ import annotations

import ast
import inspect
import pathlib

from app.services.nl2sql.retriever import HybridRetriever, LikeRetriever, Retriever

BACKEND = pathlib.Path(__file__).resolve().parents[2]
NL2SQL = BACKEND / "app" / "services" / "nl2sql"
PIPELINE_PATH = NL2SQL / "pipeline.py"
RETRIEVER_PATH = NL2SQL / "retriever.py"

# 实现名与端点名：它们只该住在 `retriever.py` 内部，出现在调用方就是分叉的开始。
_BANNED_NAMES: frozenset[str] = frozenset({"HybridRetriever", "LikeRetriever", "EmbeddingClient"})
# 三个旋钮的形参名 + 裁决的输入与函数：越过缝渗进调用方，等于调用方自己也在裁决。
_BANNED_KEYWORDS: frozenset[str] = frozenset(
    {"ef_search", "top_vector", "rrf_k", "mode", "vector_ready", "tuning"}
)
# 两根预置列宽的维度字面（0005 与 0008 的事实）：调用方写出它 = 调用方自己在选列。
_BANNED_INTS: frozenset[int] = frozenset({1536, 1024})


def _parse(path: pathlib.Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _referenced(node: ast.AST) -> tuple[set[str], set[str], set[int]]:
    """一段语法树里被**引用**到的三类东西：名字（含属性与导入项）、关键字形参、整型字面量。

    不区分名字出现的位置（形参注解、赋值目标、调用名都算）——对"不该认识它的模块"来说，
    出现即越界。
    """
    names: set[str] = set()
    keywords: set[str] = set()
    ints: set[int] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Attribute):
            names.add(sub.attr)
        elif isinstance(sub, ast.ImportFrom):
            names.update(alias.name for alias in sub.names)
        elif isinstance(sub, ast.keyword) and sub.arg is not None:
            keywords.add(sub.arg)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, int):
            ints.add(sub.value)
    return names, keywords, ints


def _assert_free_of_vector_concepts(node: ast.AST, *, where: str) -> None:
    names, keywords, ints = _referenced(node)
    for banned, found, label in (
        (_BANNED_NAMES, names, "实现/端点名"),
        (_BANNED_KEYWORDS, keywords, "旋钮形参"),
        (_BANNED_INTS, ints, "维度字面"),
    ):
        hit = sorted(banned & found, key=str)
        assert not hit, f"{where} 出现了{label}：{hit}"


def _function(name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    """从 `retriever.py` 的树里取一个函数定义节点（拿不到就是它改名或搬走了）。

    `search_preview` 与 `_retriever_for` 都是 `async def`，所以两种节点都得认——只写
    `FunctionDef` 的话，这条用例会红成"函数不存在"，而它不存在的原因是它是异步的。
    """
    tree = _parse(RETRIEVER_PATH)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"retriever.py 里没有函数 `{name}`——缝挪位置了")


def test_两条实现的签名与协议一字不差() -> None:
    """故事 21 的正面：签名里不许出现向量概念，否则调用方就得认识它们。

    比 `inspect.signature` 而不是比注解字符串：加一个 `ef_search: int = 64` 形参是最"顺手"的
    错法，它当场改变签名而什么都不报错——直到 P8 的端点开始按实现分叉。
    """
    protocol = inspect.signature(Retriever.search)
    for impl in (LikeRetriever, HybridRetriever):
        signature = inspect.signature(impl.search)
        assert str(signature) == str(protocol), f"{impl.__name__} 的签名漂了：{signature}"
    for name in ("ef_search", "top_vector", "dimension", "mode", "rrf_k"):
        assert name not in str(protocol), f"协议里出现了向量概念：{name}"


def test_实现字面不出现在调用方() -> None:
    """验收 5 的字面半边：`pipeline` 整模块与 `search_preview` 函数体都读不到向量概念。

    两处分开扫，射程不一样：`pipeline.py` 是**编排层**（它收的应该是注入进来的实现），
    而 `search_preview()` 是端点侧唯一挨着接缝的函数——它一旦自己 new 实现，
    "换实现只改一行"当场作废。
    """
    pipeline_tree = _parse(PIPELINE_PATH)
    _assert_free_of_vector_concepts(pipeline_tree, where="pipeline.py")
    # 反证"没扫到"不是"没扫"：pipeline 确实认识协议名 `Retriever`，扫描器抓得到它。
    assert "Retriever" in _referenced(pipeline_tree)[0], "扫描器在空转"
    preview = _function("search_preview")
    _assert_free_of_vector_concepts(preview, where="search_preview()")
    # 反向那一格：它确实走的是缝，而不是绕开缝自己拼了两条腿。
    assert "_retriever_for" in _referenced(preview)[0], "search_preview 没有经由缝取实现"


def test_全仓构造向量化实现的语句只有一处() -> None:
    """验收 5 的另一半：`HybridRetriever(...)` 的**构造点**在 `app/` 下只有一个，且就在缝里。

    这是"一行"这两个字唯一能被机械兑现的写法。类定义那一行不算（`class HybridRetriever:`
    不是 Call），文档串里提到的那些也不算——所以这里必须走 AST 而不是 `grep -c`。

    只数 `HybridRetriever`：`HybridRetriever.search` 内部那句 `LikeRetriever()` 是**降级分支
    复用同一份实现**（故事 26 的"逐字同构"靠它成立），不是第二处"选实现"。

    `scripts/demo_ask.py` 里那句 `LikeRetriever()` 在射程之外：那条 CLI 是 P2 交付的 L1 路径，
    把它的默认实现换成 hybrid 属于 034 的收口口径（现在换会同时改掉 P2 验收 5 的靶），
    已记进工单交付记录。
    """
    factory = _function("_retriever_for")
    sites: list[tuple[str, int]] = []
    for path in sorted((BACKEND / "app").rglob("*.py")):
        for node in ast.walk(_parse(path)):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "HybridRetriever"
            ):
                sites.append((path.relative_to(BACKEND).as_posix(), node.lineno))
    assert len(sites) == 1, f"构造点有 {len(sites)} 处：{sites}"
    name, lineno = sites[0]
    # 断言"落在缝的函数体内"而不是"落在某一行"：行号会随手抖，函数体不会。
    assert name == "app/services/nl2sql/retriever.py"
    assert factory.lineno <= lineno <= factory.end_lineno, f"{name}:{lineno} 不在缝里"
