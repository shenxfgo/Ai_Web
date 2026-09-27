# `table_uid` 是四元组的 md5 指纹，拼接用单元分隔符

表的对外标识 `table_uid = md5(datasource_id::text || 分隔符 || catalog_name || 分隔符 || schema_name || 分隔符 || table_name)`，
`char(32) GENERATED ALWAYS ... STORED` + `UNIQUE`。

> as-built(0004)：决定不变，**拼法从 `concat_ws` 改成 `||` 链**——`concat_ws` 在 PG 是 STABLE 函数，
> 生成列要求 IMMUTABLE 表达式，真库建表直接拒绝（`metadata-model.md` §1 的 as-built 注有实测口径）。
> 四列都 `NOT NULL`，所以 `||` 与 `concat_ws` 的结果逐字节相同，uid 值不受这次改写影响。

**为什么用 md5 而不是 uuid**：uuid 需要外部输入或映射表，而我们要的是"同一张源表在任何环境算出同一个串"，
hash 天然满足，且能跨同步比对、当 embedding 的 doc id。**为什么用 `\x1f`（单元分隔符）**：
用 `-` 或 `_` 这类合法标识符字符做分隔，`a_b`+`c` 与 `a`+`b_c` 会撞出同一个 md5。**为什么用 `char(32)` 不用 `uuid` 类型**：
md5 不是 uuid 形状，别硬套类型；定长 32 走 btree 精确命中。

**后果**：表被重命名 = uid 变化 = 旧卡片与人工知识成为孤儿，因此删除走差分标记而非物理删。
