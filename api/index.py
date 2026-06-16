import os
import sys
import logging
from fastapi import FastAPI, Request, Response

# Ensure project root is in path for Vercel serverless
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

_db_initialized = False

async def init_db():
    global _db_initialized
    if _db_initialized:
        return
    from app.database import engine, Base
    import app.models  # noqa: F401 — registers tables in Base.metadata
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    _db_initialized = True

app = FastAPI()

@app.get("/api/health")
async def health():
    return {"status": "ok"}

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    try:
        await init_db()
        from app.bot import bot, dp
        from aiogram.types import Update
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
        return Response(content="OK", status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return Response(content=f"Error: {e}", status_code=500)

@app.get("/api/reminders/check")
async def check_reminders():
    """Endpoint for cron job to check and send pending reminders."""
    sent_count = 0
    try:
        from app.database import async_session
        from app.reminders import get_pending_reminders, mark_reminder_sent, get_task_with_reminder
        from app.bot import bot, task_card, task_actions_keyboard
        from aiogram.enums import ParseMode

        async with async_session() as session:
            reminders = await get_pending_reminders(session)
            for reminder in reminders:
                try:
                    rem, task, user = await get_task_with_reminder(session, str(reminder.id))
                    if rem and task and user:
                        card = task_card(task)
                        kb = task_actions_keyboard(str(task.id))
                        await bot.send_message(
                            chat_id=user.telegram_id,
                            text=f"⏰ <b>Напоминание!</b>\n\n{card}",
                            reply_markup=kb,
                            parse_mode=ParseMode.HTML
                        )
                        await mark_reminder_sent(session, str(reminder.id))
                        sent_count += 1
                except Exception as e:
                    logger.error(f"Failed to send reminder {reminder.id}: {e}")
    except Exception as e:
        logger.error(f"Check reminders error: {e}")

    return {"status": "ok", "sent": sent_count}


@app.get("/api/reminders/daily")
async def daily_digest():
    """Evening cron: send digest + nudge if no transactions recorded today."""
    from datetime import timezone
    from app.database import async_session
    from app.bot import bot, ALLOWED_USER_ID, fmt_money
    from app.models import Transaction, TransactionType
    from sqlalchemy import select, func, and_
    from aiogram.enums import ParseMode

    now_utc = __import__('datetime').datetime.utcnow()
    # UTC+5 offset
    now_local = now_utc.replace(tzinfo=None)
    hour_local = (now_utc.hour + 5) % 24

    # Only run between 20:00-21:00 Astana time
    if not (20 <= hour_local < 21):
        return {"status": "skipped", "hour": hour_local}

    try:
        async with async_session() as session:
            from app.tasks_service import get_or_create_user
            user = await get_or_create_user(session, ALLOWED_USER_ID)

            # Check today's transactions
            today_start = now_utc.replace(hour=0, minute=0, second=0, microsecond=0) - __import__('datetime').timedelta(hours=5)
            count_r = await session.execute(
                select(func.count()).where(
                    and_(Transaction.user_id == user.id,
                         Transaction.created_at >= today_start)
                )
            )
            today_count = count_r.scalar() or 0

            # Month stats
            month_start = now_utc.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            inc_r = await session.execute(
                select(func.sum(Transaction.amount)).where(
                    and_(Transaction.user_id == user.id,
                         Transaction.type == TransactionType.income,
                         Transaction.created_at >= month_start))
            )
            exp_r = await session.execute(
                select(func.sum(Transaction.amount)).where(
                    and_(Transaction.user_id == user.id,
                         Transaction.type == TransactionType.expense,
                         Transaction.created_at >= month_start))
            )
            month_inc = inc_r.scalar() or 0
            month_exp = exp_r.scalar() or 0
            balance = month_inc - month_exp

        day = now_utc.strftime("%d.%m")
        if today_count == 0:
            text = (
                f"💡 <b>Вечернее напоминание ({day})</b>\n\n"
                f"Сегодня не зарегистрировано ни одной операции.\n\n"
                f"📅 Месяц: доход {fmt_money(month_inc)}, расходы {fmt_money(month_exp)}, баланс {fmt_money(balance)}\n\n"
                f"Что потратил сегодня? Напиши мне!"
            )
        else:
            text = (
                f"📊 <b>Итог дня ({day})</b>\n\n"
                f"Записей сегодня: {today_count}\n"
                f"📅 За месяц: доход {fmt_money(month_inc)}, расходы {fmt_money(month_exp)}\n"
                f"💾 Баланс: {fmt_money(balance)}"
            )

        await bot.send_message(
            chat_id=ALLOWED_USER_ID,
            text=text,
            parse_mode=ParseMode.HTML
        )
        return {"status": "sent", "today_count": today_count}
    except Exception as e:
        logger.error(f"Daily digest error: {e}")
        return {"status": "error", "detail": str(e)}
