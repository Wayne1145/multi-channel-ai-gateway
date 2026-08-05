import os
import tempfile

import sqlalchemy as sa
from cryptography.fernet import Fernet

with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as handle:
    _db = handle.name
os.environ.update(
    {
        "DATABASE_URL": f"sqlite:///{_db}",
        "REDIS_URL": "redis://localhost:6379/15",
        "IDENTITY_HMAC_KEY": "test-hmac-key-with-at-least-32-chars",
        "SECRET_ENCRYPTION_KEY": Fernet.generate_key().decode(),
        "ADMIN_TOKEN": "test-admin-token",
        "DEFAULT_MODEL": "deepseek-chat",
    }
)
import pytest

from wecom_ai_gateway.db import Base, SessionLocal, engine


@sa.event.listens_for(engine, "connect")
def _sqlite_test_fast(dbapi_connection, connection_record):
    # 测试专用提速：WSL 上 SQLite 逐表 fsync 极慢，关闭同步并改用内存日志。
    # 仅对 sqlite:// 测试连接生效；生产 PostgreSQL 不受影响。
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=MEMORY")
    cursor.execute("PRAGMA synchronous=OFF")
    cursor.close()


@pytest.fixture(autouse=True)
def database():
    Base.metadata.create_all(engine)
    yield
    Base.metadata.drop_all(engine)


@pytest.fixture
def db():
    s = SessionLocal()
    yield s
    s.close()
