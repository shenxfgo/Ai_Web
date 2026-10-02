"""向量列相关迁移的 DDL 闸（无 DB：直接断言迁移模块产出的 SQL）——0005 建表、0008 加第二列。

口径来源：
- `docs/kb-workflow.md` §7 与 ADR-0003 的"可插拔边界"：`AIWEB_EMBEDDING__DIMENSION`
  从 0005 起兼任 `kb_card.embedding`（**第一列**）的列宽，`vector(0)` 在 pgvector 里非法，
  所以维度为 0 时迁移必须**拒绝建表并给中文 hint**，而不是猜一个默认维度。
  0008 起这条只对第一列成立：第二列 `embedding_1024` 的宽度是字面量（ADR-0012 的翻案）。
- `docs/metadata-model.md` §2.6 的 SQL 块（HNSW 的 WITH 两个数、trgm、simple FTS）
- `docs/adr/0013-*.md` 与工单 026：档案三态的词表、以及现存行的归置口径
- `docs/verification.md` §2.2 第 2 项：迁移写错了 90% 的情形要在无 DB 下被抓到
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from tests.conftest import BACKEND_ROOT, load_script

MIGRATION = BACKEND_ROOT / "alembic" / "versions" / "0005_kb_card.py"
MIGRATION_0008 = BACKEND_ROOT / "alembic" / "versions" / "0008_embedding_columns.py"
MIGRATION_0009 = BACKEND_ROOT / "alembic" / "versions" / "0009_drop_profile_is_active.py"


@pytest.fixture(scope="module")
def mig() -> object:
    assert MIGRATION.is_file(), f"迁移文件不存在：{MIGRATION}"
    return load_script("m0005", Path(MIGRATION))


@pytest.fixture(scope="module")
def mig8() -> object:
    assert MIGRATION_0008.is_file(), f"迁移文件不存在：{MIGRATION_0008}"
    return load_script("m0008", Path(MIGRATION_0008))


@pytest.fixture(scope="module")
def mig9() -> object:
    assert MIGRATION_0009.is_file(), f"迁移文件不存在：{MIGRATION_0009}"
    return load_script("m0009", Path(MIGRATION_0009))


class _Result:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar(self) -> object:
        return self._value


class _Bind:
    """假连接：只记账"跑过哪些语句"并回吐一个标量，够驱动扩展闸门。

    判据必须是 `pg_extension` 而不是任何业务表——表在而扩展没了，或表不在而扩展在，
    两种都见过；拿表存在性当凭据的迁移会在别人机器上炸。
    """

    def __init__(self, value: object, seen: list[str]) -> None:
        self._value = value
        self._seen = seen

    def execute(self, stmt: object, *_a: object, **_k: object) -> _Result:
        self._seen.append(str(stmt))
        return _Result(self._value)


def test_维度为_0_时迁移拒绝而不是猜一个默认宽度(mig: object) -> None:
    """kb-workflow §7：0 意味着"没配维度"，而 `vector(0)` 是非法类型，建表会当场炸。

    猜默认值（1536）是最坏的选择：换 provider 时列宽悄悄不对，报错发生在写向量那一刻。
    所以这里必须 fail fast，并且提示里要点名那个键。
    """
    with pytest.raises(RuntimeError) as exc:
        mig.vector_type(0)  # type: ignore[attr-defined]
    message = str(exc.value)
    assert "AIWEB_EMBEDDING__DIMENSION" in message
    assert "vector(0)" in message, "hint 要说清「为什么 0 不行」，不然用户只会去查配置有没有拼错"


def test_列宽由维度参数渲染_迁移里没有第二个_1536(mig: object) -> None:
    ddl = str(mig.vector_type(1536).compile(dialect=postgresql.dialect()))  # type: ignore[attr-defined]
    assert ddl.upper() == "VECTOR(1536)"
    assert (
        str(mig.vector_type(1024).compile(dialect=postgresql.dialect())).upper()  # type: ignore[attr-defined]
        == "VECTOR(1024)"
    )


def test_缺_vector_扩展时迁移先给中文_hint(mig: object) -> None:
    """P1 as-built（roadmap §P1 验收 6 末注）：迁移**不尝试建** vector，只把人领到对的地方。

    pgvector 0.8.6 的 `vector` 不是 trusted 扩展，应用账号执行 `CREATE EXTENSION` 必失败，
    还脏掉整条迁移链。放任 PG 自己报 `type "vector" does not exist`，用户看到的是一个
    与配置无关的类型错误——所以先查 `pg_extension`，把"要超级用户执行"写进 hint。
    """
    seen: list[str] = []
    with pytest.raises(RuntimeError) as exc:
        mig.require_vector_extension(_Bind(None, seen))  # type: ignore[attr-defined]
    message = str(exc.value)
    assert "CREATE EXTENSION vector" in message
    assert "超级用户" in message
    assert "pg_extension" in seen[0], "判据只能是扩展注册表，不是某个表是否存在"

    mig.require_vector_extension(_Bind(1, []))  # type: ignore[attr-defined]


def test_0008_接在现有最新那条_0007_之后(mig8: object) -> None:
    """spec §7：「编号接现有最新的迁移文件（0007）之后」。

    链条断了的表现不是报错，而是**静默不跑**：alembic 只走它找到的那条线，
    另一条线上的列在库里根本不存在，第一次用到才炸。
    """
    assert mig8.revision == "0008"  # type: ignore[attr-defined]
    assert mig8.down_revision == "0007"  # type: ignore[attr-defined]


def test_0008_的归置口径就是那句跟着_is_active_走的_case(mig8: object) -> None:
    """工单 026 写死的归置口径：`is_active=true` → `active`，其余 → `retired`（不是 `draft`）。

    为什么不是 draft：`draft` 的含义是"建好了等着被激活"，而 P2/P3 期间落下的行是**正在服务**
    的那一套；把它们落成 draft，激活端点下一步就会把它们 retire 掉——那是把现状当待办。
    期望值是工单字面，不是从 upgrade() 里反推的。
    """
    sql = mig8.backfill_status_sql("aiweb")  # type: ignore[attr-defined]
    assert "CASE WHEN is_active THEN 'active' ELSE 'retired' END" in sql, sql
    assert '"aiweb".kb_index_profile' in sql, "得打在带 schema 的那张表上"
    assert "SET status" in sql, sql


def test_0008_加第二向量列前也要过同一道扩展闸(mig8: object) -> None:
    """0005 建过扩展不等于这一版还在：扩展可以被 DROP，而 `vector(1024)` 一样要它。

    hint 必须点名 0008 与这版要建的东西，否则人只知道"缺扩展"，不知道该重跑哪一条。
    """
    seen: list[str] = []
    with pytest.raises(RuntimeError) as exc:
        mig8.require_vector_extension(_Bind(None, seen))  # type: ignore[attr-defined]
    message = str(exc.value)
    assert "0008" in message
    assert "CREATE EXTENSION vector" in message
    assert "超级用户" in message
    assert "pg_extension" in seen[0]

    mig8.require_vector_extension(_Bind(1, []))  # type: ignore[attr-defined]


def test_0009_接在_0008_之后而不是另起一条线(mig9: object) -> None:
    """工单 031 验收 8 定的那条链：`0008` 扩、`0009` 收，两条都在同一条线上。

    期望值是工单字面，不是从文件里反推的。断链的表现不是报错而是**静默不跑**（同 0008 那条）：
    收缩那一版不在主线上，`is_active` 就一直留在库里，而读侧已经只认 `status`——
    于是"两套真相"那句 ADR-0013 反对的事，恰恰被我们自己留下来了。
    """
    assert mig9.revision == "0009"  # type: ignore[attr-defined]
    assert mig9.down_revision == "0008"  # type: ignore[attr-defined]


def test_0009_的_downgrade_按_status_把镜像补回来(mig9: object) -> None:
    """回滚那一格必须是**从 status 补**，不是"加一列全 false"。

    收缩迁移的 downgrade 很容易写成 `ADD COLUMN … DEFAULT false` 就完事：列回来了，
    内容却全空，旧代码（0008 那一世）读到的 active 档案是零行——回滚等于把现状抹掉。
    0009 收的是 `is_active`，那一刻 `status` 还在（0008 的 downgrade 才删它，顺序在 0009 之后），
    所以镜像有一个唯一的来源。期望值来自 026 定的双写口径（`is_active` 跟着 `status` 的字面走），
    不是从迁移实现里抄的。
    """
    sql = mig9.restore_is_active_sql("aiweb")  # type: ignore[attr-defined]
    assert "SET is_active = (status = 'active')" in sql, sql
    assert '"aiweb".kb_index_profile' in sql, "得打在带 schema 的那张表上"
