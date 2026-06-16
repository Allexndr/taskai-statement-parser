"""
Parse bank statements from 3 PDFs and upload to Google Sheets.
Run: python3 parse_statements.py
"""
import re
import json
import pdfplumber
import gspread
from google.oauth2.service_account import Credentials
from collections import defaultdict
from datetime import datetime

SPREADSHEET_ID = "1HuJlWZhhwTwhlDlmcCchQFsS1-auRPWgDOW4udfAKuQ"
SCOPES = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
CREDS_FILE = "gen-lang-client-0781220137-ad8ceca4cd1e.json"

# ── Category rules (order matters) ──────────────────────────
RULES = [
    # Own transfers — not real expenses
    (r'freedom finance|на карту freedom|freedom bank|ksnvkzka|6723|kaspi gold|7995|'
     r'kaspi pay|со своего счета|на свой счет|перевод собственных|kaspi депозит|'
     r'оплата kaspi кредита', 'Перевод между своими счетами', 'Перевод'),
    # Credit payments (Home Credit, other banks)
    (r'home credit|хоум кредит|1299|оплата kaspi кредита|погашение кредит|'
     r'кредит|loan payment|банк.*кредит', 'Кредит', 'Расход'),
    # Salary / ИП income
    (r'зарплата|salary|kcell|кселл', 'Доход Kcell', 'Доход'),
    (r'neurokids|платежи за компьютер|оплата за услуг|вознагражден', 'Доход ИП', 'Доход'),
    # Rent (Ольга М. — регулярные переводы похожи на аренду)
    (r'ольга м\.|ольга м$', 'Аренда', 'Расход'),
    # Furniture / large household
    (r'ангар 39|brand mebel|xan mebel|мебел|мебель|ikea|hoff', 'Маркетплейсы', 'Расход'),
    # Electronics
    (r'alser|алсер|technodom|технодом|sulpak|sulpek|dns|eldorado|mechta|samsung|apple store', 'Маркетплейсы', 'Расход'),
    # Utilities
    (r'коммунал|квартплат|gazprom|свет|газ|тепло|water supply|водоснаб|есеп|egov', 'Коммуналка', 'Расход'),
    # Tax / IP
    (r'налог|комитет гос|tax|казына', 'Налоги ИП', 'Расход'),
    # Subscriptions
    (r'google|netflix|spotify|apple|youtube|steam|discord|chatgpt|openai|'
     r'кинопоиск|amediateka|microsoft|notion|figma|github|vercel', 'Подписки', 'Расход'),
    # Food & drinks
    (r'магнум|magnum|wolt|яндекс.еда|dodo|pizza|суши|sushi|ёбидоёби|yobidoyobi|'
     r'coffee boom|coffee|кофе|бистро|столов|кафе|restaurant|cafe|пицц|burger|'
     r'шаурм|shawarma|айдаулет|байбашаев|мамырханов|korean street|живая вода|'
     r'food|dominos|kfc|mcdonalds|burger king|subway|ип пак|ип алимбаев|'
     r'kence|кенсе|шашлык|манты|плов|лагман|самса|food court', 'Еда', 'Расход'),
    # Health
    (r'аптек|фарм|zamanfarm|yesmed|есмед|глазных|медицин|клиник|стоматол|'
     r'поликли|hospital|health|pharmacy|медцентр|лаборатор|онко|дента', 'Здоровье', 'Расход'),
    # Transport
    (r'uber|indriver|яндекс.такси|такси|онай|автобус|метро|парковк|бензин|'
     r'заправк|avto|astana motors|kolesa|ato|атп', 'Транспорт', 'Расход'),
    # Marketplaces / shopping
    (r'kaspi shop|kaspi magazin|kaspi\.kz|ozon|wildberries|aliexpress|lamoda|'
     r'lamoda|fix price|фикс прайс', 'Маркетплейсы', 'Расход'),
    # Entertainment
    (r'bunker|арт-убежище|кино|kinopark|cinema|theater|билет|concert|ивент|'
     r'karaoke|bowling|батут|квест|quest|парк|сауна|sauna|ресторан|клуб|'
     r'бильярд|боулинг|аттракцион', 'Развлечения', 'Расход'),
    # Commission / service fees
    (r'комиссия|абонентск|обслужива|тариф', 'Прочее', 'Расход'),
    # Person transfers — split into sub-categories later
    (r'перевод.*[А-ЯA-Z][а-яa-z]+\s+[А-ЯA-Z]\.', 'Переводы', 'Расход'),
]

