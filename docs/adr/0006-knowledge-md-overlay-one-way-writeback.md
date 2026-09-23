# `knowledge/` markdown 是人工知识的覆盖层，回写单向

一张表在仓库里对应一个 `knowledge/<数据源>/<schema>.<表>.md`，分 `## 自动生成` 与 `## 人工维护` 两区块：
同步只整段重写前者，导入只把后者回写进库内人工列（`comment_zh`/`business_desc`/`granularity`/`is_hidden`）。
**任何路径都不允许自动生成反向覆盖人工区块。**

**为什么**：业务语义只有人知道，而它必须能进 git 审阅、能 diff、能在元数据库被误删后重建；
存成库内字段就没有这些。**后果**：人工列与同步列在同一行里靠 `COALESCE` 分离保护；
`knowledge/` 是版本库内容，`.gitignore` 里明确不忽略。
