import os
import asyncio
from datetime import datetime, timedelta
from aiogram import Bot, Dispatcher, types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton
from aiogram.filters import Command
from aiogram.enums import ParseMode

from app.database import async_session
from app.models import TaskStatus, TaskPriority, TransactionType, Task, Reminder
from app.tasks_service import get_or_create_user, create_task_from_parsed, get_user_tasks, get_today_tasks
from app.ai_parser import parse_task_text, unified_parse, agent_parse
from app.stt import transcribe_voice
from app.finance_service import (
    add_transaction, get_monthly_stats, get_or_create_finance_settings,
    update_finance_settings, calculate_savings_rate, add_wish,
    add_to_wish, get_active_wishes, calculate_wish_timeline, FINANCE_CATEGORIES
)
from app.finance_ai_parser import parse_finance_text, parse_wish_text, parse_finance_settings
from sqlalchemy import select, update, func, and_
try:
    from app.sheets_service import get_sheet_context, add_operation_to_sheet
    SHEETS_ENABLED = True
except Exception:
    SHEETS_ENABLED = False

bot = Bot(token=os.getenv("BOT_TOKEN"))
dp = Dispatcher()

CONFIDENCE_THRESHOLD = 75
PAGE_SIZE = 5
ALLOWED_USER_ID = 5450018125

# In-memory session context: stores last action per user for undo/edit
_session: dict = {}  # user_id -> {"last_tx_id": ..., "last_action": ...}


CURRENCY = "₸"

def fmt_money(amount: int) -> str:
    return f"{amount/100:,.0f}{CURRENCY}".replace(",", " ")


def kb(buttons: list) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def main_menu() -> InlineKeyboardMarkup:
    return kb([
        [InlineKeyboardButton(text="📝 Задачи", callback_data="menu:tasks"),
         InlineKeyboardButton(text="💰 Финансы", callback_data="menu:finance")],
        [InlineKeyboardButton(text="🎯 Желания", callback_data="menu:wishes"),
         InlineKeyboardButton(text="📊 Отчёт", callback_data="finance:report")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="menu:settings")],
    ])


def back_btn(target: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text="← Назад", callback_data=target)


def task_emoji(status: str) -> str:
    return {"new": "🆕", "in_progress": "⏳", "waiting": "⏸️",
            "done": "✅", "cancelled": "❌"}.get(status, "📝")


def prio_emoji(priority: str) -> str:
    return {"low": "🟢", "medium": "🟡", "high": "🟠", "critical": "🔴"}.get(priority, "⚪")


def task_card(task) -> str:
    due_str = f"\n📅 {task.due_at.strftime('%d.%m %H:%M')}" if task.due_at else ""
    cat_str = f"\n📂 {task.category}" if task.category else ""
    shared = "\n👥 Общая" if task.is_shared else "\n🔒 Личная"
    return f"{task_emoji(task.status)} <b>{task.title}</b>\n{prio_emoji(task.priority)} {task.priority}{due_str}{cat_str}{shared}"


async def edit_to_main(callback: types.CallbackQuery):
    await callback.message.edit_text(
        "<b>🤖 Taski — Главное меню</b>\n\nВыбери раздел:",
        reply_markup=main_menu(), parse_mode=ParseMode.HTML
    )


# ═══ START / MENU ═══

def is_allowed(user_id: int) -> bool:
    return user_id == ALLOWED_USER_ID


@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    async with async_session() as session:
        await get_or_create_user(session, telegram_id=message.from_user.id,
            username=message.from_user.username, first_name=message.from_user.first_name,
            last_name=message.from_user.last_name)
    await message.answer(
        "<b>🤖 Taski</b> — твой AI-ассистент\n\n"
        "• 📝 Задачи с AI-напоминаниями\n"
        "• 💰 Финансы и накопления\n"
        "• 🎯 Желания с расчётом срока\n\n"
        "Просто напиши или скажи голосом что угодно — я пойму!",
        reply_markup=main_menu(), parse_mode=ParseMode.HTML)


@dp.message(Command("menu"))
async def cmd_menu(message: types.Message):
    await message.answer("<b>🤖 Taski — Главное меню</b>", reply_markup=main_menu(), parse_mode=ParseMode.HTML)


@dp.message(Command("report", "отчет", "отчёт"))
async def cmd_report(message: types.Message):
    if not is_allowed(message.from_user.id): return
    class FakeCallback:
        from_user = message.from_user
        async def answer(self, *a, **kw): pass
        class message:
            pass
    cb = FakeCallback()
    cb.message = message
    cb.message.edit_text = message.answer
    await render_finance_report(cb)


@dp.message(Command("balance", "баланс"))
async def cmd_balance(message: types.Message):
    if not is_allowed(message.from_user.id): return
    class FakeCallback:
        from_user = message.from_user
        async def answer(self, *a, **kw): pass
        class message:
            pass
    cb = FakeCallback()
    cb.message = message
    cb.message.edit_text = message.answer
    await render_finance_balance(cb)


@dp.message(Command("last", "история"))
async def cmd_last(message: types.Message):
    if not is_allowed(message.from_user.id): return
    class FakeCallback:
        from_user = message.from_user
        async def answer(self, *a, **kw): pass
        class message:
            pass
    cb = FakeCallback()
    cb.message = message
    cb.message.edit_text = message.answer
    await render_finance_history(cb, 0)


