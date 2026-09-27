"""数据源口令的应用内 Fernet 加解密。

期望值来自文档口径，不从被测代码反算：
- `docs/roadmap.md` P2 验收 3：落库密文必须以 `gAAAAAB` 开头（Fernet 指纹），不是明文
- `docs/nl2sql-safety.md` §6：`secret_enc bytea`，写入即刻加密，轮换走 MultiFernet（新把在前，
  解密按多把尝试）
- `docs/adr/0007-fernet-in-app-no-external-kms.md`：不引外部 KMS，密钥只在 L1 env
"""

from __future__ import annotations

from collections.abc import Callable, Iterator

import pytest
from cryptography.fernet import Fernet

from app.core.security import decrypt_secret, encrypt_secret
from app.settings import get_settings


@pytest.fixture
def fernet_env(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Callable[[list[str]], None]]:
    """把 `AIWEB_FERNET__KEYS` 钉成测试值：测试不能跟着开发者机器的 .env 走。"""

    def apply(keys: list[str]) -> None:
        monkeypatch.setenv("AIWEB_FERNET__KEYS", ",".join(keys))
        get_settings.cache_clear()

    yield apply
    get_settings.cache_clear()


def test_加密后再解密回明文(fernet_env) -> None:
    fernet_env([Fernet.generate_key().decode()])
    blob = encrypt_secret("p@ss#词/100%")
    assert decrypt_secret(blob) == "p@ss#词/100%"


def test_密文以_gAAAAAB_开头(fernet_env) -> None:
    """roadmap P2 验收 3 的字面口径：库里那列看到的前缀必须是 Fernet 指纹。"""
    fernet_env([Fernet.generate_key().decode()])
    assert encrypt_secret("任意口令")[:7] == b"gAAAAAB"


def test_没配密钥时报的是该改哪一行而不是裸异常(fernet_env) -> None:
    """空列表会让 MultiFernet 当场 IndexError；运维拿到的是"未配置 AIWEB_FERNET__KEYS"。"""
    from app.core.errors import AppError

    fernet_env([])
    with pytest.raises(AppError) as boom:
        encrypt_secret("x")
    assert "AIWEB_FERNET__KEYS" in boom.value.message


def test_轮换后旧把仍能解密且加密只用新把(fernet_env) -> None:
    """新把在前是 MultiFernet 的硬约定：加密只走第一项，解密按顺序试。

    反了的话，轮换密钥会把库里已登记的口令全部变砖，而且要到用户点"测试连接"才发现。
    """
    old, new = Fernet.generate_key().decode(), Fernet.generate_key().decode()
    fernet_env([old])
    blob = encrypt_secret("轮换前登记的口令")

    fernet_env([new, old])
    assert decrypt_secret(blob) == "轮换前登记的口令"
    # 新签的那条必须能被新把单独解开：证明加密用的确实是第一项
    fresh = encrypt_secret("轮换后登记的口令")
    assert Fernet(new.encode()).decrypt(fresh) == "轮换后登记的口令".encode()


def test_解不开的密文给结构化错误(fernet_env) -> None:
    """密钥全换过、或库里那列被手工改坏：要报"解不开"，不是把 cryptography 的英文异常名抛到 500。"""
    from app.core.errors import AppError

    fernet_env([Fernet.generate_key().decode()])
    with pytest.raises(AppError) as boom:
        decrypt_secret(b"not-a-fernet-token")
    assert "解密" in boom.value.message
