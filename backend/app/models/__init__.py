"""import 本包即把全部 ORM 表注册进 `Base.metadata`——alembic autogenerate 靠这一点看见它们。"""

from app.models.datasource import DataSource
from app.models.kb import KbCard, KbIndexProfile
from app.models.meta import (
    MetaColumn,
    MetaDatabase,
    MetaIndex,
    MetaIndexColumn,
    MetaRelation,
    MetaTable,
    SyncJob,
)
from app.models.user import User

__all__ = [
    "DataSource",
    "KbCard",
    "KbIndexProfile",
    "MetaColumn",
    "MetaDatabase",
    "MetaIndex",
    "MetaIndexColumn",
    "MetaRelation",
    "MetaTable",
    "SyncJob",
    "User",
]
