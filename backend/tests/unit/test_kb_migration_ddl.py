"""0005 迁移的维度闸与索引 DDL（无 DB：直接断言迁移模块产出的 SQL）。

口径来源：
- `docs/kb-workflow.md` §7 与 ADR-0003 的"可插拔边界"：`AIWEB_EMBEDDING__DIMENSION`
  从 0005 起兼任 `kb_card.embedding` 的**列宽**，`vector(0)` 在 pgvector 里非法，
  所以维度为 0 时迁移必须**拒绝建表并给中文 hint**，而不是猜一个默认维度。
- `docs/metadata-model.md` §2.6 的 SQL 块（HNSW 的 WITH 两个数、trgm、simple FTS）
- `docs/verification.md` §2.2 第 2 项：迁移写错了 90% 的情形要在无 DB 下被抓到
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from tests.conftest import BACKEND_ROOT, load_script

MIGRATION = BACKEND_ROOT / "alembic" / "versions" / "0005_kb_card.py"


@pytest.fixture(scope="module")
def mig() -> object:
    assert MIGRATION.is_file(), f"迁移文件不存在：{MIGRATION}"
    return load_script("m0005", Path(MIGRATION))


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

    class _Result:
        def __init__(self, value: object) -> None:
            self._value = value

        def scalar(self) -> object:
            return self._value

    class _Bind:
        def __init__(self, value: object) -> None:
            self._value = value

        def execute(self, stmt: object, *_a: object, **_k: object) -> _Result:
            seen.append(str(stmt))
            return _Result(self._value)

    with pytest.raises(RuntimeError) as exc:
        mig.require_vector_extension(_Bind(None))  # type: ignore[attr-defined]
    message = str(exc.value)
    assert "CREATE EXTENSION vector" in message
    assert "超级用户" in message
    assert "pg_extension" in seen[0], "判据只能是扩展注册表，不是某个表是否存在"

    seen.clear()
    mig.require_vector_extension(_Bind(1))  # type: ignore[attr-defined]
