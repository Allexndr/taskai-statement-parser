import uuid
from datetime import datetime
from sqlalchemy import Column, String, DateTime, Integer, Boolean, ForeignKey, Text, Enum, BigInteger
from sqlalchemy.dialects.postgresql import UUID, JSONB
from sqlalchemy.orm import relationship
from app.database import Base
import enum

class TaskStatus(str, enum.Enum):
    new = "new"
    in_progress = "in_progress"
    waiting = "waiting"
    done = "done"
    cancelled = "cancelled"

class TaskPriority(str, enum.Enum):
    low = "low"
    medium = "medium"
    high = "high"
    critical = "critical"

class User(Base):
    __tablename__ = "users"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    telegram_id = Column(BigInteger, unique=True, nullable=False, index=True)
    username = Column(String(255), nullable=True)
    first_name = Column(String(255), nullable=True)
    last_name = Column(String(255), nullable=True)
    timezone = Column(String(50), default="Europe/Moscow")
    language = Column(String(10), default="ru")
    partner_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    tasks = relationship("Task", foreign_keys="Task.owner_id", back_populates="owner")
    partner = relationship("User", remote_side=[id], uselist=False)

class Task(Base):
    __tablename__ = "tasks"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    assignee_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=True)
    title = Column(String(500), nullable=False)
    description = Column(Text, nullable=True)
    status = Column(Enum(TaskStatus), default=TaskStatus.new)
    priority = Column(Enum(TaskPriority), default=TaskPriority.medium)
    category = Column(String(100), nullable=True)
    due_at = Column(DateTime, nullable=True)
    is_shared = Column(Boolean, default=False)
    is_visible_to_partner = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)
    
    owner = relationship("User", foreign_keys=[owner_id], back_populates="tasks")
    assignee = relationship("User", foreign_keys=[assignee_id])
    reminders = relationship("Reminder", back_populates="task", cascade="all, delete-orphan")

class Reminder(Base):
    __tablename__ = "reminders"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    task_id = Column(UUID(as_uuid=True), ForeignKey("tasks.id"), nullable=False)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    remind_at = Column(DateTime, nullable=False)
    status = Column(String(20), default="pending")  # pending, sent, snoozed
    snooze_minutes = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    task = relationship("Task", back_populates="reminders")
    user = relationship("User")

class Personalization(Base):
    __tablename__ = "personalizations"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    frequent_categories = Column(JSONB, default=dict)
    project_keywords = Column(JSONB, default=dict)
    time_patterns = Column(JSONB, default=dict)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user = relationship("User")

class TransactionType(str, enum.Enum):
    income = "income"
    expense = "expense"

class Transaction(Base):
    __tablename__ = "transactions"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    amount = Column(Integer, nullable=False)  # в копейках/центах для точности
    type = Column(Enum(TransactionType), nullable=False)
    category = Column(String(100), nullable=False)
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    
    user = relationship("User")

class FinanceSettings(Base):
    __tablename__ = "finance_settings"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, unique=True)
    monthly_salary = Column(Integer, nullable=True)  # в копейках
    monthly_expenses = Column(Integer, nullable=True)  # примерные обязательные расходы
    currency = Column(String(10), default="RUB")
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    user = relationship("User")

class Wish(Base):
    __tablename__ = "wishes"
    
    id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id"), nullable=False)
    title = Column(String(255), nullable=False)
    target_amount = Column(Integer, nullable=False)  # в копейках
    current_amount = Column(Integer, default=0)
    priority = Column(Enum(TaskPriority), default=TaskPriority.medium)
    category = Column(String(100), nullable=True)
    deadline = Column(DateTime, nullable=True)
    status = Column(String(20), default="active")  # active, achieved, cancelled
    created_at = Column(DateTime, default=datetime.utcnow)
    achieved_at = Column(DateTime, nullable=True)
    
    user = relationship("User")
