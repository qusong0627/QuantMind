import enum

from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.sql import func

from backend.shared.database import Base


class TaskStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"

class TaskType(str, enum.Enum):
    DAILY_PREDICTION = "DAILY_PREDICTION"
    MODEL_RETRAIN = "MODEL_RETRAIN"

class SystemTask(Base):
    """
    系统后台任务表
    用于追踪耗时的离线计算任务。
    """
    __tablename__ = "system_tasks"
    
    task_id = Column(String(64), primary_key=True, comment="任务唯一ID (UUID)")
    task_type = Column(String(32), index=True, nullable=False, comment="任务类型")
    status = Column(String(20), index=True, default=TaskStatus.PENDING, comment="当前状态")
    
    progress = Column(Integer, default=0, comment="进度百分比 (0-100)")
    logs = Column(Text, comment="实时运行日志")
    result_path = Column(Text, comment="结果文件存储路径 (如CSV/Parquet)")
    error_message = Column(Text, nullable=True, comment="错误详情")
    
    created_at = Column(DateTime(timezone=True), server_default=func.now(), comment="创建时间")
    finished_at = Column(DateTime(timezone=True), nullable=True, comment="完成时间")

    def __repr__(self):
        return f"<SystemTask(id={self.task_id}, status={self.status}, progress={self.progress}%)>"
