from datetime import datetime, timedelta
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Reminder, Task, User

async def get_pending_reminders(session: AsyncSession) -> list[Reminder]:
    now = datetime.utcnow()
    # Get reminders that should fire in the last 5 minutes window
    five_min_ago = now - timedelta(minutes=5)
    query = select(Reminder).where(
        Reminder.status == "pending",
        Reminder.remind_at <= now,
        Reminder.remind_at >= five_min_ago
    )
    result = await session.execute(query)
    return result.scalars().all()

async def mark_reminder_sent(session: AsyncSession, reminder_id: str):
    await session.execute(
        update(Reminder).where(Reminder.id == reminder_id).values(status="sent")
    )
    await session.commit()

async def snooze_reminder(session: AsyncSession, reminder_id: str, minutes: int) -> Reminder:
    # Get original reminder
    result = await session.execute(select(Reminder).where(Reminder.id == reminder_id))
    original = result.scalar_one_or_none()
    if not original:
        return None
    
    new_reminder = Reminder(
        task_id=original.task_id,
        user_id=original.user_id,
        remind_at=datetime.utcnow() + timedelta(minutes=minutes),
        status="pending",
        snooze_minutes=minutes
    )
    session.add(new_reminder)
    await session.execute(
        update(Reminder).where(Reminder.id == reminder_id).values(status="snoozed")
    )
    await session.commit()
    await session.refresh(new_reminder)
    return new_reminder

async def get_task_with_reminder(session: AsyncSession, reminder_id: str) -> tuple[Reminder, Task, User]:
    from sqlalchemy.orm import joinedload
    result = await session.execute(
        select(Reminder)
        .options(joinedload(Reminder.task), joinedload(Reminder.user))
        .where(Reminder.id == reminder_id)
    )
    reminder = result.scalar_one_or_none()
    if reminder:
        return reminder, reminder.task, reminder.user
    return None, None, None