@dp.message(Command("help", "помощь"))
async def cmd_help(message: types.Message):
    if not is_allowed(message.from_user.id): return
    await message.answer(
        "<b>🤖 Taski — что умею</b>\n\n"
        "<b>💬 Просто напиши:</b>\n"
        "• <code>Потратил 2000 на кофе</code> → запишу расход\n"
        "• <code>Получил 150000 зарплата</code> → запишу доход\n"
        "• <code>Хочу MacBook за 200к</code> → добавлю цель\n"
        "• <code>Отмени последнюю операцию</code> → удалю\n"
        "• <code>Сколько потратил в этом месяце?</code> → отвечу\n"
        "• <code>На что последний расход?</code> → покажу\n"
        "• <code>Как идут накопления на отпуск?</code> → посчитаю\n\n"
        "<b>⚡ Быстрые команды:</b>\n"
        "/report — отчёт за месяц\n"
        "/balance — баланс\n"
        "/last — история операций\n"
        "/menu — главное меню\n\n"
        "<b>🎙 Голос:</b> Говори — распознаю и обработаю",
        reply_markup=main_menu(), parse_mode=ParseMode.HTML)


# ═══ TASKS MENU ═══

def tasks_menu_kb() -> InlineKeyboardMarkup:
    return kb([
        [InlineKeyboardButton(text="➕ Добавить задачу", callback_data="task:add")],
        [InlineKeyboardButton(text="📅 На сегодня", callback_data="tasks:today")],
        [InlineKeyboardButton(text="📋 Все задачи", callback_data="tasks:list:0")],
        [InlineKeyboardButton(text="📂 По категориям", callback_data="tasks:categories")],
        [back_btn("menu:main")],
    ])


