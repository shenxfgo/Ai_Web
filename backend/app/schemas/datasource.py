"""数据源 DTO。输出模型是白名单：口令字段根本不写在里面，所以它无处可漏。"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# architecture §7 的 access 三档。'granted' 暂时到不了：它依赖 datasource_grants（§2.3），
# 而那张表目前没有工单认领。留空位是为了漏出"有源看不见"时不至于被误读成权限代码写错。
Access = Literal["owner", "granted", "global"]

# 固定四个点：任何长度的口令在界面上都长一样，否则掩码本身泄露了长度
PASSWORD_MASK = "••••"


class DataSourceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    kind: Literal["mysql", "postgres"]
    host: str = Field(min_length=1, max_length=255)
    port: int = Field(ge=1, le=65535)
    # MySQL 恒空串（metadata-model §2.2 末注：库走 include_schemas，不设 database 单列）
    catalog_name: str = Field(default="", max_length=255)
    connect_user: str = Field(min_length=1, max_length=63)
    # 只进不出：这个字段名在 DataSourceOut 里不存在
    connect_password: str = Field(min_length=1, max_length=512)
    params: dict[str, str] = Field(default_factory=dict)
    include_schemas: list[str] | None = None
    include_tables: list[str] | None = None
    exclude_tables: list[str] | None = None
    row_limit: int = Field(default=1000, ge=1, le=1_000_000)
    timeout_ms: int = Field(default=15_000, ge=100, le=600_000)
    # §7 的 POST 请求体里没有这一项（授权走 PUT /grants），但那张 grants 表没有工单认领。
    # 不开这个口的话 allow_global_access 就永远是 false，'global' 档成为死代码。
    allow_global_access: bool = False


class DataSourceOut(BaseModel):
    """`connect_password` / `secret_enc` 不在这里出现——所以任何端点都回传不了口令。

    `has_secret` 而不是把密文长度之类的线索透出去：前端只需要知道"要不要让用户重填"。
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    kind: str
    host: str
    port: int
    catalog_name: str
    connect_user: str
    status: str
    server_version: str | None
    readonly_enforced: bool
    row_limit: int
    timeout_ms: int
    allow_global_access: bool
    last_sync_at: datetime | None
    created_by: int
    created_at: datetime
    access: Access
    password_masked: str = PASSWORD_MASK
    has_secret: bool = True


class ConnectionTestRequest(BaseModel):
    """`POST /datasources/{id}/test` 的请求体，可以整体为空。

    空 = 拿库里已保存的口令去连；带 connect_password = §7 说的"临时未保存口令"，
    让 UI 能在保存之前先试一次。这里收到即原样交给源库，不写进任何一行日志。
    `min_length=1` 是为了把"清空了输入框"和"没填"分开：少这一条，空串会被
    `or 已存口令` 静默当成没填，用户看到"清空口令还能连上"。
    """

    connect_password: str | None = Field(default=None, min_length=1, max_length=512)


class GrantsVerdict(BaseModel):
    """`SHOW GRANTS` 的结论。warnings 是原文那几行，用户要照着它去源库改授权。

    `code` 是 nl2sql-safety §4.1 点名要 test_connection 报的机读标记
    （`readonly_capability_missing`）。它跟着 200 一起回，不是 HTTP 错误——同一条 §4.1
    写着"黄色警告不阻断"，阻断是 011 执行前检查的事。
    """

    read_only: bool
    code: str | None
    warnings: list[str]


class ConnectionTestOut(BaseModel):
    """/test 的响应也走白名单 DTO，不返回裸 dict。

    探测函数手里同时握着明文口令和 SHOW GRANTS 原文：裸 dict 意味着以后有人调试时
    多加一个键（`"dsn"`、`"password"`）不会有任何测试变红——白名单才会。
    字段口径来自 `docs/architecture.md` §7 的 test 行。
    """

    ok: bool
    server_version: str
    visible_schemas: list[str]
    est_table_count: int
    table_count: int
    view_count: int
    grants: GrantsVerdict
    supports_max_execution_time: bool
    latency_ms: int