def categorize(description: str, is_income: bool) -> tuple:
    d = description.lower()
    for pattern, cat, tx_type in RULES:
        if re.search(pattern, d):
            return cat, tx_type
    if is_income:
        return 'Прочий доход', 'Доход'
    return 'Прочее', 'Расход'

def parse_num(s: str) -> float:
    s = str(s).replace('\xa0','').replace(' ','').replace(',','.').replace('₸','').replace('KZT','').strip()
    s = re.sub(r'[^\d.\-]', '', s)
    try:
        return float(s)
    except:
        return 0.0

def fmt_date(d: str) -> tuple:
    """Returns (dd.mm.yyyy, yyyy-mm)"""
    d = d.strip()
    # DD.MM.YYYY or DD.MM.YY
    m = re.match(r'(\d{2})\.(\d{2})\.(\d{2,4})', d)
    if m:
        day, mon, yr = m.groups()
        if len(yr) == 2:
            yr = '20' + yr
        return f"{day}.{mon}.{yr}", f"{yr}-{mon}"
    return d, d[:7]


# ═══════════════════════════════════════════════════════════════
# PARSER 1: Freedom Bank (KZ49551A / KZ31722S)  — table format
# ═══════════════════════════════════════════════════════════════
def parse_freedom(text: str) -> list:
    ops = []
    # Freedom format: Date  Description  Amount (debit/credit columns)
    # Line example: "16.07.2025  Пополнение от ИП NEUROKIDS  +400 000,00"
    # Also: "31.07.2025  Покупка  -3 500,00"
    pat = re.compile(
        r'(\d{2}\.\d{2}\.\d{4})'        # date
        r'[^\d\n]{2,80}?'               # skip time/doc number
        r'([\+\-]?\s*[\d\s]+[,\.]\d{2})\s*(?:₸|KZT)?'  # amount
    )
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Match date at start
        dm = re.match(r'^(\d{2}\.\d{2}\.\d{4})', line)
        if dm:
            date_str = dm.group(1)
            # Gather context: this line + next 2 lines
            context = ' '.join(lines[i:i+3])
            # Find amounts
            amounts = re.findall(r'([\+\-]?\s*[\d\s]{1,12}[,\.]\d{2})\s*(?:₸|KZT|$)', context)
            # Find description
            desc_match = re.search(r'\d{2}\.\d{2}\.\d{4}\s+(?:\d+:\d+:\d+\s+)?(.+?)(?=[\+\-]\s*[\d\s]+[,\.])', context)
            if not desc_match:
                # Try debit/credit columns
                desc_match = re.search(r'\d{2}\.\d{2}\.\d{4}\s+(.+)', line)
            desc = desc_match.group(1).strip() if desc_match else line

            for amt_str in amounts:
                amt = parse_num(amt_str)
                if abs(amt) < 1:
                    continue
                is_income = amt > 0 or '+' in amt_str
                full_date, month = fmt_date(date_str)
                cat, tx_type = categorize(desc, is_income)
                ops.append({
                    'date': full_date, 'month': month,
                    'type': tx_type, 'category': cat,
                    'subcategory': '',
                    'account': 'Freedom Super Card (*6723)',
                    'counterpart': '',
                    'description': desc[:120],
                    'amount': abs(amt),
                    'is_income': is_income,
                    'source': 'Freedom'
                })
                break  # one amount per line
        i += 1
    return ops


