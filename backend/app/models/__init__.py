"""import 本包即把全部 ORM 表注册进 `Base.metadata`——alembic autogenerate 靠这一点看见它们。"""

from app.models.user import User

__all__ = ["User"]
