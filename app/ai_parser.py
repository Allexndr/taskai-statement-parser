import json
import os
from datetime import datetime, timedelta
from openai import AsyncOpenAI

client = AsyncOpenAI(
    api_key=os.getenv("OPENAI_API_KEY"),
    base_url="https://api.groq.com/openai/v1"
)

PARSE_PROMPT = """Ты — персональный AI-ассистент для управления задачами пользователя. 
Твоя задача — разобрать сообщение пользователя и извлечь структурированные данные задачи.

Ответ всегда давай СТРОГО в формате JSON, без пояснений вне JSON.

Поля JSON:
- title: краткий заголовок задачи (до 100 символов)
- description: развернутое описание, если есть, иначе null
- category: категория задачи из списка [Работа, Дом, Финансы, Здоровье, Учёба, Покупки, Отношения, Хобби, Другое]
- priority: один из [low, medium, high, critical]
- due_at: дедлайн в формате ISO 8601 (YYYY-MM-DDTHH:MM:SS), либо null если не указано
- confidence_category: число 0-100, насколько ты уверен в категории
- confidence_priority: число 0-100, насколько ты уверен в приоритете
- is_task: true если это явно задача/действие, false если просто заметка/мысль

Правила:
- "срочно", "важно", "критично", "надо сегодня" → priority high или critical
- "как-нибудь", "если будет время", "желательно" → low
- Сегодня — {today}, завтра — {tomorrow}
- Понимай относительные даты: "через 2 часа", "завтра в 19:00", "в субботу утром"
- Если не уверен в категории (confidence_category < 70), ставь "Другое" и низкий confidence
- Если is_task == false, остальные поля могут быть null

Пример ответа:
{{
  "title": "Купить продукты",
  "description": "Молоко, хлеб, яйца",
  "category": "Покупки",
  "priority": "medium",
  "due_at": "2024-06-16T18:00:00",
  "confidence_category": 95,
  "confidence_priority": 80,
  "is_task": true
}}
"""

async def parse_task_text(text: str, user_context: dict = None) -> dict:
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    
    messages = [
        {"role": "system", "content": PARSE_PROMPT.format(today=today, tomorrow=tomorrow)},
        {"role": "user", "content": text}
    ]
    
    try:
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            temperature=0.3,
            max_tokens=400
        )
        result = json.loads(response.choices[0].message.content)
        
        # Validate and normalize
        if result.get("is_task") is not True:
            return {"is_task": False, "title": text[:100]}
            
        # Ensure due_at is valid ISO or None
        due = result.get("due_at")
        if due:
            try:
                datetime.fromisoformat(due.replace('Z', '+00:00'))
            except ValueError:
                due = None
        
        return {
            "title": result.get("title", text[:100]),
            "description": result.get("description"),
            "category": result.get("category", "Другое"),
            "priority": result.get("priority", "medium"),
            "due_at": due,
            "confidence_category": result.get("confidence_category", 50),
            "confidence_priority": result.get("confidence_priority", 50),
            "is_task": True
        }
    except Exception as e:
        # Fallback — create simple task
        return {
            "title": text[:100],
            "description": text[:500],
            "category": "Другое",
            "priority": "medium",
            "due_at": None,
            "confidence_category": 30,
            "confidence_priority": 30,
            "is_task": True
        }


