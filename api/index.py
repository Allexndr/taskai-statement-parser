import os
import sys
import logging
from fastapi import FastAPI, Request, Response
from contextlib import asynccontextmanager

# Ensure project root is in path for Vercel serverless
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.bot import bot, dp
from app.database import engine, Base
from sqlalchemy.ext.asyncio import AsyncSession
from app.database import async_session
from app.reminders import get_pending_reminders, mark_reminder_sent, get_task_with_reminder
from app.bot import task_card, task_actions_keyboard
from aiogram.types import Update

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    webhook_url = os.getenv("WEBHOOK_URL")
    if webhook_url:
        try:
            await bot.set_webhook(url=webhook_url, drop_pending_updates=True)
            logger.info(f"Webhook set to {webhook_url}")
        except Exception as e:
            logger.error(f"Failed to set webhook: {e}")
    yield
    await bot.delete_webhook(drop_pending_updates=True)
    await engine.dispose()

app = FastAPI(lifespan=lifespan)

@app.post("/api/webhook")
async def telegram_webhook(request: Request):
    try:
        data = await request.json()
        update = Update.model_validate(data)
        await dp.feed_update(bot, update)
        return Response(content="OK", status_code=200)
    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return Response(content="Error", status_code=500)

@app.get("/api/reminders/check")
async def check_reminders():
    """Endpoint for cron job to check and send pending reminders."""
    sent_count = 0
    try:
        async with async_session() as session:
            reminders = await get_pending_reminders(session)
            for reminder in reminders:
                try:
                    rem, task, user = await get_task_with_reminder(session, str(reminder.id))
                    if rem and task and user:
                        from aiogram.enums import ParseMode
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

@app.get("/api/health")
async def health():
    return {"status": "ok"}
