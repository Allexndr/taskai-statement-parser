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


AGENT_PROMPT = """Ты — личный финансовый агент и ассистент Taski. Ты работаешь в Казахстане, валюта — тенге (₸, KZT).
Ты умеешь вести задачи, записывать финансы, анализировать бюджет и давать умные советы.
Ты — как финансовый директор в кармане: задаёшь правильные вопросы, считаешь, предлагаешь распределение.

Данные пользователя прямо сейчас:
{context}

Сегодня: {today}

Возможные действия:
- "create_transaction" — записать доход или расход (деньги уже потрачены/получены)
- "cancel_transaction" — отменить/удалить последнюю операцию
- "edit_transaction" — исправить последнюю операцию: изменить сумму или категорию
- "create_task" — создать задачу, дело, напоминание
- "update_settings" — сохранить зарплату, фиксированные расходы пользователя
- "create_wish" — добавить желание/цель накопления (только будущая цель, не текущая трата)
- "ask_question" — переспросить, если сообщение слишком короткое или неоднозначное
- "answer" — ответить на вопрос, дать совет, посчитать бюджет

Как рассуждать (СТРОГО по порядку):
1. Если есть "исправь сумму", "измени категорию" — action: edit_transaction. В data.amount новая сумма (0 если не меняется), в data.category новая категория (null если не меняется).
1b. Если есть "отмени", "удали", "убери" + описание операции — action: cancel_transaction.
2. Если есть СУММА + глагол прошедшего времени (потратил, заплатил, купил, отправил, скинул, оплатил, снял, дал, взял) — это РАСХОД или ДОХОД, action: create_transaction. НЕ create_wish.
3. Если сообщение короткое (1-2 символа, обрывок) — action: ask_question.
4. Если пользователь спрашивает про бюджет, советы, анализ — action: answer.
5. Если пользователь называет свою зарплату или месячные расходы — action: update_settings.
6. Если пользователь говорит "хочу купить X", "мечтаю о X", "коплю на X" (БУДУЩЕЕ) — action: create_wish.
7. Если пишет задачу/напоминание/дело — action: create_task.
8. Если неоднозначно — action: ask_question.

ВАЖНО: "отправил 2000 на подарок" = create_transaction (expense), НЕ create_wish.
"хочу подарить Свете" без суммы = ask_question (уточни сумму).

Категории расходов (используй точные названия):
- Еда — продукты, кафе, доставка, рестораны
- Транспорт — такси, проездной, бензин
- Здоровье — аптека, врач, анализы
- Подписки — стриминги, сервисы, Google, YouTube, Steam
- Маркетплейсы — Kaspi, Ozon, Wildberries
- Развлечения — кино, игры, клуб, бар
- Подарки — подарок кому-то, поздравление, праздник
- Аренда — аренда жилья, съём
- Кредит — погашение кредита, займа
- Коммуналка — свет, газ, вода, интернет
- Налоги ИП — налог, ИП
- Прочее — всё остальное

Правила по суммам (Казахстан):
- "к", "тыс", "тысяч" = умножить на 1000 (50к = 50000, 2к = 2000)
- "млн", "миллион" = умножить на 1000000 (1.5 млн = 1500000)
- "." в числах часто разделитель тысяч: 370.000 = 370000
- Если число уже написано цифрами БЕЗ суффикса — бери как есть: "2000" = 2000, "500" = 500
- НИКОГДА не умножай голое число сам — только если есть суффикс к/тыс/млн
- Все суммы в тенге (KZT), знак ₸. В поле amount пиши итоговую сумму в тенге.

Финансовые советы (для action: answer):
- Если спрашивают "сколько откладывать" — посчитай: (зарплата + ИП) - обязательные расходы - расходы на жизнь = свободные деньги, из них рекомендуй откладывать 20-30%.
- Если спрашивают про отпуск/цель — посчитай сколько месяцев нужно при текущем балансе.
- Если нет данных в контексте — спроси нужные цифры.
- Пиши конкретные суммы в тенге, не проценты без цифр.
- Будь кратким и по делу, как финансовый директор.

Ответ СТРОГО в JSON (без markdown, только JSON):
{{
  "action": "create_task|create_transaction|cancel_transaction|update_settings|create_wish|ask_question|answer",
  "confidence": число 0-100,
  "reasoning": "краткое рассуждение на русском",
  "data": {{
    "title": "...",
    "description": null,
    "category": "...",
    "priority": "low|medium|high|critical",
    "due_at": "YYYY-MM-DDTHH:MM:SS или null",
    "type": "income|expense",
    "amount": число,
    "monthly_salary": число или null,
    "monthly_expenses": число или null,
    "target_amount": число,
    "deadline": "YYYY-MM-DD или null",
    "question": "текст вопроса пользователю",
    "text": "текст ответа/совета"
  }}
}}
"""


