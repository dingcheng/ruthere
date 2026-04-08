"""Shared test fixtures: async client, in-memory DB, authenticated user helpers."""
import asyncio
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker

# Override settings BEFORE importing the app
import os
os.environ["DATABASE_URL"] = "sqlite+aiosqlite://"  # in-memory
os.environ["SECRET_KEY"] = "test-secret-key-not-for-production"
os.environ["VAULT_KEY"] = "dGVzdC12YXVsdC1rZXktMzItYnl0ZXMhIQ=="  # base64 of 32 bytes
os.environ["RESEND_API_KEY"] = ""  # disable email sending
os.environ["BASE_URL"] = "http://testserver"
os.environ["NTFY_BASE_URL"] = "https://ntfy.sh"

from app.database import Base, engine, async_session, get_db
from app.main import app
from app.models.models import User
from app.services.auth import hash_password, create_access_token


# Use a single event loop for the entire test session
@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


# Create tables once per session, drop after
@pytest_asyncio.fixture(scope="session", autouse=True)
async def setup_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


# Per-test DB session that cleans up after each test
@pytest_asyncio.fixture
async def db():
    async with async_session() as session:
        yield session
        # Roll back any pending changes
        await session.rollback()
    # Clean up all tables after each test in a new session
    async with async_session() as cleanup:
        for table in reversed(Base.metadata.sorted_tables):
            await cleanup.execute(table.delete())
        await cleanup.commit()


# Override the app's get_db dependency to use our test session
@pytest_asyncio.fixture
async def client(db):
    async def _override_get_db():
        yield db

    app.dependency_overrides[get_db] = _override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c

    app.dependency_overrides.clear()


# Helper: create a user directly in the DB and return (user, token)
async def create_test_user(
    db: AsyncSession,
    email: str = "test@example.com",
    password: str = "testpassword123",
    display_name: str = "Test User",
) -> tuple[User, str]:
    """Create a user and return (user_object, jwt_token)."""
    import uuid
    user = User(
        id=str(uuid.uuid4()),
        email=email,
        password_hash=hash_password(password),
        display_name=display_name,
        ntfy_topic=f"test-{uuid.uuid4().hex[:12]}",
        heartbeat_interval_hours=4,
        response_window_hours=2,
        missed_threshold=3,
        timezone="America/Los_Angeles",
        active_hours_start=8,
        active_hours_end=22,
    )
    db.add(user)
    await db.flush()
    token = create_access_token(user.id)
    return user, token


@pytest_asyncio.fixture
async def auth_user(db):
    """Fixture that returns (user, token) for an authenticated test user."""
    user, token = await create_test_user(db)
    return user, token


@pytest_asyncio.fixture
async def auth_headers(auth_user):
    """Fixture that returns auth headers dict."""
    _, token = auth_user
    return {"Authorization": f"Bearer {token}"}
