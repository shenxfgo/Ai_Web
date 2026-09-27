"""建初始管理员：`scripts/seed_admin.py`。

幂等：账号已存在就只报告，**不重置口令**（docs/verification.md §3 第 3 步）。
口令为空则拒绝执行——宁可不建，也不建出一个空口令或随机口令的 admin。

口令两条来源都认，进程环境变量优先（一次性使用，推荐），其次 `.env`（`.env.example`
里就列在 jwt 分组旁边）；环境变量里出现过就以它为准，空值也算"这次就是不给"。
Settings 不收这两个键：`AIWEB_BOOTSTRAP_ADMIN_PASSWORD` 没有 `__` 分隔符，硬塞进
Settings 会让"配置唯一入口"多一个只对脚本说话的分组。

退出码：0 成功或已存在；1 建库失败；2 缺口令。
口令本身既不打印也不写日志——它会留在终端回滚缓冲和 CI 日志里。
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

if sys.platform == "win32":  # asyncpg 与 Proactor 事件循环不兼容
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import dotenv_values  # noqa: E402  (随 pydantic-settings 一起装)
from sqlalchemy import select  # noqa: E402
from sqlalchemy.exc import SQLAlchemyError  # noqa: E402

from app.core.db import dispose_engine, get_sessionmaker  # noqa: E402
from app.core.logging import reconfigure_std_streams  # noqa: E402
from app.core.security import hash_password  # noqa: E402
from app.models.user import User  # noqa: E402

USER_KEY = "AIWEB_BOOTSTRAP_ADMIN_USERNAME"
PWD_KEY = "AIWEB_BOOTSTRAP_ADMIN_PASSWORD"


def _config(key: str, default: str = "") -> str:
    # 环境变量里出现过就以它为准，空值也算"这次就是不给"。
    # 写成 `if value:` 会落回 .env，把开发者机器上留着的另一份口令捡起来——
    # 于是"空口令拒绝执行"这条保护在配过 .env 的机器上直接失效。
    if key in os.environ:
        return os.environ[key]
    return dotenv_values(BACKEND_ROOT / ".env").get(key) or default


async def _seed(username: str, password: str) -> int:
    try:
        async with get_sessionmaker()() as session:
            found = (
                await session.execute(select(User).where(User.username == username))
            ).scalar_one_or_none()
            if found is not None:
                print(f"[ skip ] 账号 {username} 已存在（id={found.id}），未改动口令")
                return 0
            user = User(
                username=username,
                display_name="初始管理员",
                password_hash=hash_password(password),
                role="admin",
            )
            session.add(user)
            await session.commit()
            print(f"[ ok ] 已创建 {username}（id={user.id}，role=admin，口令已 argon2id 哈希）")
            return 0
    except SQLAlchemyError as exc:
        # 只报异常名与驱动原文：SQLAlchemyError 的字符串形式会把绑定参数（含哈希串）一起抄出来
        print(f"[FAIL] 写 users 失败：{type(exc.orig).__name__}: {exc.orig}", file=sys.stderr)
        print("       报 UndefinedTable 就先跑迁移：uv run alembic upgrade head", file=sys.stderr)
        return 1
    finally:
        await dispose_engine()


def main() -> int:
    reconfigure_std_streams()
    password = _config(PWD_KEY)
    if not password:
        print(f"[FAIL] {PWD_KEY} 为空，拒绝执行。", file=sys.stderr)
        print("       口令请显式给一次（一次性，不要长期留在 .env）：", file=sys.stderr)
        print(f"         {PWD_KEY}='...' uv run python scripts/seed_admin.py", file=sys.stderr)
        return 2
    username = _config(USER_KEY, "admin")
    return asyncio.run(_seed(username, password))


if __name__ == "__main__":
    raise SystemExit(main())
