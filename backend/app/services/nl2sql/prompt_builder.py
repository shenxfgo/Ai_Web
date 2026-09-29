"""问数 prompt 组装（architecture §4.1 的 ④ 与 ⑨，工单 010 / 012）。

纯函数：输入是构造出来的卡片对象，不碰 DB、不碰凭据，输出直接就是 `llm_client` 吃的 messages。
④ 的段落顺序出自 §4.1：schema 卡片 → JOIN → 术语 → few-shot → 问题 → 输出格式
（角色与硬约束在 system，见 `nl2sql_system.j2`）。
⑨ 的素材出自同一节的"≤50 行喂 markdown 表；更多行只喂摘要"，住在这一层是因为
**"给模型看哪些行"和"给模型看哪些列"是同一种判断**——都由预算与意图决定，都不该上抬到编排层。

为什么这两句硬约束值得被逐字钉死：少了"只能引用给定的表"，模型会去 JOIN 一张没给的表，
守卫按表白名单拒；少了"必须带 LIMIT"，守卫会自己补一条 LIMIT——但补出来的行数上限来自配置，
不是模型按问题意图选的量。两种失效都不报错，只是答案变差或变慢。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Final

from jinja2 import Environment, FileSystemLoader

from app.services.chart_advisor import is_number, numeric_column_slots
from app.services.token_estimate import estimate_tokens

logger = logging.getLogger(__name__)

PROMPT_DIR: Final = Path(__file__).resolve().parents[2] / "prompts"

_ENV: Final = Environment(
    loader=FileSystemLoader(str(PROMPT_DIR)),
    trim_blocks=True,
    lstrip_blocks=True,
    autoescape=False,
    keep_trailing_newline=False,
)

# §5.3："只有 ≥0.8 才进 prompt"——辖区是【可 JOIN】这份可执行边清单，不是"整份 prompt 里不许出现"：
# 卡片全文的【可关联】行仍会带上同一条低置信边（008 的渲染器不看 confidence，而 010 的口径是
# 卡片原文进 prompt、装配层不做二次加工）。写侧今天最低给 0.85，所以这道门槛筛的是 007 时代
# 按常数 0.7 落库的历史行。
_INFERRED_PROMPT_FLOOR: Final = 0.8


@dataclass(frozen=True)
class RelationEdge:
    """一条关联边的 prompt 视角：只要渲染与门槛判定要用的那五个字段。

    刻意不复用 `kb_service.RelationMeta`（卡片渲染用那个）：本模块是**零 DB 依赖的叶子**——
    单测构造这些 dataclass 就能跑完整装配，import 卡片层会把 sqlalchemy 一起拖进来。
    """

    from_column: str
    to_table_full: str
    to_column: str
    kind: str = "extracted"
    confidence: float | None = None


@dataclass(frozen=True)
class SchemaTable:
    """一张表的 prompt 素材：卡片段（主卡在前、列切片按 `seq` 升序）+ 它参与的关联边。

    段而不是一整篇文本，是因为预算裁切要能**只丢切片、留主卡**——008 的主卡带着表头和全部
    PK/索引/外键列，丢了切片"这张表叫什么、主键是谁"还在，丢了整张表就什么都没了。
    """

    full_name: str
    segments: tuple[str, ...] = ()
    relations: tuple[RelationEdge, ...] = ()


@dataclass(frozen=True)
class Term:
    term: str
    definition: str
    synonyms: tuple[str, ...] = ()


@dataclass(frozen=True)
class Example:
    question: str
    sql: str


@dataclass(frozen=True)
class _JoinLine:
    from_table: str
    from_column: str
    to_table_full: str
    to_column: str
    confidence: float | None
    inferred: bool


def _join_lines(tables: Sequence[SchemaTable]) -> list[_JoinLine]:
    """候选表之间的**直连边**（010 的范围）；BFS/桥表扩展归工单 014。

    只渲染从候选表出发的一跳：目标表即使没被检索到，它的名字也已经出现在源表的卡片全文
    【可关联】段里，所以"只能引用给定的表"这句约束不会被这条渲染破坏。
    """
    lines: list[_JoinLine] = []
    for table in tables:
        for rel in table.relations:
            if rel.kind == "inferred" and (rel.confidence or 0) < _INFERRED_PROMPT_FLOOR:
                continue
            lines.append(
                _JoinLine(
                    from_table=table.full_name,
                    from_column=rel.from_column,
                    to_table_full=rel.to_table_full,
                    to_column=rel.to_column,
                    confidence=rel.confidence,
                    inferred=rel.kind == "inferred",
                )
            )
    return lines


def _cut_tables(
    tables: Sequence[SchemaTable], budget: int
) -> tuple[list[SchemaTable], list[str], int]:
    """按**卡片段**贪心装填，返回（保留的表，被整张丢弃的表名，已用 token）。

    裁的粒度是段而不是字符：008 的主卡带着表头和全部 PK/索引/外键列，切片只是第 26 列往后的
    补充，所以"保住表名与主键列"这句话只在段这一层成立。
    装不下主卡才算整张丢弃——整张丢掉的表对模型就是"不存在"，比半张表更容易写出错 SQL。
    """
    kept: list[SchemaTable] = []
    dropped: list[str] = []
    used = 0
    for table in tables:
        segments: list[str] = []
        for segment in table.segments:
            cost = estimate_tokens(segment)
            if used + cost > budget:
                break
            used += cost
            segments.append(segment)
        if not segments:
            dropped.append(table.full_name)
            continue
        kept.append(replace(table, segments=tuple(segments)))
    return kept, dropped, used


def _term_line(term: Term) -> str:
    """术语行的渲染形状——预算必须按渲染后的那一行算，估算式偏离模板就等于没算。
    形状一致性由 `test_术语与示例的行形状与模板渲染同源` 钉住（改模板会红，不会悄悄算错账）。"""
    alias = f"（同义词：{'、'.join(term.synonyms)}）" if term.synonyms else ""
    return f"- {term.term}{alias}: {term.definition}"


def _cut_terms(terms: Sequence[Term], remaining: int) -> tuple[list[Term], int]:
    """术语按条整条装填，超出的**整条丢掉**——半条口径（"GMV = 已支付订单…"）比没有更危险。"""
    kept: list[Term] = []
    used = 0
    for term in terms:
        cost = estimate_tokens(_term_line(term))
        if used + cost > remaining:
            break
        used += cost
        kept.append(term)
    return kept, used


def _example_line(example: Example) -> str:
    """同 `_term_line`：一条示例渲染成两行，预算就按这两行算。"""
    return f"问：{example.question}\nSQL：{example.sql}"


def _cut_examples(
    examples: Sequence[Example], remaining: int, few_shot_budget: int
) -> list[Example]:
    """few-shot 受两个上限夹：全局剩余与 `FEW_SHOT_TOKEN_BUDGET`，且**整段丢弃而不是半条**。

    "半条"在 §4.1 ④ 这里指一条示例被截断——一条截断的示例会把错误的写法当范本教给模型，
    比完全没有示例更糟，所以装不下就整条不装，一条都装不下就整段不留。
    """
    cap = min(remaining, few_shot_budget)
    kept: list[Example] = []
    used = 0
    for example in examples:
        cost = estimate_tokens(_example_line(example))
        if used + cost > cap:
            break
        used += cost
        kept.append(example)
    return kept


def build_prompt(
    *,
    question: str,
    tables: Sequence[SchemaTable],
    terms: Sequence[Term] = (),
    examples: Sequence[Example] = (),
    row_limit: int = 1000,
    token_budget: int | None = None,
    few_shot_budget: int = 1500,
) -> list[dict[str, Any]]:
    """产出 `[{system}, {user}]`，可直接送 `llm_client`。

    `token_budget=None` 时不裁切（单测与调试用）；传数就是 `AIWEB_RETRIEVAL__TOKEN_BUDGET`，
    管的是**素材段**（schema → 术语 → 示例）——问题、输出格式和 system 里的硬约束是固定开销，
    由 roadmap §P2 那句"与 LLM max_output 之和留出余量"负责，不参与竞争。
    """
    kept_tables: list[SchemaTable] = list(tables)
    kept_terms: list[Term] = list(terms)
    kept_examples: list[Example] = list(examples)
    if token_budget is not None:
        kept_tables, dropped, used = _cut_tables(kept_tables, token_budget)
        if dropped:
            logger.info("prompt 预算裁切：丢弃表 %s", "、".join(dropped))
        kept_terms, term_used = _cut_terms(kept_terms, token_budget - used)
        left_for_examples = token_budget - used - term_used
        kept_examples = _cut_examples(kept_examples, left_for_examples, few_shot_budget)

    return [
        {"role": "system", "content": _ENV.get_template("nl2sql_system.j2").render()},
        {
            "role": "user",
            "content": _ENV.get_template("nl2sql_user.j2").render(
                tables=kept_tables,
                joins=_join_lines(kept_tables),
                terms=kept_terms,
                examples=kept_examples,
                question=question,
                row_limit=row_limit,
            ),
        },
    ]


# ============================================================ ⑨ 结论生成的素材


_MAX_ROWS_IN_PROMPT: Final = 50
"""§4.1 ⑨ 的原文阈值：≤50 行喂整表，超过就只喂摘要——68 列宽表全量进 prompt 会先把
上下文吃掉，而结论要的是"数出来的是什么"，不是每一行。"""

_TOP_BOTTOM: Final = 5
"""§4.1 ⑨ 的"top/bottom 5"：摘要里保留的头尾行数。"""


def _markdown_table(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """结果集 → markdown 表。空格与竖线形状是给模型看的，不是给人看的，所以不做对齐。"""
    out = ["| " + " | ".join(columns) + " |", "| " + " | ".join("---" for _ in columns) + " |"]
    out += ["| " + " | ".join("" if v is None else str(v) for v in row) + " |" for row in rows]
    return "\n".join(out)


def _cell(row: Sequence[Any], index: int) -> Any:
    """按**下标**取一格：列名可以重复（`SELECT a, a`），下标不会。行比列短时按空处理。"""
    return row[index] if index < len(row) else None


def _as_float(value: Any) -> float | None:
    """序列化后的数值格可能是字符串（safety §4.3），也可能是被截出来的空位。"""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    return float(value) if is_number(value) else None


def _result_summary(columns: Sequence[str], rows: Sequence[Sequence[Any]]) -> str:
    """摘要分支：每个数值列的 sum/avg/min/max + 按最后一个数值列排的头尾各 5 行。

    排序键取**最后一个**数值列，因为 SQL 的习惯是把度量放在维度之后
    （`SELECT 月份, SUM(金额)`），取第一列会在 `SELECT 金额, 月份` 这种少数写法上排错。
    """
    measures = numeric_column_slots(columns, rows)
    lines = [f"（结果共 {len(rows)} 行，只给摘要，未列出的行不在这里）", ""]
    lines += ["| 数值列 | 合计 | 均值 | 最小 | 最大 |", "| --- | --- | --- | --- | --- |"]
    for index, name in measures:
        values = [v for v in (_as_float(_cell(row, index)) for row in rows) if v is not None]
        if not values:
            continue
        lines.append(
            f"| {name} | {sum(values):.2f} | {sum(values) / len(values):.2f}"
            f" | {min(values):.2f} | {max(values):.2f} |"
        )
    if measures:
        key_index, key_name = measures[-1]
        # 空值排到最后：摘要里的头尾是"最值的头尾"，一格空的行既不该占榜首也不该占榜尾。
        ordered = sorted(
            rows,
            key=lambda row: (
                _as_float(_cell(row, key_index)) is None,
                -(_as_float(_cell(row, key_index)) or 0.0),
            ),
        )
        lines += [
            "",
            f"按 {key_name} 取前 {_TOP_BOTTOM} 行：",
            _markdown_table(columns, ordered[:_TOP_BOTTOM]),
            "",
            f"按 {key_name} 取后 {_TOP_BOTTOM} 行：",
            _markdown_table(columns, ordered[-_TOP_BOTTOM:]),
        ]
    return "\n".join(lines)


def result_brief(
    *,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    token_budget: int | None = None,
) -> str:
    """结果集 → 进 ⑨ prompt 的那段素材（§4.1 ⑨ 的两条分支）。

    分支只按行数判：≤50 行喂整表（"一行都不许少"），超过喂摘要（`_result_summary`）。
    这是 §4.1 ⑨ 原文的形状，不因预算改道——把"装不下就换摘要"当兜底是会反噬的：
    摘要要开一张 stats 表加两张头尾表，**小窄结果的摘要比整表更长**，换过去等于把
    "超预算"变成"更超预算"。

    `token_budget` 承担的是 verification §2.1 的 `token_estimate` ⑥（"摘要路径的预算
    ≤TOKEN_BUDGET"）：算一遍选中素材的体量，**超了就告警不改道**——语义与 010 那条
    "被裁掉的表要 logger.info 记下"同一条（P4 验收 6：不报错但要可查）。真按预算裁行的
    那一刀要等"给模型看哪些行"有口径之后，属引依赖的那一档（roadmap §P4）。
    传数就是 `Settings.retrieval.token_budget`，`None`（与 `build_prompt` 同一套默认值口径）
    意为不看预算。
    """
    brief = (
        _markdown_table(columns, rows)
        if len(rows) <= _MAX_ROWS_IN_PROMPT
        else _result_summary(columns, rows)
    )
    if token_budget is not None:
        used = estimate_tokens(brief)
        if used > token_budget:
            logger.warning(
                "⑨ 的素材超出预算：%d > %d（分支=%s 行数=%d 列数=%d）",
                used,
                token_budget,
                "整表" if len(rows) <= _MAX_ROWS_IN_PROMPT else "摘要",
                len(rows),
                len(columns),
            )
    return brief


def build_conclusion_prompt(
    *,
    question: str,
    columns: Sequence[str],
    rows: Sequence[Sequence[Any]],
    row_count: int,
    truncated: bool,
    token_budget: int | None = None,
) -> list[dict[str, Any]]:
    """⑨ 结论生成的 messages：素材是 `result_brief` 的结果，不是整张结果表。

    这里的 `row_count` 与 `len(rows)` 可以不相等（executor 丢过探针行、也截过断），
    所以两个都传：模板里那句"共 N 行"要说的是真实行数。
    """
    return [
        {"role": "system", "content": _ENV.get_template("conclusion_system.j2").render()},
        {
            "role": "user",
            "content": _ENV.get_template("conclusion_user.j2").render(
                question=question,
                row_count=row_count,
                truncated=truncated,
                brief=result_brief(columns=columns, rows=rows, token_budget=token_budget),
            ),
        },
    ]