async def agent_parse(text: str, context: dict) -> dict:
    """Thinking agent with full user context."""
    today = datetime.now().strftime("%Y-%m-%d")

    # Build rich context string in KZT
    ctx_lines = []
    def kzt(val): return f"{val/100:,.0f}₸".replace(",", " ") if val else "0₸"

    if context.get("salary"):
        ctx_lines.append(f"- Зарплата (сохранена): {kzt(context['salary'])}/мес")
    if context.get("fixed_expenses"):
        ctx_lines.append(f"- Фиксированные расходы (кредит+комм+налоги): {kzt(context['fixed_expenses'])}/мес")
    if context.get("monthly_income") is not None:
        ctx_lines.append(f"- Доход за текущий месяц (реальный): {kzt(context['monthly_income'])}")
    if context.get("monthly_expenses") is not None:
        ctx_lines.append(f"- Траты за текущий месяц: {kzt(context['monthly_expenses'])}")
    if context.get("savings_potential") is not None:
        sp = context["savings_potential"]
        ctx_lines.append(f"- Доступно для накоплений: {kzt(sp)}/мес ({'профицит' if sp > 0 else 'дефицит'})")
    if context.get("top_expenses"):
        ctx_lines.append(f"- Топ категорий трат: {context['top_expenses']}")
    if context.get("active_tasks_count") is not None:
        ctx_lines.append(f"- Активных задач: {context['active_tasks_count']}")
    if context.get("today_tasks"):
        ctx_lines.append(f"- Задачи на сегодня: {', '.join(context['today_tasks'])}")
    if context.get("active_wishes"):
        wishes_str = "; ".join([
            f"{w['title']} (цель: {kzt(w['target'])}, накоплено: {kzt(w['current'])})"
            for w in context["active_wishes"]
        ])
        ctx_lines.append(f"- Желания и цели (бот): {wishes_str}")

    # Last transactions from DB (most important for "на что последний расход" type questions)
    last_txs = context.get("last_transactions", [])
    if last_txs:
        tx_lines = []
        for t in last_txs[:7]:
            sign = "+" if t["type"] == "income" else "-"
            amt = t["amount"] / 100
            desc = f" ({t['description']})" if t.get("description") else ""
            tx_lines.append(f"{t['date']} {sign}{amt:,.0f}₸ [{t['category']}]{desc}".replace(",", " "))
        ctx_lines.append("- Последние операции (бот):\n  " + "\n  ".join(tx_lines))

    # Enrich with Google Sheets data
    sheets = context.get("sheets", {})
    if sheets:
        settings = sheets.get("settings", {})
        if sheets.get("total_balance"):
            ctx_lines.append(f"- Общий баланс по счетам: {sheets['total_balance']:,}₸".replace(",", " "))
        if sheets.get("fixed_monthly"):
            ctx_lines.append(f"- Фиксированные обязательные: {sheets['fixed_monthly']:,}₸/мес (кредит 100к + комм 30к + налоги 16к + подписки 10к)".replace(",", " "))
        if sheets.get("month_income") is not None:
            ctx_lines.append(f"- Доход этого месяца (таблица): {sheets['month_income']:,}₸".replace(",", " "))
        if sheets.get("month_expense") is not None:
            ctx_lines.append(f"- Расход этого месяца (таблица): {sheets['month_expense']:,}₸".replace(",", " "))
        if sheets.get("month_by_cat"):
            top = sorted(sheets["month_by_cat"].items(), key=lambda x: x[1], reverse=True)[:3]
            ctx_lines.append(f"- Топ трат месяца: {', '.join(f'{k}: {v:,}₸' for k,v in top)}".replace(",", " "))
        goals = sheets.get("goals", [])
        if goals:
            g_str = "; ".join([
                f"{g['title']}: цель {g['target']:,}₸, накоплено {g['current']:,}₸, нужно {g['monthly_needed']:,}₸/мес, дедлайн {g['deadline']}"
                for g in goals if g['title']
            ]).replace(",", " ")
            ctx_lines.append(f"- Финансовые цели: {g_str}")
        accs = [a for a in sheets.get("accounts", []) if a["balance"] > 0]
        if accs:
            accs_str = "; ".join(f"{a['name']} ({a['bank']}): {a['balance']:,}₸".replace(",", " ") for a in accs)
            ctx_lines.append(f"- Счета: {accs_str}")
        # Expected income
        income_kcell = settings.get("income_kcell_summer", 0)
        income_ip = settings.get("income_ip", 0)
        if income_kcell:
            ctx_lines.append(f"- Ожидаемый доход Kcell (июль-сент): {income_kcell:,}₸/мес".replace(",", " "))
        if income_ip:
            ctx_lines.append(f"- Ожидаемый доход ИП: {income_ip:,}₸/мес".replace(",", " "))
        # Recent ops from sheets (last 5 real expenses/income)
        recent = [o for o in sheets.get("current_ops", []) if o.get("type") in ("Расход", "Доход")][:5]
        if recent:
            r_lines = [
                f"{o['date']} {'-' if o['type']=='Расход' else '+'}{o['amount']:,}₸ [{o['category']}] {o['description'][:40]}".replace(",", " ")
                for o in recent
            ]
            ctx_lines.append("- Последние операции (таблица):\n  " + "\n  ".join(r_lines))

    context_str = "\n".join(ctx_lines) if ctx_lines else "(данных пока нет — пользователь новый, скоро заполнит)"

    messages = [
        {"role": "system", "content": AGENT_PROMPT.format(context=context_str, today=today)},
        {"role": "user", "content": text}
    ]

    try:
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=messages,
            temperature=0.15,
            max_tokens=700
        )

        raw = response.choices[0].message.content.strip()
        # Strip markdown code blocks if model wraps in ```json
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        result = json.loads(raw)

        action = result.get("action", "unknown")
        # Support both flat data and nested data
        data = result.get("data", result)

        # Normalize to unified format
        if action == "update_settings":
            sal = data.get("monthly_salary")
            exp = data.get("monthly_expenses")
            return {
                "type": "finance_settings",
                "monthly_salary": sal,
                "monthly_expenses": exp,
                "reasoning": result.get("reasoning", ""),
            }

        if action == "cancel_transaction":
            return {
                "type": "cancel_transaction",
                "description": data.get("description") or text[:200],
                "reasoning": result.get("reasoning", ""),
            }

        if action == "edit_transaction":
            return {
                "type": "edit_transaction",
                "amount": data.get("amount", 0),
                "category": data.get("category"),
                "reasoning": result.get("reasoning", ""),
            }

        if action == "create_transaction":
            return {
                "type": "finance_transaction",
                "is_finance": True,
                "type_tx": data.get("type", "expense"),
                "amount": data.get("amount", 0),
                "category": data.get("category", "Другое"),
                "description": data.get("description") or text[:200],
                "confidence": result.get("confidence", 80),
                "reasoning": result.get("reasoning", ""),
            }

        if action == "create_wish":
            return {
                "type": "wish",
                "is_wish": True,
                "title": data.get("title", text[:100]),
                "target_amount": data.get("target_amount", 0),
                "category": data.get("category"),
                "priority": data.get("priority", "medium"),
                "deadline": data.get("deadline"),
                "confidence": result.get("confidence", 80),
                "reasoning": result.get("reasoning", ""),
            }

        if action == "create_task":
            due = data.get("due_at")
            if due and isinstance(due, str):
                try:
                    datetime.fromisoformat(due.replace('Z', '+00:00'))
                except (ValueError, AttributeError):
                    due = None
            return {
                "type": "task",
                "is_task": True,
                "title": data.get("title", text[:100]),
                "description": data.get("description"),
                "category": data.get("category", "Другое"),
                "priority": data.get("priority", "medium"),
                "due_at": due,
                "confidence_category": data.get("confidence_category", 60),
                "confidence_priority": data.get("confidence_priority", 60),
                "confidence": result.get("confidence", 80),
                "reasoning": result.get("reasoning", ""),
            }

        if action == "ask_question":
            return {
                "type": "question",
                "question": data.get("question", "Можешь уточнить, что ты имеешь в виду?"),
                "reasoning": result.get("reasoning", ""),
            }

        if action == "answer":
            return {
                "type": "answer",
                "text": data.get("text", ""),
                "reasoning": result.get("reasoning", ""),
            }

        return {"type": "unknown"}

    except Exception as e:
        return {"type": "unknown"}
