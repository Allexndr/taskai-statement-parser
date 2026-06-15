import os
import sys
import logging
from fastapi import FastAPI, Request, Response
from contextlib import asynccontextmanager

# Ensure project root is in path for Vercel serverless
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def init_db():
    from app.database import engine, Base
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield

app = FastAPI(lifespan=lifespan)

@app.get("/api/health")
async def health():
    return {"status": "ok"}

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    try:
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
