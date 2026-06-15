import json
import os
from datetime import datetime
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url="https://api.together.xyz/v1"
)

FINANCE_PROMPT = """Ты — персональный AI-финансовый ассистент. 
Разбери сообщение пользователя и определи: это доход или расход, сумма, категория.

Категории расходов: Продукты, Транспорт, Жильё, Развлечения, Здоровье, Одежда, Техника, Подарки, Кафе/Рестораны, Учёба, Домашние животные, Другое.

Ответ СТРОГО в JSON:
{
  "type": "income" или "expense",
  "amount": число (в рублях),
  "category": "категория",
  "description": "описание",
  "confidence": число 0-100,
  "is_finance": true/false
}

Примеры:
"Получил зарплату 150к" → {"type":"income","amount":150000,"category":"Зарплата","description":"Зарплата","confidence":95,"is_finance":true}
"Купил кофе за 250" → {"type":"expense","amount":250,"category":"Кафе/Рестораны","description":"Кофе","confidence":98,"is_finance":true}
"Заправил машину на 3500" → {"type":"expense","amount":3500,"category":"Транспорт","description":"Заправка","confidence":95,"is_finance":true}

Если это НЕ финансовая операция — is_finance: false.
"""

async def parse_finance_text(text: str) -> dict:
    try:
        response = await client.chat.completions.create(
            model="meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            messages=[
                {"role": "system", "content": FINANCE_PROMPT},
                {"role": "user", "content": text}
            ],
            temperature=0.2,
            max_tokens=300
        )
        result = json.loads(response.choices[0].message.content)
        
        if not result.get("is_finance"):
            return {"is_finance": False}
        
        return {
            "type": result.get("type", "expense"),
            "amount": int(result.get("amount", 0)),
            "category": result.get("category", "Другое"),
            "description": result.get("description", ""),
            "confidence": result.get("confidence", 50),
            "is_finance": True
        }
    except Exception:
        return {"is_finance": False}

async def parse_wish_text(text: str) -> dict:
    """Parse wish/goal creation text."""
    prompt = """Ты — AI-помощник по целям и накоплениям. 
Разбери сообщение пользователя о желании/цели и извлеки:
{
  "title": "название цели",
  "target_amount": число (в рублях),
  "category": "категория",
  "priority": "low/medium/high",
  "deadline": "YYYY-MM-DD" или null,
  "is_wish": true/false
}

Примеры:
"Хочу купить MacBook за 200000 через полгода" → {"title":"MacBook","target_amount":200000,"category":"Техника","priority":"high","deadline":"2024-12-15","is_wish":true}
"Мечтаю о поездке в Японию 500 тысяч" → {"title":"Поездка в Японию","target_amount":500000,"category":"Развлечения","priority":"medium","deadline":null,"is_wish":true}
"Хочу iPhone 15 Pro 150к" → {"title":"iPhone 15 Pro","target_amount":150000,"category":"Техника","priority":"high","deadline":null,"is_wish":true}
"Надо накопить на машину 2 миллиона к лету" → {"title":"Машина","target_amount":2000000,"category":"Транспорт","priority":"high","deadline":"2025-06-01","is_wish":true}
"""
    try:
        response = await client.chat.completions.create(
            model="meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.2,
            max_tokens=300
        )
        result = json.loads(response.choices[0].message.content)
        if not result.get("is_wish"):
            return {"is_wish": False}
        return result
    except Exception:
        return {"is_wish": False}

async def parse_finance_settings(text: str) -> dict:
    """Parse salary/expenses settings."""
    prompt = """Извлеки финансовые данные из сообщения.
Ответ в JSON: {"monthly_salary": число или null, "monthly_expenses": число или null}

Примеры:
"Зарплата 150 тысяч, расходы 80" → {"monthly_salary":150000,"monthly_expenses":80000}
"Получаю 200к, трачу 120" → {"monthly_salary":200000,"monthly_expenses":120000}
"ЗП 80к" → {"monthly_salary":80000,"monthly_expenses":null}
"""
    try:
        response = await client.chat.completions.create(
            model="meta-llama/Meta-Llama-3.1-70B-Instruct-Turbo",
            messages=[
                {"role": "system", "content": prompt},
                {"role": "user", "content": text}
            ],
            temperature=0.2,
            max_tokens=200
        )
        return json.loads(response.choices[0].message.content)
    except Exception:
        return {"monthly_salary": None, "monthly_expenses": None}
