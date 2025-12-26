from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from book_agents.config import settings

engine = create_async_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)

async def session() -> AsyncSession:
    return SessionLocal()
