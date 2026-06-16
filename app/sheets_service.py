import os
import json
from datetime import datetime
from typing import Optional

import gspread
from google.oauth2.service_account import Credentials

SPREADSHEET_ID = "1HuJlWZhhwTwhlDlmcCchQFsS1-auRPWgDOW4udfAKuQ"

SCOPES = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive"
]

SHEET_STRUCTURE = {
    "Операции": {
        "cols": ["Дата", "Месяц", "Тип", "Категория", "Подкатегория",
                 "Счет", "Контрагент", "Описание", "План/Факт",
                 "Сумма", "Перевод?", "Влияет на бюджет", "Комментарий", "ID"],
        "types": ["Доход", "Расход", "Перевод", "Накопление"],
    },
    "Настройки": {
        "keys": {
            "Целевой отпускный фонд": "vacation_goal",
            "Стартовая подушка": "savings_start",
            "Ежемесячный кредит": "monthly_credit",
            "Ежемесячная коммуналка": "monthly_utilities",
            "Ежемесячные налоги ИП": "monthly_tax",
            "Ежемесячные подписки": "monthly_subscriptions",
            "Доход Kcell июль-сентябрь": "income_kcell_summer",
            "Доход Kcell далее": "income_kcell_later",
            "Доход ИП базовый": "income_ip",
        }
    }
}

CATEGORY_MAP = {
    "Продукты": "Еда", "Магазин": "Еда", "Доставка": "Еда",
    "Кафе": "Еда", "Ресторан": "Еда", "Wolt": "Еда", "Magnum": "Еда",
    "Такси": "Транспорт", "Uber": "Транспорт", "inDrive": "Транспорт", "Онай": "Транспорт",
    "Аптека": "Здоровье", "Врач": "Здоровье",
    "Ozon": "Маркетплейсы", "Wildberries": "Маркетплейсы", "Kaspi": "Маркетплейсы",
    "Steam": "Развлечения", "Кино": "Развлечения", "Netflix": "Развлечения",
    "YouTube": "Подписки", "Spotify": "Подписки", "Telegram": "Подписки",
    "Kcell": "Доход Kcell", "Зарплата": "Доход Kcell",
    "ИП": "Доход ИП", "Проект": "Доход ИП",
}


def _get_client() -> gspread.Client:
    creds_json = os.getenv("GOOGLE_CREDENTIALS_JSON")
    if creds_json:
        info = json.loads(creds_json)
        creds = Credentials.from_service_account_info(info, scopes=SCOPES)
    else:
        creds = Credentials.from_service_account_file(
            "gen-lang-client-0781220137-ad8ceca4cd1e.json", scopes=SCOPES
        )
    return gspread.authorize(creds)


def _parse_number(s: str) -> int:
    """Parse '1,500,000' or '370 000' → int (in tenge). Returns 0 for non-numeric/date values."""
    try:
        cleaned = str(s).replace(",", "").replace(" ", "").strip()
        # Skip date-like values (e.g. '27.02.2995')
        if "." in cleaned and len(cleaned.split(".")) == 3:
            return 0
        cleaned = cleaned.replace(".", "")
        return int(float(cleaned))
    except Exception:
        return 0


def get_sheet_settings() -> dict:
    """Read Настройки sheet and return parsed dict."""
    gc = _get_client()
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.worksheet("Настройки")
    rows = ws.get_all_values()
    result = {}
    key_map = SHEET_STRUCTURE["Настройки"]["keys"]
    for row in rows[1:]:
        if row and row[0] in key_map:
            result[key_map[row[0]]] = _parse_number(row[1]) if row[1] else 0
    return result


def get_sheet_context() -> dict:
    """Load full financial context from Google Sheet for the AI agent."""
    gc = _get_client()
    sh = gc.open_by_key(SPREADSHEET_ID)

    # Settings
    settings = get_sheet_settings()

    # Goals from Цели
    goals_ws = sh.worksheet("Цели")
    goals = []
    for row in goals_ws.get_all_values()[1:]:
        if row[0]:
            goals.append({
                "title": row[0],
                "target": _parse_number(row[1]),
                "current": _parse_number(row[2]),
                "needed": _parse_number(row[3]),
                "deadline": row[4],
                "monthly_needed": _parse_number(row[5]),
                "progress": row[6],
            })

    # Accounts from Счета
    accounts_ws = sh.worksheet("Счета")
    accounts = []
    total_balance = 0
    for row in accounts_ws.get_all_values()[1:]:
        if row[0]:
            bal = _parse_number(row[5])
            total_balance += bal
            accounts.append({
                "name": row[0],
                "type": row[1],
                "bank": row[2],
                "balance": bal,
            })

    # Current month operations from Операции
    ops_ws = sh.worksheet("Операции")
    ops = ops_ws.get_all_values()
    current_month = datetime.now().strftime("%Y-%m")
    month_income = 0
    month_expense = 0
    month_by_cat: dict = {}
    ops_list = []
    for row in ops[1:]:
        if not row[0] or not row[2]:
            continue
        if row[1] == current_month:
            amount = _parse_number(row[9])
            tx_type = row[2]
            category = row[3]
            if tx_type == "Доход":
                month_income += amount
            elif tx_type == "Расход":
                month_expense += amount
                month_by_cat[category] = month_by_cat.get(category, 0) + amount
            ops_list.append({
                "date": row[0], "type": tx_type, "category": category,
                "description": row[7], "amount": amount, "account": row[5],
            })

    # Categories
    cats_ws = sh.worksheet("Категории")
    categories = []
    for row in cats_ws.get_all_values()[1:]:
        if row[0]:
            categories.append({
                "name": row[0], "type": row[1],
                "sub": row[2], "required": row[3] == "Да"
            })

    fixed_monthly = (
        settings.get("monthly_credit", 0) +
        settings.get("monthly_utilities", 0) +
        settings.get("monthly_tax", 0) +
        settings.get("monthly_subscriptions", 0)
    )

    return {
        "settings": settings,
        "goals": goals,
        "accounts": accounts,
        "total_balance": total_balance,
        "month_income": month_income,
        "month_expense": month_expense,
        "month_by_cat": month_by_cat,
        "current_ops": ops_list[-20:],
        "categories": categories,
        "fixed_monthly": fixed_monthly,
        "free_to_save": (
            settings.get("income_kcell_summer", 0) +
            settings.get("income_ip", 0) - fixed_monthly
        ),
    }


def add_operation_to_sheet(
    date: str, tx_type: str, category: str, subcategory: str,
    account: str, counterpart: str, description: str,
    amount: int, is_transfer: bool = False, comment: str = ""
) -> bool:
    """Add one operation row to Операции sheet."""
    try:
        gc = _get_client()
        sh = gc.open_by_key(SPREADSHEET_ID)
        ws = sh.worksheet("Операции")

        all_rows = ws.get_all_values()
        next_id = len([r for r in all_rows[1:] if r[0]]) + 1
        month = datetime.strptime(date, "%d.%m.%Y").strftime("%Y-%m") if "." in date else date[:7]
        affects_budget = "Нет" if is_transfer else "Да"

        row = [
            date, month, tx_type, category, subcategory,
            account, counterpart, description, "Факт",
            str(amount), "Да" if is_transfer else "Нет",
            affects_budget, comment, str(next_id)
        ]
        ws.append_row(row, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        return False


def guess_category(text: str) -> str:
    """Guess sheet category from free text."""
    text_lower = text.lower()
    for keyword, cat in CATEGORY_MAP.items():
        if keyword.lower() in text_lower:
            return cat
    return "Прочее"
