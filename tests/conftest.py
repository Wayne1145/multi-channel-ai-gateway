import os
import tempfile

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