async def render_task_list(callback: types.CallbackQuery, page: int, status_filter=None):
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        tasks = await get_user_tasks(session, user.id, status=status_filter, limit=100)

    if not tasks:
        await callback.message.edit_text(
            "📭 Нет задач.\n\nНажми «Добавить задачу» или просто напиши мне.",
            reply_markup=kb([
                [InlineKeyboardButton(text="➕ Добавить", callback_data="task:add")],
                [back_btn("menu:tasks")],
            ]), parse_mode=ParseMode.HTML)
        await callback.answer()
        return

    total = len(tasks)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_tasks = tasks[start:end]
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    text = f"<b>📋 Задачи</b> (стр. {page+1}/{total_pages})\n\n"
    buttons = []

    for t in page_tasks:
        text += f"{task_emoji(t.status)} {t.title[:30]}{'...' if len(t.title)>30 else ''}\n"
        buttons.append([InlineKeyboardButton(
            text=f"{'✅' if t.status=='done' else '⏳' if t.status=='in_progress' else '○'} {t.title[:25]}",
            callback_data=f"task:view:{t.id}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"tasks:list:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"tasks:list:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([back_btn("menu:tasks")])

    await callback.message.edit_text(text, reply_markup=kb(buttons), parse_mode=ParseMode.HTML)
    await callback.answer()


async def render_task_detail(callback: types.CallbackQuery, task_id: str):
    async with async_session() as session:
        result = await session.execute(select(Task).where(Task.id == task_id))
        task = result.scalar_one_or_none()

    if not task:
        await callback.answer("Задача не найдена", show_alert=True)
        return

    card = task_card(task)
    buttons = []

    if task.status != TaskStatus.done.value:
        buttons.append([
            InlineKeyboardButton(text="✅ Сделано", callback_data=f"task:done:{task.id}"),
            InlineKeyboardButton(text="⏳ В работу", callback_data=f"task:progress:{task.id}"),
        ])
    else:
        buttons.append([InlineKeyboardButton(text="🔄 Вернуть", callback_data=f"task:reopen:{task.id}")])

    buttons.append([
        InlineKeyboardButton(text="✏️ Название", callback_data=f"task:edit:{task.id}:title"),
        InlineKeyboardButton(text="📅 Дедлайн", callback_data=f"task:edit:{task.id}:due"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🔴 Приоритет", callback_data=f"task:edit:{task.id}:priority"),
        InlineKeyboardButton(text="📂 Категория", callback_data=f"task:edit:{task.id}:category"),
    ])
    buttons.append([
        InlineKeyboardButton(text="👥 Общая" if not task.is_shared else "🔒 Личная",
            callback_data=f"task:toggle_share:{task.id}"),
        InlineKeyboardButton(text="❌ Удалить", callback_data=f"task:delete:{task.id}"),
    ])
    buttons.append([
        InlineKeyboardButton(text="🕐 +15м", callback_data=f"task:snooze:{task.id}:15"),
        InlineKeyboardButton(text="🕐 +1ч", callback_data=f"task:snooze:{task.id}:60"),
        InlineKeyboardButton(text="📅 Завтра", callback_data=f"task:snooze:{task.id}:1440"),
    ])
    buttons.append([back_btn("tasks:list:0")])

    await callback.message.edit_text(
        f"{card}\n\n<i>Нажми кнопку для действия</i>",
        reply_markup=kb(buttons), parse_mode=ParseMode.HTML)
    await callback.answer()


# ═══ FINANCE MENU ═══

def finance_menu_kb() -> InlineKeyboardMarkup:
    return kb([
        [InlineKeyboardButton(text="📊 Баланс", callback_data="finance:balance"),
         InlineKeyboardButton(text="📈 Отчёт", callback_data="finance:report")],
        [InlineKeyboardButton(text="➕ Доход", callback_data="finance:add_income"),
         InlineKeyboardButton(text="➖ Расход", callback_data="finance:add_expense")],
        [InlineKeyboardButton(text="📋 История", callback_data="finance:list:0")],
        [InlineKeyboardButton(text="⚙️ Зарплата", callback_data="finance:settings")],
        [back_btn("menu:main")],
    ])


def bar(amount: int, max_amount: int, width: int = 10) -> str:
    if max_amount == 0:
        return '░' * width
    filled = round(amount / max_amount * width)
    return '█' * filled + '░' * (width - filled)


async def render_finance_report(callback: types.CallbackQuery):
    """Monthly report: categories bar chart + prev month comparison."""
    from app.models import Transaction
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        now = datetime.utcnow()
        # Current month
        cur_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        # Prev month
        prev_end = cur_start - timedelta(seconds=1)
        prev_start = prev_end.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        def month_query(start, end, tx_type):
            return select(Transaction.category, func.sum(Transaction.amount)).where(
                and_(Transaction.user_id == user.id,
                     Transaction.type == tx_type,
                     Transaction.created_at >= start,
                     Transaction.created_at <= end)
            ).group_by(Transaction.category).order_by(func.sum(Transaction.amount).desc())

        cur_exp_r = await session.execute(month_query(cur_start, now, TransactionType.expense))
        cur_inc_r = await session.execute(
            select(func.sum(Transaction.amount)).where(
                and_(Transaction.user_id == user.id, Transaction.type == TransactionType.income,
                     Transaction.created_at >= cur_start))
        )
        prev_exp_r = await session.execute(month_query(prev_start, prev_end, TransactionType.expense))
        prev_inc_r = await session.execute(
            select(func.sum(Transaction.amount)).where(
                and_(Transaction.user_id == user.id, Transaction.type == TransactionType.income,
                     Transaction.created_at >= prev_start, Transaction.created_at <= prev_end))
        )

    cur_cats = {c: v for c, v in cur_exp_r.all()}
    prev_cats = {c: v for c, v in prev_exp_r.all()}
    cur_inc = cur_inc_r.scalar() or 0
    prev_inc = prev_inc_r.scalar() or 0
    cur_exp = sum(cur_cats.values())
    prev_exp = sum(prev_cats.values())
    max_cat = max(cur_cats.values(), default=1)

    # Also enrich from Google Sheets if available
    sheets_exp = 0
    sheets_inc = 0
    if SHEETS_ENABLED:
        try:
            sh_ctx = await asyncio.get_event_loop().run_in_executor(None, get_sheet_context)
            sheets_exp = sh_ctx.get("month_expense", 0)
            sheets_inc = sh_ctx.get("month_income", 0)
            if not cur_cats and sh_ctx.get("month_by_cat"):
                cur_cats = sh_ctx["month_by_cat"]
                max_cat = max(cur_cats.values(), default=1)
                cur_exp = sheets_exp
                cur_inc = sheets_inc
        except Exception:
            pass

    mon = now.strftime("%B %Y")
    text = f"<b>📊 Отчёт: {mon}</b>\n\n"
    text += f"➕ Доход:  {fmt_money(cur_inc or sheets_inc)}\n"
    text += f"➖ Расходы: {fmt_money(cur_exp or sheets_exp)}\n"
    text += f"💾 Баланс:  {fmt_money((cur_inc or sheets_inc) - (cur_exp or sheets_exp))}\n"

    # Prev month diff
    if prev_exp > 0 and cur_exp > 0:
        diff = cur_exp - prev_exp
        arrow = "📈" if diff > 0 else "📉"
        text += f"{arrow} vs прошлый месяц: {'+' if diff>0 else ''}{fmt_money(diff)}\n"

    if cur_cats:
        text += "\n<b>Расходы по категориям:</b>\n"
        for cat, amt in sorted(cur_cats.items(), key=lambda x: x[1], reverse=True)[:10]:
            b = bar(amt, max_cat, 8)
            prev = prev_cats.get(cat, 0)
            trend = " 📈" if amt > prev * 1.2 and prev > 0 else (" 📉" if amt < prev * 0.8 and prev > 0 else "")
            text += f"<code>[{b}]</code> {fmt_money(amt)} {cat}{trend}\n"
    else:
        text += "\n<i>Нет данных за этот месяц. Запиши первую трату!</i>"

    await callback.message.edit_text(text, reply_markup=kb([
        [InlineKeyboardButton(text="📋 История", callback_data="finance:list:0")],
        [back_btn("menu:finance")],
    ]), parse_mode=ParseMode.HTML)
    await callback.answer()


async def render_finance_history(callback: types.CallbackQuery, page: int = 0):
    """Paginated transaction history."""
    from app.models import Transaction
    PAGE = 10
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        count_r = await session.execute(
            select(func.count()).where(Transaction.user_id == user.id)
        )
        total = count_r.scalar() or 0
        txs_r = await session.execute(
            select(Transaction)
            .where(Transaction.user_id == user.id)
            .order_by(Transaction.created_at.desc())
            .offset(page * PAGE).limit(PAGE)
        )
        txs = txs_r.scalars().all()

    if not txs:
        await callback.message.edit_text(
            "📭 Нет операций.",
            reply_markup=kb([[back_btn("menu:finance")]]), parse_mode=ParseMode.HTML)
        await callback.answer()
        return

    total_pages = (total + PAGE - 1) // PAGE
    text = f"<b>📋 История операций</b> (стр. {page+1}/{total_pages})\n\n"
    for t in txs:
        sign = "➕" if t.type == TransactionType.income else "➖"
        d = t.created_at.strftime("%d.%m") if t.created_at else "?"
        desc = f" — {t.description[:25]}" if t.description else ""
        text += f"{sign} {d} <b>{fmt_money(t.amount)}</b> [{t.category}]{desc}\n"

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"finance:list:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if page + 1 < total_pages:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"finance:list:{page+1}"))

    buttons = []
    if nav:
        buttons.append(nav)
    buttons.append([back_btn("menu:finance")])
    await callback.message.edit_text(text, reply_markup=kb(buttons), parse_mode=ParseMode.HTML)
    await callback.answer()


async def render_finance_balance(callback: types.CallbackQuery):
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        stats = await get_monthly_stats(session, user.id)
        settings = await get_or_create_finance_settings(session, user.id)
        savings = await calculate_savings_rate(session, user.id)

    text = (
        f"<b>💰 Финансы за месяц</b>\n\n"
        f"➕ Доход: {fmt_money(stats['income'])}\n"
        f"➖ Расходы: {fmt_money(stats['expenses'])}\n"
        f"📊 Баланс: {fmt_money(stats['balance'])}\n\n"
    )
    if settings.monthly_salary:
        text += (
            f"<b>📈 Накопления</b>\n"
            f"Зарплата: {fmt_money(settings.monthly_salary)}\n"
            f"Расходы: {fmt_money(settings.monthly_expenses or 0)}\n"
            f"Потенциал: {fmt_money(savings['savings_potential'])}\n"
            f"Ставка: {savings['savings_rate']}%\n\n"
        )
    if stats['by_category']:
        text += "<b>📂 Топ категории:</b>\n"
        for cat, amt in list(stats['by_category'].items())[:5]:
            text += f"  {cat}: {fmt_money(amt)}\n"

    await callback.message.edit_text(
        text,
        reply_markup=kb([
            [InlineKeyboardButton(text="➕ Доход", callback_data="finance:add_income")],
            [InlineKeyboardButton(text="➖ Расход", callback_data="finance:add_expense")],
            [back_btn("menu:finance")],
        ]), parse_mode=ParseMode.HTML)
    await callback.answer()


# ═══ WISHES MENU ═══

def wishes_menu_kb() -> InlineKeyboardMarkup:
    return kb([
        [InlineKeyboardButton(text="➕ Добавить желание", callback_data="wish:add")],
        [InlineKeyboardButton(text="🎯 Мои желания", callback_data="wishes:list:0")],
        [back_btn("menu:main")],
    ])


async def render_wish_list(callback: types.CallbackQuery, page: int = 0):
    async with async_session() as session:
        user = await get_or_create_user(session, callback.from_user.id)
        wishes = await get_active_wishes(session, user.id)

    if not wishes:
        await callback.message.edit_text(
            "🎯 Пока нет желаний.\n\nПросто напиши: «Хочу MacBook за 200000»",
            reply_markup=kb([
                [InlineKeyboardButton(text="➕ Добавить", callback_data="wish:add")],
                [back_btn("menu:wishes")],
            ]), parse_mode=ParseMode.HTML)
        await callback.answer()
        return

    total = len(wishes)
    start = page * PAGE_SIZE
    end = start + PAGE_SIZE
    page_items = wishes[start:end]
    total_pages = (total + PAGE_SIZE - 1) // PAGE_SIZE

    text = f"<b>🎯 Желания</b> (стр. {page+1}/{total_pages})\n\n"
    buttons = []

    for w in page_items:
        pct = (w.current_amount / w.target_amount * 100) if w.target_amount else 0
        bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
        text += f"{bar} {pct:.0f}% {w.title[:20]}\n"
        buttons.append([InlineKeyboardButton(
            text=f"{w.title[:28]}{'...' if len(w.title)>28 else ''} — {fmt_money(w.current_amount)}/{fmt_money(w.target_amount)}",
            callback_data=f"wish:view:{w.id}")])

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton(text="◀️", callback_data=f"wishes:list:{page-1}"))
    nav.append(InlineKeyboardButton(text=f"{page+1}/{total_pages}", callback_data="noop"))
    if end < total:
        nav.append(InlineKeyboardButton(text="▶️", callback_data=f"wishes:list:{page+1}"))
    if nav:
        buttons.append(nav)
    buttons.append([back_btn("menu:wishes")])

    await callback.message.edit_text(text, reply_markup=kb(buttons), parse_mode=ParseMode.HTML)
    await callback.answer()


async def render_wish_detail(callback: types.CallbackQuery, wish_id: str):
    async with async_session() as session:
        from app.models import Wish
        result = await session.execute(select(Wish).where(Wish.id == wish_id))
        wish = result.scalar_one_or_none()
        timeline = None
        if wish:
            timeline = await calculate_wish_timeline(session, wish.user_id, wish)

    if not wish:
        await callback.answer("Желание не найдено", show_alert=True)
        return

    pct = (wish.current_amount / wish.target_amount * 100) if wish.target_amount else 0
    bar = "█" * int(pct / 10) + "░" * (10 - int(pct / 10))
    text = (
        f"<b>🎯 {wish.title}</b>\n\n"
        f"[{bar}] {pct:.1f}%\n"
        f"💰 {fmt_money(wish.current_amount)} / {fmt_money(wish.target_amount)}\n"
        f"📂 {wish.category or 'Другое'}\n"
    )
    if timeline and timeline.get("months") is not None:
        text += f"⏱ ~{timeline['months']} мес до цели\n"
        text += f"📅 {timeline.get('completion_date', '')}\n"

    buttons = [
        [InlineKeyboardButton(text="💰 +5 000₸", callback_data=f"wish:save:{wish.id}:500000"),
         InlineKeyboardButton(text="💰 +10 000₸", callback_data=f"wish:save:{wish.id}:1000000")],
        [InlineKeyboardButton(text="💰 +50 000₸", callback_data=f"wish:save:{wish.id}:5000000"),
         InlineKeyboardButton(text="💰 +100 000₸", callback_data=f"wish:save:{wish.id}:10000000")],
        [InlineKeyboardButton(text="❌ Удалить", callback_data=f"wish:delete:{wish.id}")],
        [back_btn("wishes:list:0")],
    ]
    await callback.message.edit_text(text, reply_markup=kb(buttons), parse_mode=ParseMode.HTML)
    await callback.answer()


# ═══ CALLBACK ROUTER ═══

@dp.callback_query()
async def handle_callback(callback: types.CallbackQuery):
    if not is_allowed(callback.from_user.id):
        await callback.answer()
        return
    data = callback.data
    parts = data.split(":")
    action = parts[0]

    try:
        if data == "noop":
            await callback.answer()
            return

        if data == "menu:main":
            await edit_to_main(callback)
            return

        if data == "menu:tasks":
            await callback.message.edit_text(
                "<b>📝 Задачи</b>\n\nЧто делаем?",
                reply_markup=tasks_menu_kb(), parse_mode=ParseMode.HTML)
            await callback.answer()
            return

        if data == "tasks:today":
            async with async_session() as session:
                user = await get_or_create_user(session, callback.from_user.id)
                tasks = await get_today_tasks(session, user.id)
            if not tasks:
                text = "🎉 На сегодня нет задач!"
            else:
                text = "<b>📅 Задачи на сегодня:</b>\n\n"
                for t in tasks:
                    text += f"{task_emoji(t.status)} {t.title}\n"
            await callback.message.edit_text(text, reply_markup=kb([
                [InlineKeyboardButton(text="📋 Все задачи", callback_data="tasks:list:0")],
                [back_btn("menu:tasks")],
            ]), parse_mode=ParseMode.HTML)
            await callback.answer()
            return

        if data == "tasks:categories":
            cats = ["Работа","Дом","Финансы","Здоровье","Учёба","Покупки","Отношения","Хобби","Другое"]
            btns = [[InlineKeyboardButton(text=cat, callback_data=f"tasks:cat:{cat}:0")] for cat in cats]
            btns.append([back_btn("menu:tasks")])
            await callback.message.edit_text("<b>📂 Выбери категорию:</b>", reply_markup=kb(btns), parse_mode=ParseMode.HTML)
            await callback.answer()
            return

        if action == "tasks" and parts[1] == "list":
            page = int(parts[2]) if len(parts) > 2 else 0
            await render_task_list(callback, page)
            return

        if action == "task" and parts[1] == "view":
            await render_task_detail(callback, parts[2])
            return

        if action == "task" and parts[1] == "done":
            async with async_session() as session:
                await session.execute(update(Task).where(Task.id == parts[2]).values(
                    status=TaskStatus.done, completed_at=datetime.utcnow()))
                await session.commit()
            await callback.answer("✅ Выполнено!")
            await render_task_detail(callback, parts[2])
            return

        if action == "task" and parts[1] == "progress":
            async with async_session() as session:
                await session.execute(update(Task).where(Task.id == parts[2]).values(status=TaskStatus.in_progress))
                await session.commit()
            await callback.answer("⏳ В работе")
            await render_task_detail(callback, parts[2])
            return

        if action == "task" and parts[1] == "reopen":
            async with async_session() as session:
                await session.execute(update(Task).where(Task.id == parts[2]).values(status=TaskStatus.new, completed_at=None))
                await session.commit()
            await callback.answer("🔄 Вернуто")
            await render_task_detail(callback, parts[2])
            return

        if action == "task" and parts[1] == "delete":
            async with async_session() as session:
                await session.execute(update(Task).where(Task.id == parts[2]).values(status=TaskStatus.cancelled))
                await session.commit()
            await callback.answer("❌ Удалено")
            await render_task_list(callback, 0)
            return

        if action == "task" and parts[1] == "toggle_share":
            async with async_session() as session:
                result = await session.execute(select(Task).where(Task.id == parts[2]))
                t = result.scalar_one_or_none()
                if t:
                    new_shared = not t.is_shared
                    await session.execute(update(Task).where(Task.id == parts[2]).values(
                        is_shared=new_shared, is_visible_to_partner=new_shared))
                    await session.commit()
            await callback.answer("👥 Обновлено")
            await render_task_detail(callback, parts[2])
            return

        if action == "task" and parts[1] == "snooze":
            minutes = int(parts[3])
            async with async_session() as session:
                result = await session.execute(select(Task).where(Task.id == parts[2]))
                task = result.scalar_one_or_none()
                if task:
                    r = Reminder(task_id=task.id, user_id=task.owner_id,
                        remind_at=datetime.utcnow() + timedelta(minutes=minutes), status="pending")
                    session.add(r)
                    await session.commit()
            await callback.answer(f"⏰ +{minutes} мин")
            await render_task_detail(callback, parts[2])
            return

        if data == "task:add":
            await callback.message.edit_text(
                "📝 <b>Добавить задачу</b>\n\nПросто напиши, например:\n<code>Завтра в 10 созвон с клиентом !важно</code>",
                reply_markup=kb([[back_btn("menu:tasks")]]), parse_mode=ParseMode.HTML)
            await callback.answer()
            return

        # ── FINANCE ──
        if data == "menu:finance":
            await callback.message.edit_text(
                "<b>💰 Финансы</b>\n\nЧто делаем?",
                reply_markup=finance_menu_kb(), parse_mode=ParseMode.HTML)
            await callback.answer()
            return

        if data == "finance:balance":
            await render_finance_balance(callback)
            return

        if data == "finance:report":
            await render_finance_report(callback)
            return

        if action == "finance" and parts[1] == "list":
            page = int(parts[2]) if len(parts) > 2 else 0
            await render_finance_history(callback, page)
            return

        if data == "finance:add_income":
            await callback.message.edit_text(
                "💵 <b>Запись дохода</b>\n\nНапиши, например:\n<code>Получил зарплату 150000</code>",
                reply_markup=kb([[back_btn("menu:finance")]]), parse_mode=ParseMode.HTML)
            await callback.answer()
            return

        if data == "finance:add_expense":
            await callback.message.edit_text(
                "💸 <b>Запись расхода</b>\n\nНапиши, например:\n<code>Потратил 500 на кофе</code>",
                reply_markup=kb([[back_btn("menu:finance")]]), parse_mode=ParseMode.HTML)
            await callback.answer()
            return

        if data == "finance:settings":
            await callback.message.edit_text(
                "💰 <b>Настройки зарплаты</b>\n\nНапиши, например:\n<code>ЗП 150000, расходы 80000</code>",
                reply_markup=kb([[back_btn("menu:finance")]]), parse_mode=ParseMode.HTML)
            await callback.answer()
            return

        # ── WISHES ──
        if data == "menu:wishes":
            await callback.message.edit_text(
                "<b>🎯 Желания</b>\n\nЧто делаем?",
                reply_markup=wishes_menu_kb(), parse_mode=ParseMode.HTML)
            await callback.answer()
            return

        if data == "wish:add":
            await callback.message.edit_text(
                "🎯 <b>Добавить желание</b>\n\nНапиши, например:\n<code>Хочу MacBook за 200000</code>",
                reply_markup=kb([[back_btn("menu:wishes")]]), parse_mode=ParseMode.HTML)
            await callback.answer()
            return

        if action == "wishes" and parts[1] == "list":
            page = int(parts[2]) if len(parts) > 2 else 0
            await render_wish_list(callback, page)
            return

        if action == "wish" and parts[1] == "view":
            await render_wish_detail(callback, parts[2])
            return

        if action == "wish" and parts[1] == "save":
            amount = int(parts[3])
            async with async_session() as session:
                w = await add_to_wish(session, parts[2], amount)
            if w and w.status == "achieved":
                await callback.answer("🎉 Цель достигнута!", show_alert=True)
            else:
                await callback.answer(f"💰 +{fmt_money(amount)}")
            await render_wish_detail(callback, parts[2])
            return

        # ── TRANSACTION CANCEL (inline button after recording) ──
        if action == "tx" and parts[1] == "cancel":
            from app.models import Transaction
            tx_id = parts[2]
            async with async_session() as session:
                result = await session.execute(select(Transaction).where(Transaction.id == tx_id))
                tx = result.scalar_one_or_none()
                if tx:
                    amt = tx.amount
                    cat = tx.category
                    await session.delete(tx)
                    await session.commit()
                    _session.pop(callback.from_user.id, None)
                    await callback.message.edit_text(
                        f"🗑 <b>Операция отменена</b>\n\nУдалено: {fmt_money(amt)} — {cat}",
                        reply_markup=main_menu(), parse_mode=ParseMode.HTML)
                else:
                    await callback.answer("Операция не найдена", show_alert=True)
            await callback.answer()
            return

        if action == "wish" and parts[1] == "delete":
            async with async_session() as session:
                from app.models import Wish
                await session.execute(update(Wish).where(Wish.id == parts[2]).values(status="cancelled"))
                await session.commit()
            await callback.answer("❌ Удалено")
            await render_wish_list(callback, 0)
            return

        # ── SETTINGS ──
        if data == "menu:settings":
            await callback.message.edit_text("<b>⚙️ Настройки</b>", reply_markup=kb([
                [InlineKeyboardButton(text="👥 Добавить девушку", callback_data="settings:partner")],
                [InlineKeyboardButton(text="💰 Зарплата", callback_data="finance:settings")],
                [back_btn("menu:main")],
            ]), parse_mode=ParseMode.HTML)
            await callback.answer()
            return

        await callback.answer()
    except Exception as e:
        await callback.answer(f"Ошибка: {str(e)[:50]}", show_alert=True)


# ═══ MESSAGE HANDLER (Free text / voice) ═══

async def get_user_context(session, user_id: str) -> dict:
    """Load full user context from DB for the agent. Sequential to avoid session concurrency issues."""
    ctx = {}

    # Finance settings
    fs = await get_or_create_finance_settings(session, user_id)
    ctx["salary"] = fs.monthly_salary
    ctx["fixed_expenses"] = fs.monthly_expenses

    # Monthly stats (3 queries inside but all sequential, fast)
    stats = await get_monthly_stats(session, user_id)
    ctx["monthly_income"] = stats["income"]
    ctx["monthly_expenses"] = stats["expenses"]

    # Calculate savings inline — no redundant DB calls
    salary = fs.monthly_salary or 0
    fixed_exp = fs.monthly_expenses or 0
    actual_exp = stats["expenses"]
    if salary > 0:
        ctx["savings_potential"] = salary - max(fixed_exp, actual_exp)
    else:
        ctx["savings_potential"] = stats["income"] - actual_exp

    # Top 3 expenses
    top = dict(sorted(stats["by_category"].items(), key=lambda x: x[1], reverse=True)[:3])
    ctx["top_expenses"] = ", ".join([f"{k}: {v/100:,.0f}₸" for k, v in top.items()]) if top else None

    # Tasks
    tasks = await get_user_tasks(session, user_id, status="new", limit=20)
    ctx["active_tasks_count"] = len(tasks)

    today = await get_today_tasks(session, user_id)
    ctx["today_tasks"] = [t.title for t in today[:5]] if today else []

    # Wishes
    wishes = await get_active_wishes(session, user_id)
    ctx["active_wishes"] = [
        {"title": w.title, "target": w.target_amount, "current": w.current_amount}
        for w in wishes[:5]
    ] if wishes else []

    # Last 10 transactions from DB
    from app.models import Transaction
    tx_result = await session.execute(
        select(Transaction)
        .where(Transaction.user_id == user_id)
        .order_by(Transaction.created_at.desc())
        .limit(10)
    )
    last_txs = tx_result.scalars().all()
    ctx["last_transactions"] = [
        {
            "date": t.created_at.strftime("%d.%m.%Y") if t.created_at else "?",
            "type": t.type.value,
            "category": t.category,
            "amount": t.amount,
            "description": (t.description or "")[:60],
        }
        for t in last_txs
    ]

    return ctx


async def handle_free_text(message: types.Message, text: str):
    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        try:
            context = await asyncio.wait_for(get_user_context(session, user.id), timeout=4.0)
        except Exception:
            context = {}

    # Enrich context with Google Sheets data
    if SHEETS_ENABLED:
        try:
            sheets_ctx = await asyncio.get_event_loop().run_in_executor(None, get_sheet_context)
            context["sheets"] = sheets_ctx
            # Merge key financial data from sheets into context
            s = sheets_ctx["settings"]
            if not context.get("salary") and s.get("income_kcell_summer"):
                context["salary"] = s["income_kcell_summer"] * 100
            if not context.get("fixed_expenses") and sheets_ctx.get("fixed_monthly"):
                context["fixed_expenses"] = sheets_ctx["fixed_monthly"] * 100
            if not context.get("savings_potential"):
                context["savings_potential"] = sheets_ctx.get("free_to_save", 0) * 100
            context["goals"] = sheets_ctx.get("goals", [])
            context["total_balance"] = sheets_ctx.get("total_balance", 0)
            context["accounts"] = sheets_ctx.get("accounts", [])
        except Exception:
            pass

    # Agent thinks with full context (with 5s timeout)
    try:
        parsed = await asyncio.wait_for(agent_parse(text, context), timeout=5.0)
    except asyncio.TimeoutError:
        parsed = await asyncio.wait_for(unified_parse(text), timeout=4.0)
    except Exception:
        parsed = await unified_parse(text)

    msg_type = parsed.get("type", "unknown")

    # 0. Question — agent is unsure, asks user
    if msg_type == "question":
        await message.answer(
            f"🤔 <b>{parsed['question']}</b>",
            reply_markup=main_menu(), parse_mode=ParseMode.HTML)
        return

    # 0.5. Answer — agent responds with analysis
    if msg_type == "answer":
        await message.answer(
            f"💡 {parsed['text']}",
            reply_markup=main_menu(), parse_mode=ParseMode.HTML)
        return

    # 0.7. Cancel / edit last transaction
    if msg_type == "cancel_transaction":
        from app.models import Transaction
        async with async_session() as session:
            user = await get_or_create_user(session, message.from_user.id)
            # Try session memory first, fallback to last in DB
            sess = _session.get(message.from_user.id, {})
            last_id = sess.get("last_tx_id")
            if last_id:
                result = await session.execute(select(Transaction).where(Transaction.id == last_id))
            else:
                result = await session.execute(
                    select(Transaction).where(Transaction.user_id == user.id)
                    .order_by(Transaction.created_at.desc()).limit(1)
                )
            last_tx = result.scalar_one_or_none()
            if last_tx:
                desc = last_tx.description or ""
                amt = last_tx.amount
                cat = last_tx.category
                await session.delete(last_tx)
                await session.commit()
                _session.pop(message.from_user.id, None)
                await message.answer(
                    f"🗑 <b>Операция отменена</b>\n\nУдалено: {fmt_money(amt)} — {cat}"
                    f"{chr(10) + desc[:80] if desc else ''}",
                    reply_markup=main_menu(), parse_mode=ParseMode.HTML)
            else:
                await message.answer("🤷 Нет операций для отмены.", reply_markup=main_menu(), parse_mode=ParseMode.HTML)
        return

    # 0.8. Edit last transaction (change amount or category)
    if msg_type == "edit_transaction":
        from app.models import Transaction
        async with async_session() as session:
            user = await get_or_create_user(session, message.from_user.id)
            sess = _session.get(message.from_user.id, {})
            last_id = sess.get("last_tx_id")
            if last_id:
                result = await session.execute(select(Transaction).where(Transaction.id == last_id))
            else:
                result = await session.execute(
                    select(Transaction).where(Transaction.user_id == user.id)
                    .order_by(Transaction.created_at.desc()).limit(1)
                )
            last_tx = result.scalar_one_or_none()
            if last_tx:
                new_amount = parsed.get("amount", 0)
                new_cat = parsed.get("category")
                if new_amount and new_amount > 0:
                    last_tx.amount = int(new_amount) * 100
                if new_cat:
                    last_tx.category = new_cat
                await session.commit()
                await message.answer(
                    f"✏️ <b>Операция обновлена</b>\n\n"
                    f"Сумма: {fmt_money(last_tx.amount)}\n"
                    f"Категория: {last_tx.category}",
                    reply_markup=main_menu(), parse_mode=ParseMode.HTML)
            else:
                await message.answer("🤷 Нет операции для изменения.", reply_markup=main_menu(), parse_mode=ParseMode.HTML)
        return

    # 1. Finance settings
    if msg_type == "finance_settings":
        async with async_session() as session:
            user = await get_or_create_user(session, message.from_user.id)
            fs = await update_finance_settings(
                session, user.id,
                monthly_salary=parsed.get("monthly_salary") * 100 if parsed.get("monthly_salary") else None,
                monthly_expenses=parsed.get("monthly_expenses") * 100 if parsed.get("monthly_expenses") else None)
        msg = "✅ <b>Настройки сохранены!</b>\n\n"
        if fs.monthly_salary:
            msg += f"Зарплата: {fmt_money(fs.monthly_salary)}\n"
        if fs.monthly_expenses:
            msg += f"Расходы: {fmt_money(fs.monthly_expenses)}"
        await message.answer(msg, reply_markup=main_menu(), parse_mode=ParseMode.HTML)
        return

    # 2. Finance transaction
    if msg_type == "finance_transaction":
        async with async_session() as session:
            user = await get_or_create_user(session, message.from_user.id)
            tx = await add_transaction(
                session, user.id, amount=parsed["amount"] * 100,
                type_=TransactionType.income if parsed["type_tx"] == "income" else TransactionType.expense,
                category=parsed["category"], description=parsed.get("description", text[:200]))
        # Save to session for undo/edit
        _session[message.from_user.id] = {"last_tx_id": str(tx.id), "last_amount": parsed["amount"], "last_cat": parsed["category"]}
        # Also write to Google Sheet
        if SHEETS_ENABLED:
            try:
                tx_type_sheet = "Доход" if parsed["type_tx"] == "income" else "Расход"
                today_str = datetime.now().strftime("%d.%m.%Y")
                await asyncio.get_event_loop().run_in_executor(None, lambda: add_operation_to_sheet(
                    date=today_str, tx_type=tx_type_sheet,
                    category=parsed["category"], subcategory="",
                    account="Kaspi карта", counterpart="",
                    description=parsed.get("description", text[:100]),
                    amount=int(parsed["amount"])
                ))
            except Exception:
                pass
        emoji = "➕" if parsed["type_tx"] == "income" else "➖"
        sign = "Доход" if parsed["type_tx"] == "income" else "Расход"
        await message.answer(
            f"{emoji} <b>{sign} записан!</b>\n\nСумма: {parsed['amount']:,.0f}₸\nКатегория: {parsed['category']}\n📊 Записано в таблицу",
            reply_markup=kb([
                [InlineKeyboardButton(text="🗑 Отменить", callback_data=f"tx:cancel:{tx.id}")],
                [InlineKeyboardButton(text="← Меню", callback_data="menu:main")],
            ]), parse_mode=ParseMode.HTML)
        return

    # 3. Wish
    if msg_type == "wish":
        async with async_session() as session:
            user = await get_or_create_user(session, message.from_user.id)
            w = await add_wish(
                session, user.id, title=parsed["title"], target_amount=parsed["target_amount"] * 100,
                priority=parsed.get("priority", "medium"), category=parsed.get("category"),
                deadline=datetime.strptime(parsed["deadline"], "%Y-%m-%d") if parsed.get("deadline") else None)
            timeline = await calculate_wish_timeline(session, user.id, w)
        extra = ""
        if timeline.get("months") is not None:
            extra = f"\n⏱ ~{timeline['months']} мес до цели"
        await message.answer(
            f"🎯 <b>Желание добавлено!</b>\n\n<b>{w.title}</b>\n💰 Цель: {w.target_amount/100:,.0f}₸\n📂 {w.category or 'Другое'}{extra}",
            reply_markup=main_menu(), parse_mode=ParseMode.HTML)
        return

    # 4. Task
    if msg_type not in ("task",) or not parsed.get("is_task"):
        await message.answer(
            "🤔 Не понял что делать с этим.\n\nМожешь написать: задачу, трату, желание, или задать вопрос по бюджету.",
            reply_markup=main_menu(), parse_mode=ParseMode.HTML)
        return

    conf_cat = parsed.get("confidence_category", 0)
    if conf_cat < CONFIDENCE_THRESHOLD:
        cats = ["Работа","Дом","Финансы","Здоровье","Учёба","Покупки","Отношения","Хобби","Другое"]
        cat_buttons = [[InlineKeyboardButton(
            text=f"{cat} {'✅' if cat == parsed.get('category') else ''}",
            callback_data=f"confirm_cat:{cat}:{text[:80]}")]
            for cat in cats]
        await message.answer(
            f"🤔 Кажется, это категория <b>{parsed.get('category', 'Другое')}</b> ({conf_cat}%).\nВерно?",
            reply_markup=kb(cat_buttons), parse_mode=ParseMode.HTML)
        return

    async with async_session() as session:
        user = await get_or_create_user(session, message.from_user.id)
        task = await create_task_from_parsed(session, user.id, parsed)
    card = task_card(task)
    await message.answer(
        f"✅ <b>Задача создана!</b>\n\n{card}",
        reply_markup=kb([
            [InlineKeyboardButton(text="📋 Мои задачи", callback_data="tasks:list:0")],
            [InlineKeyboardButton(text="📊 Главное меню", callback_data="menu:main")],
        ]), parse_mode=ParseMode.HTML)


@dp.message()
async def handle_message(message: types.Message):
    if not is_allowed(message.from_user.id):
        return
    if message.text and not message.text.startswith('/'):
        await handle_free_text(message, message.text)
    elif message.voice or message.audio:
        await message.answer("🎤 Распознаю голос...")
        file = await bot.get_file(message.voice.file_id if message.voice else message.audio.file_id)
        file_url = f"https://api.telegram.org/file/bot{os.getenv('BOT_TOKEN')}/{file.file_path}"
        transcript = await transcribe_voice(file_url)
        if transcript.startswith("["):
            await message.answer(transcript, reply_markup=main_menu())
            return
        await message.answer(f"📝 <b>Распознано:</b>\n<i>{transcript[:400]}</i>", parse_mode=ParseMode.HTML)
        await handle_free_text(message, transcript)