UNIFIED_PROMPT = """Ты — умный ассистент Taski AI. Проанализируй сообщение пользователя и определи его ТИП с учётом контекста.

Возможные типы:
- "finance_settings" — пользователь говорит о своей зарплате, доходах, расходах, бюджете (примеры: "зарплата 150к", "трачу 80 тысяч", "В кселл зарплата 370.000", "доход 400к")
- "finance_transaction" — конкретная финансовая операция (примеры: "купил кофе за 250", "заправил машину на 3500", "получил премию 50к")
- "wish" — желание, мечта, цель накопления (примеры: "хочу машину за 2 миллиона", "мечтаю о поездке в Японию на 500к")
- "task" — задача, действие, дело, напоминание (примеры: "купить молоко", "позвонить маме", "проверить почту")
- "unknown" — не подходит ни под одну категорию

Критические правила контекста:
- Слова "зарплата", "доход", "расходы", "трачу", "получаю", "бюджет" ВСЕГДА относятся к финансам (finance_settings или finance_transaction), НИКОГДА к задачам
- "В кселл зарплата 370.000" → type: "finance_settings", monthly_salary: 370000
- "проверить цены в кселл" → type: "task"
- "потратил 500 на кофе" → type: "finance_transaction"

Ответ СТРОГО в JSON:
{
  "type": "finance_settings|finance_transaction|wish|task|unknown",
  "confidence": число 0-100,
  "data": {
    // finance_settings:
    "monthly_salary": число или null,
    "monthly_expenses": число или null,

    // finance_transaction:
    "transaction_type": "income" или "expense",
    "amount": число,
    "category": "категория",
    "description": "описание",

    // wish:
    "title": "название",
    "target_amount": число,
    "category": "категория",
    "priority": "low|medium|high",
    "deadline": "YYYY-MM-DD" или null,

    // task:
    "title": "краткий заголовок",
    "description": "описание или null",
    "category": "Работа|Дом|Финансы|Здоровье|Учёба|Покупки|Отношения|Хобби|Другое",
    "priority": "low|medium|high|critical",
    "due_at": "YYYY-MM-DDTHH:MM:SS" или null,
    "confidence_category": число,
    "confidence_priority": число
  }
}
"""


async def unified_parse(text: str) -> dict:
    """Single-call smart parser that understands context."""
    today = datetime.now().strftime("%Y-%m-%d")
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")

    messages = [
        {"role": "system", "content": UNIFIED_PROMPT.replace("{today}", today).replace("{tomorrow}", tomorrow)},
        {"role": "user", "content": text}
    ]

    try:
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            temperature=0.2,
            max_tokens=500
        )
        result = json.loads(response.choices[0].message.content)

        msg_type = result.get("type", "unknown")
        data = result.get("data", {})

        # Normalize finance_settings
        if msg_type == "finance_settings":
            return {
                "type": "finance_settings",
                "monthly_salary": data.get("monthly_salary"),
                "monthly_expenses": data.get("monthly_expenses"),
            }

        # Normalize finance_transaction
        if msg_type == "finance_transaction":
            return {
                "type": "finance_transaction",
                "is_finance": True,
                "type_tx": data.get("transaction_type", "expense"),
                "amount": data.get("amount", 0),
                "category": data.get("category", "Другое"),
                "description": data.get("description", text[:200]),
                "confidence": result.get("confidence", 80),
            }

        # Normalize wish
        if msg_type == "wish":
            return {
                "type": "wish",
                "is_wish": True,
                "title": data.get("title", text[:100]),
                "target_amount": data.get("target_amount", 0),
                "category": data.get("category"),
                "priority": data.get("priority", "medium"),
                "deadline": data.get("deadline"),
                "confidence": result.get("confidence", 80),
            }

        # Normalize task
        if msg_type == "task":
            due = data.get("due_at")
            if due:
                try:
                    datetime.fromisoformat(due.replace('Z', '+00:00'))
                except ValueError:
                    due = None
            return {
                "type": "task",
                "is_task": True,
                "title": data.get("title", text[:100]),
                "description": data.get("description"),
                "category": data.get("category", "Другое"),
                "priority": data.get("priority", "medium"),
                "due_at": due,
                "confidence_category": data.get("confidence_category", 50),
                "confidence_priority": data.get("confidence_priority", 50),
                "confidence": result.get("confidence", 80),
            }

        return {"type": "unknown"}

    except Exception as e:
        # If unified parse fails, fallback to task
        return {
            "type": "task",
            "is_task": True,
            "title": text[:100],
            "description": text[:500],
            "category": "Другое",
            "priority": "medium",
            "due_at": None,
            "confidence_category": 30,
            "confidence_priority": 30,
            "confidence": 30,
        }