def parse_kaspi_pay(text: str) -> list:
    """Parse Kaspi Pay / ИП account (KZ31722S)."""
    ops = []
    lines = text.split('\n')
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # Pattern: docnum  date/time  debit  name/iin/account  ...  description
        m = re.match(r'^(\d{5,10})\s+(\d{2}\.\d{2}\.\d{4})', line)
        if m:
            date_str = m.group(2)
            # Next lines might have description
            context = ' '.join(lines[i:i+3])
            # Find debit or credit amount (digits with spaces + ,NN)
            amounts = re.findall(r'(?<!\d)([\d\s]{1,12},\d{2})(?!\d)', context)
            # Description usually after account number
            desc_m = re.search(r'Назначение платежа\s+(.+)', context)
            if not desc_m:
                desc_m = re.search(r'(?:342|190|851|841)\s+(.+)', context)
            desc = desc_m.group(1).strip()[:120] if desc_m else context[30:80]

            # Determine debit vs credit from column position
            # In raw text debit comes before credit; if only one amount assume debit
            amt = parse_num(amounts[0]) if amounts else 0
            if amt < 1:
                i += 1
                continue

            # Heuristic: own transfers are debit (expense)
            is_income = bool(re.search(r'neurokids|зачислен|пополн|приход|поступ', context.lower()))
            full_date, month = fmt_date(date_str)
            cat, tx_type = categorize(desc, is_income)

            ops.append({
                'date': full_date, 'month': month,
                'type': tx_type, 'category': cat,
                'subcategory': '',
                'account': 'Kaspi Pay ИП',
                'counterpart': '',
                'description': desc,
                'amount': amt,
                'is_income': is_income,
                'source': 'KaspiPay'
            })
        i += 1
    return ops


def parse_kaspi_gold(text: str) -> list:
    """Parse Kaspi Gold card statement."""
    ops = []
    # Format: DD.MM.YY [+/-] Amount ₸  OperationType  Details
    # Sometimes split across lines, so join smartly
    pat = re.compile(
        r'(\d{2}\.\d{2}\.\d{2})\s+'
        r'([-+])\s*([\d\s]+[,]\d{2})\s*₸\s+'
        r'(Покупка|Перевод|Пополнение|Поступление со своего счета|'
        r'Снятие|Разное|Зачисление|перевод на свой счет)\s+'
        r'(.*?)(?=\d{2}\.\d{2}\.\d{2}\s+[-+]|\Z)',
        re.DOTALL
    )
    for m in pat.finditer(text):
        date_raw, sign, amt_str, op_type, details = m.groups()
        details = re.sub(r'\s+', ' ', details).strip()
        # Remove page headers
        details = re.sub(r'АО «Kaspi Bank».*?Приложение к Справке.*?2026', '', details).strip()
        details = details[:120]

        amt = parse_num(amt_str)
        if amt < 1:
            continue

        is_income = sign == '+'
        full_date, month = fmt_date(date_raw)
        
        # Determine category
        combined = f"{op_type} {details}"
        cat, tx_type = categorize(combined, is_income)

        ops.append({
            'date': full_date, 'month': month,
            'type': tx_type, 'category': cat,
            'subcategory': '',
            'account': 'Kaspi Gold (*7995)',
            'counterpart': details.split()[0] if details else '',
            'description': f"{op_type}: {details}",
            'amount': amt,
            'is_income': is_income,
            'source': 'KaspiGold'
        })
    return ops


# ── Run parsers ──────────────────────────────────────────────
print("Reading PDFs...")
all_ops = []

# 1. Kaspi Pay ИП account
with pdfplumber.open("Выписка_по_счету_KZ31722S000048244271.pdf") as pdf:
    text1 = "\n".join(p.extract_text() or "" for p in pdf.pages)
ops1 = parse_kaspi_pay(text1)
print(f"  Kaspi Pay ИП: {len(ops1)} операций")
all_ops.extend(ops1)

# 2. Freedom Super Card
with pdfplumber.open("Выписка5276028511881940825.pdf") as pdf:
    text2 = "\n".join(p.extract_text() or "" for p in pdf.pages)
ops2 = parse_freedom(text2)
print(f"  Freedom Super Card: {len(ops2)} операций")
all_ops.extend(ops2)

# 3. Kaspi Gold
with pdfplumber.open("gold_statement (1).pdf") as pdf:
    text3 = "\n".join(p.extract_text() or "" for p in pdf.pages)
ops3 = parse_kaspi_gold(text3)
print(f"  Kaspi Gold: {len(ops3)} операций")
all_ops.extend(ops3)

