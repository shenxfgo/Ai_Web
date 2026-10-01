"""真演示库上的分批（工单 020 验收 1/3/4/5/7）：批是**发送方式**，不是第二套账。

标记 live：要真 MySQL 在线，凭据从 gitignored 的 `.setup/aiweb_ro.cnf` 读（同 002/007），
缺文件就 skip。方言层的批语义由 `tests/unit/test_extract_mysql_batching.py` 在假连接上钉，
这一份钉的是它跑在真 `information_schema` 上时**真的**分成几批、每批问了谁、隔了多久。

为什么不硬写"第一批 = {category, customer, order_item}"：§8.1 B 那条**没有 `ORDER BY`**，
交付顺序不是源库对外的承诺，把刀口按它写死会让用例在另一台 MySQL 上无故发红。所以这里断的是
每批的**成员**——批大小逐个钉、批与批互不重叠、并起来正好是文档名单，而每一批的名单同时是
那三条昂贵查询各自收到的 `IN` 清单（这才叫"分批真的传到 SQL 上了"）。

期望值出处：`docs/verification.md` §1/§1.2 的 as-built（排除 `_%` 后 9 表 + 1 视图 = 10 个业务
对象，名单逐字来自 `backend/scripts/init_demo_mysql.sql` 的 `CREATE` 语句）、§8.1 的 B/C/D/E
分工、工单 020 的已定口径（默认 200/100 不改、具名绑定参数、每库一事务不变）。
"""

from __future__ import annotations

import itertools
import time
from collections.abc import Awaitable, Callable, Iterator
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.extractor.mysql import MySQLExtractor
from app.settings import get_settings
from tests.integration.conftest import Account, DbAccount
from tests.integration.test_sync_events_pg import _events
from tests.integration.test_sync_live import DEMO_DB, _counts, _rows
from tests.integration.test_sync_pg import _sync

pytestmark = [pytest.mark.pg, pytest.mark.live]

Login = Callable[..., Awaitable[Account]]
Factory = async_sessionmaker[AsyncSession]

# verification.md §1 的口径（9 BASE TABLE + 1 VIEW）
TABLES = 9
VIEWS = 1

#: 用例把批大小压到 3 才有 4 批；200（默认）在 10 个对象上只有一批。
BATCH_SIZE = 3
#: 一个够大、又不至于把闸拖慢的值：4 批之间有 3 个间隔，下限 750ms。
BATCH_INTERVAL_MS = 250

# 演示库排除 `_%` 后的业务对象名单（init_demo_mysql.sql 的 CREATE 逐字抄，按字典序）
BUSINESS = (
    "category",
    "customer",
    "order_item",
    "order_main",
    "payment_record",
    "product",
    "product_stats_wide",
    "refund_record",
    "user_activity_log",
    "v_daily_sales",
)

# C/D/E 三条昂贵查询在 §8.1 原文里的表别名（用来把语句认回它属于哪一条）；
# B 不在列：它按 schema + 范围过滤拿全量，本来就没有 `IN` 清单，也就没法"分批"。
NEEDLES = (
    "information_schema.statistics s",
    "information_schema.columns c",
    "information_schema.key_column_usage k",
)


