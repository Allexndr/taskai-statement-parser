from datetime import datetime, timedelta
from uuid import UUID
from sqlalchemy import select, and_, or_
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import User, Task, Reminder, TaskStatus, TaskPriority
from app.ai_parser import parse_task_text

async def get_or_create_user(session: AsyncSession, telegram_id: int, username: str = None, 
                              first_name: str = None, last_name: str = None) -> User:
    result = await session.execute(select(User).where(User.telegram_id == telegram_id))
    user = result.scalar_one_or_none()
    if not user:
        user = User(
            telegram_id=telegram_id,
            username=username,
            first_name=first_name,
            last_name=last_name
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user

async def create_task_from_parsed(session: AsyncSession, owner_id: UUID, parsed: dict, 
                                   assignee_id: UUID = None, is_shared: bool = False) -> Task:
    task = Task(
        owner_id=owner_id,
        assignee_id=assignee_id or owner_id,
        title=parsed["title"],
        description=parsed.get("description"),
        category=parsed.get("category", "Другое"),
        priority=parsed.get("priority", "medium"),
        status=TaskStatus.new,
        due_at=datetime.fromisoformat(parsed["due_at"].replace('Z', '+00:00')) if parsed.get("due_at") else None,
        is_shared=is_shared,
        is_visible_to_partner=is_shared
    )
    session.add(task)
    await session.commit()
    await session.refresh(task)
    
    # Create default reminders if due_at exists
    if task.due_at:
        await _create_default_reminders(session, task)
    
    return task

async def _create_default_reminders(session: AsyncSession, task: Task):
    now = datetime.utcnow()
    due = task.due_at
    
    reminders = []
    # 1 day before
    if due - timedelta(days=1) > now:
        reminders.append(Reminder(task_id=task.id, user_id=task.assignee_id, remind_at=due - timedelta(days=1)))
    # 2 hours before
    if due - timedelta(hours=2) > now:
        reminders.append(Reminder(task_id=task.id, user_id=task.assignee_id, remind_at=due - timedelta(hours=2)))
    # 15 minutes before
    if due - timedelta(minutes=15) > now:
        reminders.append(Reminder(task_id=task.id, user_id=task.assignee_id, remind_at=due - timedelta(minutes=15)))
    
    for r in reminders:
        session.add(r)
    await session.commit()

async def get_user_tasks(session: AsyncSession, user_id: UUID, status: str = None, 
                         category: str = None, limit: int = 20) -> list[Task]:
    query = select(Task).where(
        or_(
            Task.owner_id == user_id,
            and_(Task.is_shared == True, Task.is_visible_to_partner == True)
        )
    )
    if status:
        query = query.where(Task.status == status)
    if category:
        query = query.where(Task.category == category)
    
    query = query.order_by(Task.due_at.asc().nullslast(), Task.created_at.desc()).limit(limit)
    result = await session.execute(query)
    return result.scalars().all()

async def get_today_tasks(session: AsyncSession, user_id: UUID) -> list[Task]:
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_end = today_start + timedelta(days=1)
    
    query = select(Task).where(
        and_(
            or_(
                Task.owner_id == user_id,
                and_(Task.is_shared == True, Task.is_visible_to_partner == True)
            ),
            Task.status.in_([TaskStatus.new, TaskStatus.in_progress]),
            or_(
                Task.due_at.between(today_start, today_end),
                Task.due_at < today_start
            )
        )
    ).order_by(Task.due_at.asc().nullslast())
    
    result = await session.execute(query)
    return result.scalars().all()

