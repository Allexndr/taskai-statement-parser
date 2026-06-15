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
