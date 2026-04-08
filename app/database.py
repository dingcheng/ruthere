from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import get_settings


class Base(DeclarativeBase):
    pass


_url = get_settings().database_url
_is_sqlite = _url.startswith("sqlite")

# SQLite in-memory (used in tests) requires StaticPool and doesn't support pool_size params.
# File-based SQLite and other databases support pooling.
_engine_kwargs = {"echo": False}
_is_memory = ":memory:" in _url or "mode=memory" in _url or _url.endswith("://")
if _is_sqlite and not _is_memory:
    _engine_kwargs.update(pool_size=5, max_overflow=10, pool_timeout=30)

engine = create_async_engine(_url, **_engine_kwargs)


# Enable WAL mode for file-based SQLite — allows concurrent reads during writes.
if _is_sqlite:
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        # WAL only works on file-based SQLite, not in-memory — but the pragmas are harmless either way
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_db() -> AsyncSession:
    async with async_session() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