class Recorder:
    """拦在 `MySQLExtractor._rows` 上的收音器：`(规范化语句, 绑定参数, 单调时刻)`。

    `_rows` 是方言层唯一的 I/O 出口（与 `test_sync_live` 同一个理由），所以"这一批发了几条、
    各自问了哪几张表、批与批之间隔了多久"这三件事只有在这里量得到——事件行只会告诉你结果。
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], float]] = []

    def install(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = MySQLExtractor._rows

        def record(
            extractor: MySQLExtractor, sql: str, params: dict[str, object] | None = None
        ) -> list[dict[str, object]]:
            self.calls.append((" ".join(sql.lower().split()), dict(params or {}), time.monotonic()))
            return real(extractor, sql, params)

        monkeypatch.setattr(MySQLExtractor, "_rows", record)

    def batches_sent(self, needle: str) -> list[tuple[list[str], float]]:
        """含 `needle` 的每条语句各自问了哪几张表、发出时刻是多少（按发出顺序）。"""
        out: list[tuple[list[str], float]] = []
        for sql, params, stamp in self.calls:
            if needle in sql:
                # 按绑定参数的**序号**还原顺序（`:tbl_0` 在前）：那是这一批声明的顺序，
                # 排成字典序就等于替被测方发明了它并没有的顺序。
                asked = [
                    str(v)
                    for _, v in sorted(
                        (int(k[4:]), v) for k, v in params.items() if k.startswith("tbl_")
                    )
                ]
                out.append((asked, stamp))
        return out


async def _index_shape(session_factory: Factory, ds_id: int) -> list[dict[str, Any]]:
    """索引那一格的**形状**：谁在读、读到第几位、前缀长度是多少、`cardinality` 有没有落下来。

    这里是刻意**不**取 `cardinality` 的数值，只取"它是不是 NULL"。理由是数值那一列在 MySQL 里
    是源库的估算（InnoDB 采样出来的），两次真跑之间它可以合法地变；把"分批没改变结果"钉在
    一个本来就会漂的数上，用例就变成看运气的红。"它非空"才是 §10 末那个锚点要的东西。
    """
    return await _rows(
        session_factory,
        "select t.table_name as tbl, i.index_name as idx, ic.seq_in_index as seq,"
        "       ic.column_name as col, ic.sub_part as sub_part,"
        "       (i.cardinality is not null) as has_cardinality"
        ' from "{s}".meta_index_column ic'
        ' join "{s}".meta_index i on i.id = ic.index_id'
        ' join "{s}".meta_table t on t.id = i.table_id'
        " where t.datasource_id = :ds"
        " order by t.table_name, i.index_name, ic.seq_in_index",
        ds=ds_id,
    )


def _use_batching(monkeypatch: pytest.MonkeyPatch, size: int, interval_ms: int) -> None:
    """经真配置通道改批大小/间隔（同 021 的 `max_tables_one`）。

    桩掉 `get_settings` 只盖住一次读取，验不到"读取点"这件事本身。
    """
    monkeypatch.setenv("AIWEB_EXTRACT__BATCH_SIZE", str(size))
    monkeypatch.setenv("AIWEB_EXTRACT__BATCH_INTERVAL_MS", str(interval_ms))
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


async def _register(client: AsyncClient, acct: Account, account: DbAccount, name: str) -> int:
    user, password, host, port = account
    resp = await client.post(
        "/api/datasources",
        json={
            "name": name,
            "kind": "mysql",
            "host": host,
            "port": port,
            "connect_user": user,
            "connect_password": password,
            "include_schemas": [DEMO_DB],
            # verification §1.2：下划线前缀的是建库脚本的内部对象，不是业务表
            "exclude_tables": ["\\_%"],
        },
        headers=acct.headers,
    )
    assert resp.status_code == 201, resp.text
    return int(resp.json()["id"])


async def test_批三发送时四条昂贵查询各发四条_批名单与间隔都对得上(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: Factory,
) -> None:
    """验收 1 + 3 + 4：`BATCH_SIZE=3` 在 10 个对象的真演示库上是 4 批，且批名单真的进了 SQL。

    断的是**每批的成员**而不是"总共 4 批"：批大小逐个钉（3/3/3/1，末批只剩 1 张正是验收 2
    那个边界）、批与批互不重叠、并起来正好是文档那份名单。再拿同一份名单去比那三条昂贵查询
    各自收到的 `IN` 清单和事件负载里的 `batches`——四条链一起对上，"分批只在方言层自娱自乐"
    这种假绿就没有立足点。
    """
    rec = Recorder()
    rec.install(monkeypatch)
    _use_batching(monkeypatch, BATCH_SIZE, BATCH_INTERVAL_MS)

    acct = await login(username="batch-live-owner", role="member")
    ds_id = await _register(client, acct, account, "batch-live")
    submit = await _sync(client, acct, ds_id)
    assert submit.status_code == 202, submit.body
    body = submit.body
    assert body["status"] == "success", body

    # ---- 验收 1：事件负载里那 4 批（017 的 `tables` 帧）
    events = await _events(session_factory, int(body["job_id"]))
    payload_batches = next(e for e in events if e["phase"] == "tables")["payload"]["batches"]
    assert [len(b) for b in payload_batches] == [3, 3, 3, 1], payload_batches
    flat = [n for b in payload_batches for n in b]
    assert sorted(flat) == sorted(BUSINESS), "并起来必须是文档那 10 个对象，不多不少"
    assert len(set(flat)) == len(flat), "同一张表出现在两批里就是重复抽取"

    # ---- 验收 3：三条昂贵查询各 4 发，且第 k 发问的就是第 k 批
    for needle in NEEDLES:
        sent = rec.batches_sent(needle)
        assert [names for names, _ in sent] == payload_batches, needle
    # B 那条只有一发：它是名单的来源，不能被"分批"（分批它就没有全量可抽了）。
    # 认它靠 `t.row_format` 而不是 `information_schema.tables t`：A（库级统计）和分母那条
    # `count_scope` 共用同一个 FROM/WHERE，按表名认会一次认出三条。
    assert len([sql for sql, _, _ in rec.calls if "t.row_format" in sql]) == 1

    # ---- 验收 4：批间隔真的让了气（4 批 3 个间隔，下限 3×250ms）。
    # "第一批之前不许睡"与间隔的上限由单测 `test_批间隔真的让了一口气_而且第一批之前不让` 钉，
    # 这里只钉真链路上那个**下限**——键读不到就会退回默认 100ms，那条路当场红。
    stamps = [stamp for _, stamp in rec.batches_sent(NEEDLES[0])]
    gaps = [b - a for a, b in itertools.pairwise(stamps)]
    assert len(gaps) == 3, gaps
    assert all(g >= BATCH_INTERVAL_MS / 1000 for g in gaps), f"批间没有让气：{gaps}"
    assert sum(gaps) >= 3 * BATCH_INTERVAL_MS / 1000

    # 落库的对象数仍是文档口径的 10（分批不改分母，也不改 `counters.tables`）
    assert body["counters"]["tables"] == TABLES + VIEWS, body["counters"]
    assert body["counters"]["databases"] == 1
    assert body["counters"]["batches"] == 4, "跨库相加的批数：一个库 4 批"


async def test_分批没有改变落库结果_与不分批那一次逐行逐字相同(
    client: AsyncClient,
    login: Login,
    monkeypatch: pytest.MonkeyPatch,
    account: DbAccount,
    session_factory: Factory,
) -> None:
    """验收 5 的回归门 + 验收 7 那一格：同一份源、同一个连接，只换发送方式，落库必须**逐字节**一样。

    先按默认 200 跑一批装完，再按 3 切四批重跑，比对五张 `meta_*` 的行数、全部卡片正文
    （`text_md` 逐字）和索引那一条链的形状。为什么跟另一次真跑比而不是跟 008 的 golden 文件比：
    那七份 golden 喂的是按 §2.1 手工摆出来的 `meta_*` 输入（`test_sync_card_isolation_pg.py`
    开头记着），不是演示库的实样——拿它对真库等于比两件本来就不该相等的事。文档口径那一份锚点
    仍然钉在这里：`counters.tables == 10`（§1 的 9 表 + 1 视图）。
    """
    acct = await login(username="batch-equiv-owner", role="member")
    ds_id = await _register(client, acct, account, "batch-equiv")

    async def _snapshot() -> dict[str, Any]:
        submit = await _sync(client, acct, ds_id)
        assert submit.status_code == 202, submit.body
        assert submit.body["status"] == "success", submit.body
        counters = dict(submit.body["counters"])
        # batches 是唯一该变的那一格：它说的正是"这轮打了几批"
        batches = counters.pop("batches")
        return {
            "counters": counters,
            "batches": batches,
            "meta": await _counts(session_factory, ds_id),
            "idx": await _index_shape(session_factory, ds_id),
            "cards": await _rows(
                session_factory,
                'select kind, seq, doc_uid, text_md from "{s}".kb_card'
                " where datasource_id = :ds and deleted_at is null order by doc_uid",
                ds=ds_id,
            ),
        }

    _use_batching(monkeypatch, 200, 0)
    one_batch = await _snapshot()
    _use_batching(monkeypatch, BATCH_SIZE, 0)
    four_batches = await _snapshot()

    assert one_batch["batches"] == 1 and four_batches["batches"] == 4
    assert one_batch["counters"] == four_batches["counters"], "行数账必须一模一样"
    assert one_batch["meta"] == four_batches["meta"]
    # 验收 7（roadmap §P3）：`CARDINALITY`/`SUB_PART` 早在 007 就通了，这一格补的是
    # "分批后每批都还读到"——切片换了发送方式，索引那条链的形状必须一格不少。
    assert one_batch["idx"] == four_batches["idx"], "索引行的成员/顺序/前缀长度两边必须逐位相同"
    shape = four_batches["idx"]
    assert all(r["has_cardinality"] for r in shape), "有批次的 `cardinality` 没落下来：D 条读丢了行"
    # 前缀索引全库只有一处（verification §1 的夹具 `idx_product_name(name(32))`）。
    # 它落在哪一批是不可知的（B 没有 ORDER BY），所以断的是"它确实在场且值仍是 32"。
    assert [(r["tbl"], r["col"], r["sub_part"]) for r in shape if r["sub_part"] is not None] == [
        ("product", "name", 32)
    ]
    assert len(four_batches["cards"]) == len(one_batch["cards"]) > 0
    # 逐字符：卡片正文里连一个空格都不许多
    assert [c["text_md"] for c in four_batches["cards"]] == [
        c["text_md"] for c in one_batch["cards"]
    ]
    assert [c["doc_uid"] for c in four_batches["cards"]] == [
        c["doc_uid"] for c in one_batch["cards"]
    ]
    # 文档锚点（不是从上一次跑反推的数）：演示库排除内部对象后是 10 个业务对象
    assert four_batches["meta"]["meta_table"] == TABLES + VIEWS
    assert four_batches["counters"]["tables"] == TABLES + VIEWS
