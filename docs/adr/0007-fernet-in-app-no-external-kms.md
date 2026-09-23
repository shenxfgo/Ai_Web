# 数据源口令用应用内 Fernet 加密落库，不引外部 KMS

源库口令存在 `data_sources.secret_enc bytea`，用 `AIWEB_FERNET__KEYS`（支持多把、可轮换）加密；
任何 API 响应体不含口令，只回 `password_masked` / `has_secret`。

**为什么**：单用户本地部署引 Vault/KMS 的收益低于其运维成本，而真正的威胁面是"口令在库里明文躺着"
和"口令从接口泄漏出去"，这两条 Fernet + 响应裁剪就能封住。**后果**：密钥只在 env（L1），
`app_settings` 运行期配置端点**拒绝**写入任何 L1 密钥；丢了密钥等于丢了所有已存口令，只能重新录入。