# Sort by date
def sort_key(op):
    try:
        return datetime.strptime(op['date'], "%d.%m.%Y")
    except:
        return datetime.min

all_ops.sort(key=sort_key, reverse=True)
print(f"\nВсего операций: {len(all_ops)}")

# ── Statistics ───────────────────────────────────────────────
by_month_inc = defaultdict(float)
by_month_exp = defaultdict(float)
by_cat = defaultdict(float)
by_month_cat = defaultdict(lambda: defaultdict(float))

for op in all_ops:
    if op['type'] == 'Перевод':
        continue
    m = op['month']
    if op['type'] == 'Доход':
        by_month_inc[m] += op['amount']
    elif op['type'] == 'Расход':
        by_month_exp[m] += op['amount']
        by_cat[op['category']] += op['amount']
        by_month_cat[m][op['category']] += op['amount']

print("\n=== ПО МЕСЯЦАМ (без переводов) ===")
all_months = sorted(set(list(by_month_inc.keys()) + list(by_month_exp.keys())), reverse=True)
for m in all_months[:12]:
    inc = by_month_inc[m]
    exp = by_month_exp[m]
    print(f"  {m}: доход {inc:>12,.0f}₸  расход {exp:>12,.0f}₸  остаток {inc-exp:>+12,.0f}₸")

print("\n=== ТОП КАТЕГОРИЙ РАСХОДОВ (за весь период) ===")
for cat, amt in sorted(by_cat.items(), key=lambda x: x[1], reverse=True):
    print(f"  {cat:<35} {amt:>12,.0f}₸")

# ── Upload to Google Sheets ──────────────────────────────────
print("\nUploading to Google Sheets...")
creds = Credentials.from_service_account_file(CREDS_FILE, scopes=SCOPES)
gc = gspread.authorize(creds)
sh = gc.open_by_key(SPREADSHEET_ID)

ws_ops = sh.worksheet("Операции")

# Keep header row, clear data
existing = ws_ops.get_all_values()
header = existing[0] if existing else []
# Count existing real rows (non-template)
existing_data = [r for r in existing[1:] if r[0] and r[0] != '16.06.2026']
print(f"  Existing rows in sheet: {len(existing_data)}")

# Build rows to upload
rows_to_add = []
next_id = 100  # start IDs from 100 for imported data

for op in all_ops:
    if op['type'] == 'Перевод':
        affects = 'Нет'
    else:
        affects = 'Да'
    
    row = [
        op['date'],
        op['month'],
        op['type'],
        op['category'],
        op.get('subcategory', ''),
        op['account'],
        op.get('counterpart', ''),
        op['description'][:100],
        'Факт',
        str(int(op['amount'])),
        'Да' if op['type'] == 'Перевод' else 'Нет',
        affects,
        op['source'],
        str(next_id)
    ]
    rows_to_add.append(row)
    next_id += 1

# Clear old data and write fresh
# Find last row with data
if len(existing) > 1:
    last_row = len(existing)
    # Clear from row 2 to last_row
    if last_row > 1:
        ws_ops.batch_clear([f"A2:N{max(last_row, len(rows_to_add)+10)}"])
        print(f"  Cleared {last_row-1} old rows")

# Write in batches of 500
BATCH = 500
for i in range(0, len(rows_to_add), BATCH):
    batch = rows_to_add[i:i+BATCH]
    ws_ops.append_rows(batch, value_input_option='USER_ENTERED')
    print(f"  Uploaded rows {i+1}–{min(i+BATCH, len(rows_to_add))}")

print(f"\n✅ Done! {len(rows_to_add)} операций загружено в Google Sheets")
print("   Открой таблицу: https://docs.google.com/spreadsheets/d/1HuJlWZhhwTwhlDlmcCchQFsS1-auRPWgDOW4udfAKuQ/edit#gid=0")

# Save JSON backup
with open('/tmp/parsed_ops.json', 'w', encoding='utf-8') as f:
    json.dump(all_ops, f, ensure_ascii=False, indent=2)
print(f"\nBackup: /tmp/parsed_ops.json ({len(all_ops)} ops)")
