# data/

查询结果集落盘目录，内容不进版本库（`.gitignore` 只放行本文件）。

- `results/<yyyymmdd>/<message_id>.csv`：UTF-8 **带 BOM** + CRLF，Excel 直接双击不乱码。
- 同名 `.meta.json`：列名、类型、行数、是否截断，供下载接口校验用。
- 元数据库只存路径与这几个统计字段，**不存业务行**。
- 保留天数由 `AIWEB_RESULT__RETENTION_DAYS` 控制，清理在同步任务里顺带跑。

删掉整个 `results/` 是安全的：历史会话的结果随之不可下载，业务库与元数据库都不受影响。
