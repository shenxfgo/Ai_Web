from __future__ import annotations

import pytest

from app.services.sql_guard import SqlGuardError
from tests.guard.corpus import REJECT_CASES, run


@pytest.mark.parametrize("sql,rule", REJECT_CASES, ids=[c[0][:38] for c in REJECT_CASES])
def test_攻击语料必须被拒且规则可归因(sql: str, rule: str) -> None:
    with pytest.raises(SqlGuardError) as ei:
        run(sql)

    assert ei.value.rule_id == rule, f"{sql!r} 被拒了，但原因不是预期规则"
