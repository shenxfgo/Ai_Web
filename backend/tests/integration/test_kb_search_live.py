"""真连演示库问一句：工单 009 的验收 1/2 落在这里。

为什么必须 live：这两条钉的都是**真注释经过真抽取、真渲染成卡片、再被切词命中**之后的次序——
"order_main 排第一"依赖建库脚本里那句 `订单金额（元，优惠前）` 真的走到了 `search_text`，
任何一层的桩都会把"注释没到达"这种失败伪装成"检索算法不行"。

期望值口径：
- 工单 009 验收 ①②（问句与首选表直接抄自文档）
- `docs/verification.md` §1：演示库 10 个业务对象、列注释全中文
- `docs/architecture.md` §5.1 L1：排序主键是**命中的不同词数**，命中列数降为次键
  （as-built 0009 的口径修正，理由见 §5.1 那条注）

活体管线（登记源 → 打同步 → 卡片落库）直接复用 007/008 的助手：凭证只从 gitignored 的
`.setup/aiweb_ro.cnf` 读，复制一份只会多一个泄露口令的地方。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

import pytest
from httpx import AsyncClient

from tests.integration.conftest import Account, DbAccount
from tests.integration.test_sync_live import SyncRun, _sync_once, _trigger

pytestmark = [pytest.mark.pg, pytest.mark.live]

Login = Callable[..., Awaitable[Account]]


async def _search_once(
    client: AsyncClient, run: SyncRun, query: str, **extra: Any
) -> list[dict[str, Any]]:
    """跑一次检索预览，把候选表条目按次序带回来（断言读表名比读 md5 的 uid 好看得多）。

    刻意不叫"问一句/ask"：本仓库的术语表里 **问数** 专指"提问→SQL→执行→结果→结论"的整条往返
    （CONTEXT.md），这里只走检索这一小截。
    """
    resp = await client.post(
        "/api/kb/search",
        json={"query": query, "datasource_ids": [run.ds_id], **extra},
        headers=run.acct.headers,
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


def _names(items: list[dict[str, Any]]) -> list[str]:
    return [_table_name(item) for item in items]


def _item(items: list[dict[str, Any]], table: str) -> dict[str, Any]:
    """按表名取一条候选，取不到就直接失败——`next(..., None)` 会把"没召回"写成 TypeError。"""
    for item in items:
        if _table_name(item) == table:
            return item
    raise AssertionError(f"{table} 不在候选里：{_names(items)}")


def _table_name(item: dict[str, Any]) -> str:
    """从 `ai_web_demo.order_main（订单主表…）` 里剥出 `order_main`。"""
    full = str(item["title"]).split("（")[0]
    return full.split(".")[-1]


async def test_验收1_问订单总金额_order_main排第一且payment_record进前5(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
) -> None:
    """§5.1 L1 的"命中词数优先"次序在真语料上成立吗。

    问句切出的词里能对上注释的是 `订单` 和 `金额` 两个。
    `order_main` 两个词都命中且落在 5 列上（id/order_no/status/amount/discount），
    `payment_record` 也是两个词、3 列——所以它排在 order_main 之后但照样进前 5。
    68 列的 `product_stats_wide` 有 10 列的注释带 `金额`，命中列数是全场最高的，
    但它只对上一个词：这正是旧口径（主键=命中列数）把它顶到头名、验收 ① 当场破的地方。
    """
    run = await _sync_once(client, login, monkeypatch, account)
    items = await _search_once(client, run, "2024 年每个月的订单总金额", k=5)
    names = _names(items)

    assert names[0] == "order_main", names
    assert "payment_record" in names[:5], names
    # 数字全部对着 `backend/scripts/init_demo_mysql.sql` 里的注释原文数出来的：
    # order_main 的 id/order_no/status 带`订单`、amount/discount 带`金额`；
    # payment_record 的 order_id/paid_at 带`订单`、amount 带`金额`。
    order_main = _item(items, "order_main")
    payment = _item(items, "payment_record")
    assert (order_main["matched_term_count"], order_main["matched_column_count"]) == (2, 5)
    assert (payment["matched_term_count"], payment["matched_column_count"]) == (2, 3)
    # 旧口径下就是这张 10 列的宽表占了头名：它对上的只有`金额`一个词
    wide = _item(items, "product_stats_wide")
    assert (wide["matched_term_count"], wide["matched_column_count"]) == (1, 10)
    assert names.index("product_stats_wide") > names.index("payment_record"), names


async def test_验收2_问客户手机号_命中列与注释字段说得清是谁(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
) -> None:
    """`retrieved` 字段要能给人看，所以命中理由必须落到**具体哪一列的哪个字段**。

    `customer` 排第一靠的是三列都带"客户"或"手机号"：`id` 写着"客户ID，主键"、
    `nickname` 写着"客户昵称"、`phone` 写着"手机号"，而别的表最多因 `下单客户ID` 沾上一列。
    """
    run = await _sync_once(client, login, monkeypatch, account)
    items = await _search_once(client, run, "客户手机号", k=5)
    assert _names(items)[0] == "customer", _names(items)

    customer = _item(items, "customer")
    assert {"phone", "nickname", "id"} == {h["column_name"] for h in customer["hits"]}
    assert (customer["matched_term_count"], customer["matched_column_count"]) == (3, 3)
    # 理由三元组齐了才算"可解释"：列名 + 比的是哪个字段 + 被哪个词对上的。
    # `客户手机号` 有 5 个字，超过整段阈值，只发二字滑窗 → 对上的词是 客户/手机/机号。
    # 演示库没人做过人工翻译，`comment_zh` 是空的（sync_service 只保留人工改过的那几列），
    # 所以理由全落在 `comment_raw` 上——三个原料字段各查一遍，查到哪个就报哪个。
    reasons = {(h["column_name"], h["field"], h["term"]) for h in customer["hits"]}
    assert {
        ("id", "comment_raw", "客户"),
        ("nickname", "comment_raw", "客户"),
        ("phone", "comment_raw", "手机"),
        ("phone", "comment_raw", "机号"),
    } <= reasons
    assert {h["field"] for h in customer["hits"]} == {"comment_raw"}


async def test_验收3_第二次同步后问句结果不变(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
) -> None:
    """幂等一路走到检索：doc_uid 唯一 + profile 复用，第二轮不该多出候选表。

    翻倍了会怎样：同一张表的两套卡各自占一个名额，`k=5` 就只剩三个候选——010 拼 prompt
    的素材被凭空砍掉四成，而这一层不会报任何错。
    """
    run = await _sync_once(client, login, monkeypatch, account)
    first = await _search_once(client, run, "订单金额", k=5)
    await _trigger(client, run)
    second = await _search_once(client, run, "订单金额", k=5)

    assert first == second
    assert _names(first)[0] == "order_main", _names(first)
