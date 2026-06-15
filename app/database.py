import os
from urllib.parse import urlparse, urlunparse
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import declarative_base

DATABASE_URL = os.getenv("DATABASE_URL", "")
# Vercel Postgres/Neon uses postgresql://; convert to async driver and strip query params
if DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)

# asyncpg doesn't support sslmode/channel_binding in URL; strip query string
parsed = urlparse(DATABASE_URL)
if parsed.query:
    DATABASE_URL = urlunparse(parsed._replace(query=""))

engine = create_async_engine(
    DATABASE_URL,
    echo=False,
    future=True,
    connect_args={"ssl": "require"}
)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()

async def get_session() -> AsyncSession:
    async with async_session() as session:
        yield session
