"""统一数据库连接管理"""

import logging
import os
import sys

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

# 添加项目根目录到Python路径
sys.path.append(os.path.join(os.path.dirname(__file__), "..", ".."))

logger = logging.getLogger(__name__)

# 尝试导入配置，优先使用新的统一配置
try:
    from .config import database_config

    DATABASE_URL = database_config.database_url
    logger.info(f"使用统一数据库配置: {database_config.db_type}")
except (ImportError, AttributeError, ModuleNotFoundError):
    # 回退到旧配置系统
    env_url = os.getenv("DATABASE_URL")
    if env_url:
        DATABASE_URL = env_url
        logger.info("使用环境变量DATABASE_URL配置")
    else:
        try:
            from config.settings import settings

            DATABASE_URL = settings.database.postgres_url
            logger.info("使用旧配置系统的PostgreSQL配置")
        except (ImportError, AttributeError, ModuleNotFoundError):
            DATABASE_URL = "postgresql+psycopg2://postgres:password@localhost:5432/quantmind"
            logger.warning("未找到配置文件，使用默认PostgreSQL数据库配置")

# 创建数据库引擎
# 自动根据数据库URL选择合适的配置
engine = create_engine(
    DATABASE_URL,
    pool_size=5,
    max_overflow=10,
    pool_pre_ping=True,
    echo=False,  # 默认关闭SQL日志输出
)

# SQLAlchemy配置
Base = declarative_base()
metadata = MetaData()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db() -> Session:
    """获取数据库会话"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """初始化数据库 - 已禁用自动建表，强制使用 quantmind_init.sql"""
    # Base.metadata.create_all(bind=engine)  # 禁用：强制使用 SQL 初始化脚本
    logger.info("数据库初始化已跳过（使用 quantmind_init.sql）")
