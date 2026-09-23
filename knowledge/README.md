# knowledge/

人工维护的表知识 Markdown，**进版本库**——它是知识库的一部分，不是文档附件。

- 文件名：`<数据源>/<schema>.<表名>.md`，与元数据库里的表一一对应。
- 每个文件两段：`## 自动生成`（同步时整段覆盖，别手改）和 `## 人工维护`（同步永不覆盖，业务口径写这里）。
- 头部 front-matter 带 `table_uid` / `struct_hash` / `template_version`，用来判断结构是否变了。
- 问数时按检索命中的表把这里的内容拼进 prompt，所以写在这里的一句话口径能直接影响答对率。

`kb_export.py` 把库里的卡片刷成这些文件，`kb_import.py` 只把 `## 人工维护` 段回写进库——单向，不会把自动段盖回去。

详见 [docs/kb-workflow.md](../docs/kb-workflow.md)。
