from datetime import datetime, timedelta
from uuid import UUID
from sqlalchemy import select, and_, or_, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import User, Transaction, TransactionType, FinanceSettings, Wish, TaskPriority

FINANCE_CATEGORIES = [
    "Продукты", "Транспорт", "Жильё", "Развлечения", "Здоровье",
    "Одежда", "Техника", "Подарки", "Кафе/Рестораны", "Учёба",
    "Домашние животные", "Другое"
]

async def add_transaction(session: AsyncSession, user_id: UUID, amount: int, 
                          type_: TransactionType, category: str, description: str = None) -> Transaction:
    tx = Transaction(
        user_id=user_id,
        amount=amount,
        type=type_,
        category=category,
        description=description
    )
    session.add(tx)
    await session.commit()
    await session.refresh(tx)
    return tx

async def get_monthly_stats(session: AsyncSession, user_id: UUID) -> dict:
    now = datetime.utcnow()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    
    # Income
    income_result = await session.execute(
        select(func.sum(Transaction.amount)).where(
            and_(
                Transaction.user_id == user_id,
                Transaction.type == TransactionType.income,
                Transaction.created_at >= month_start
            )
        )
    )
    income = income_result.scalar() or 0
    
    # Expenses
    expense_result = await session.execute(
        select(func.sum(Transaction.amount)).where(
            and_(
                Transaction.user_id == user_id,
                Transaction.type == TransactionType.expense,
                Transaction.created_at >= month_start
            )
        )
    )
    expenses = expense_result.scalar() or 0
    
    # By category
    cat_result = await session.execute(
        select(Transaction.category, func.sum(Transaction.amount)).where(
            and_(
                Transaction.user_id == user_id,
                Transaction.type == TransactionType.expense,
                Transaction.created_at >= month_start
            )
        ).group_by(Transaction.category).order_by(func.sum(Transaction.amount).desc())
    )
    by_category = {cat: val for cat, val in cat_result.all()}
    
    return {
        "income": income,
        "expenses": expenses,
        "balance": income - expenses,
        "by_category": by_category
    }

async def get_or_create_finance_settings(session: AsyncSession, user_id: UUID) -> FinanceSettings:
    result = await session.execute(select(FinanceSettings).where(FinanceSettings.user_id == user_id))
    settings = result.scalar_one_or_none()
    if not settings:
        settings = FinanceSettings(user_id=user_id)
        session.add(settings)
        await session.commit()
        await session.refresh(settings)
    return settings

async def update_finance_settings(session: AsyncSession, user_id: UUID, 
                                   monthly_salary: int = None, monthly_expenses: int = None) -> FinanceSettings:
    settings = await get_or_create_finance_settings(session, user_id)
    if monthly_salary is not None:
        settings.monthly_salary = monthly_salary
    if monthly_expenses is not None:
        settings.monthly_expenses = monthly_expenses
    await session.commit()
    await session.refresh(settings)
    return settings

async def calculate_savings_rate(session: AsyncSession, user_id: UUID) -> dict:
    settings = await get_or_create_finance_settings(session, user_id)
    stats = await get_monthly_stats(session, user_id)
    
    salary = settings.monthly_salary or 0
    fixed_expenses = settings.monthly_expenses or 0
    actual_expenses = stats["expenses"]
    
    if salary > 0:
        savings_potential = salary - max(fixed_expenses, actual_expenses)
        savings_rate = (savings_potential / salary) * 100 if salary > 0 else 0
    else:
        savings_potential = stats["income"] - actual_expenses
        savings_rate = 0
    
    return {
        "salary": salary,
        "fixed_expenses": fixed_expenses,
        "actual_expenses": actual_expenses,
        "savings_potential": savings_potential,
        "savings_rate": round(savings_rate, 1),
        "monthly_income": stats["income"]
    }

# --- Wishes / Goals ---

async def add_wish(session: AsyncSession, user_id: UUID, title: str, target_amount: int,
                   priority: str = "medium", category: str = None, deadline: datetime = None) -> Wish:
    wish = Wish(
        user_id=user_id,
        title=title,
        target_amount=target_amount,
        priority=priority,
        category=category,
        deadline=deadline
    )
    session.add(wish)
    await session.commit()
    await session.refresh(wish)
    return wish

async def add_to_wish(session: AsyncSession, wish_id: str, amount: int) -> Wish:
    result = await session.execute(select(Wish).where(Wish.id == wish_id))
    wish = result.scalar_one_or_none()
    if wish:
        wish.current_amount += amount
        if wish.current_amount >= wish.target_amount:
            wish.current_amount = wish.target_amount
            wish.status = "achieved"
            wish.achieved_at = datetime.utcnow()
        await session.commit()
        await session.refresh(wish)
    return wish

async def get_active_wishes(session: AsyncSession, user_id: UUID) -> list[Wish]:
    result = await session.execute(
        select(Wish).where(
            and_(Wish.user_id == user_id, Wish.status == "active")
        ).order_by(Wish.priority.desc(), Wish.created_at.desc())
    )
    return result.scalars().all()

async def calculate_wish_timeline(session: AsyncSession, user_id: UUID, wish: Wish) -> dict:
    """Calculate how long to reach the wish goal based on current savings rate."""
    savings = await calculate_savings_rate(session, user_id)
    monthly_savings = savings["savings_potential"]
    remaining = wish.target_amount - wish.current_amount
    
    if remaining <= 0:
        return {"achieved": True, "months": 0, "monthly_savings": monthly_savings}
    
    if monthly_savings > 0:
        months = remaining / monthly_savings
        years = int(months // 12)
        rem_months = int(months % 12)
        return {
            "achieved": False,
            "remaining": remaining,
            "months": round(months, 1),
            "years": years,
            "rem_months": rem_months,
            "monthly_savings": monthly_savings,
            "completion_date": (datetime.utcnow() + timedelta(days=int(months*30))).strftime("%B %Y")
        }
    else:
        return {
            "achieved": False,
            "remaining": remaining,
            "months": None,
            "monthly_savings": 0,
            "message": "Накопление невозможно при текущем доходе/расходе"
        }
