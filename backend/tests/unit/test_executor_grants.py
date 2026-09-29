"""执行前只读校验：工单 011 拍板——复用 grants_verdict，code 非空即硬阻断。

口径来自工单「开工前拍板」：
- 执行路径开头跑 `SHOW GRANTS`，判定复用 datasource_service.grants_verdict（不重造）；
- verdict 的 code 非 None → 抛 ReadonlyCapabilityMissing（403），登记时回 200 的警告在这里翻成拦；
- 判定不缓存（每次执行都探一遍）；`readonly_enforced` 那个自报家门列不作阻断依据。
"""

from __future__ import annotations

import pytest

from app.core.errors import ReadonlyCapabilityMissing
from app.services.nl2sql.executor import enforce_readonly_grants


def test_只读账号放行() -> None:
    # 演示库 aiweb_ro 的真实形态：只有单库 SELECT
    lines = [
        "GRANT USAGE ON *.* TO `aiweb_ro`@`localhost`",
        "GRANT SELECT ON `ai_web_demo`.* TO `aiweb_ro`@`localhost`",
    ]
    enforce_readonly_grants(lines)  # 不抛即通过


@pytest.mark.parametrize(
    "lines",
    [
        ["GRANT SELECT, INSERT, UPDATE, DELETE ON `ai_web_demo`.* TO `x`@`localhost`"],
        ["GRANT ALL PRIVILEGES ON *.* TO `root`@`localhost`"],
    ],
)
def test_非只读账号硬阻断(lines: list[str]) -> None:
    with pytest.raises(ReadonlyCapabilityMissing) as exc:
        enforce_readonly_grants(lines)
    # 复用 grants_verdict 的机读码，不另起一个
    assert exc.value.code == "readonly_capability_missing"
