import asyncio
import logging
from datetime import datetime, timedelta, date
from calendar import monthcalendar
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, InlineKeyboardMarkup, InlineKeyboardButton, ReplyKeyboardMarkup, KeyboardButton
from aiogram.enums import ParseMode
from aiogram.types import FSInputFile
import aiosqlite
import os

# Импортируем настройки из config.py
import config

# --- НАСТРОЙКИ ИЗ CONFIG ---
TOKEN = config.TOKEN
ADMIN_ID = config.ADMIN_ID
DB_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookings.db")
services = config.services
SALON_ADDRESS = config.SALON_ADDRESS
MASTER_PHONE = config.MASTER_PHONE
MASTER_INSTAGRAM = config.MASTER_INSTAGRAM
MASTER_USERNAME = config.MASTER_USERNAME
WORKING_DAYS = config.WORKING_DAYS
WORK_HOURS_START = config.WORK_HOURS_START
WORK_HOURS_END = config.WORK_HOURS_END
PORTFOLIO_PHOTO_URL = config.PORTFOLIO_PHOTO_URL
PORTFOLIO_CAPTION = config.PORTFOLIO_CAPTION

# --- Дни недели для отображения ---
DAYS_RU = {
    0: "понедельник",
    1: "вторник", 
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье"
}

DAYS_RU_SHORT = {
    0: "ПН",
    1: "ВТ",
    2: "СР",
    3: "ЧТ",
    4: "ПТ",
    5: "СБ",
    6: "ВС"
}

# --- ФУНКЦИИ ДЛЯ ГИБКОГО ГРАФИКА ---
def get_work_hours_for_day(weekday: int) -> list:
    """
    Возвращает список интервалов работы для указанного дня недели.
    Формат: [(start_hour, end_hour), ...]
    """
    # Если есть кастомное расписание
    if hasattr(config, 'CUSTOM_WORK_HOURS') and config.CUSTOM_WORK_HOURS:
        if weekday in config.CUSTOM_WORK_HOURS:
            return config.CUSTOM_WORK_HOURS[weekday]
        else:
            return []  # день не указан - выходной
    else:
        # Используем старую систему
        if weekday in WORKING_DAYS:
            return [(WORK_HOURS_START, WORK_HOURS_END)]
        else:
            return []

def get_all_work_hours_for_day(weekday: int) -> list:
    """
    Возвращает список всех часов работы для указанного дня (плоский список)
    """
    intervals = get_work_hours_for_day(weekday)
    hours = []
    for start, end in intervals:
        for h in range(start, end + 1):
            hours.append(f"{h}:00")
    return hours

def is_work_day(weekday: int) -> bool:
    """Проверяет, является ли день рабочим"""
    return len(get_work_hours_for_day(weekday)) > 0

# --- ИНИЦИАЛИЗАЦИЯ ---
bot = Bot(token=TOKEN)
dp = Dispatcher()
user_data = {}

# --- РАБОТА С БАЗОЙ ДАННЫХ ---
async def init_db():
    async with aiosqlite.connect(DB_NAME) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                user_name TEXT,
                username TEXT,
                service_key TEXT NOT NULL,
                service_name TEXT NOT NULL,
                booking_date TEXT NOT NULL,
                booking_time TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                status TEXT DEFAULT 'active',
                reminder_24h_sent INTEGER DEFAULT 0,
                reminder_2h_sent INTEGER DEFAULT 0
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS occupied_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_date TEXT NOT NULL,
                slot_time TEXT NOT NULL,
                booking_id INTEGER,
                UNIQUE(slot_date, slot_time)
            )
        ''')
        
        await db.execute('''
            CREATE TABLE IF NOT EXISTS blocked_slots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                slot_date TEXT NOT NULL,
                slot_time TEXT,
                reason TEXT,
                is_full_day INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        await db.commit()
        print("✅ База данных готова!")

async def add_booking(user_id: int, user_name: str, username: str, service_key: str, 
                      service_name: str, booking_date: str, booking_time: str) -> int:
    async with aiosqlite.connect(DB_NAME) as db:
        cursor = await db.execute(
            '''INSERT INTO bookings (user_id, user_name, username, service_key, 
               service_name, booking_date, booking_time)
               VALUES (?, ?, ?, ?, ?, ?, ?)''',
            (user_id, user_name, username, service_key, service_name, booking_date, booking_time)
        )
        booking_id = cursor.lastrowid
        
        await db.execute(
            '''INSERT INTO occupied_slots (slot_date, slot_time, booking_id)
               VALUES (?, ?, ?)''',
            (booking_date, booking_time, booking_id)
        )
        
        await db.commit()
        return booking_id

async def get_occupied_slots(date_str: str) -> list:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            'SELECT slot_time FROM occupied_slots WHERE slot_date = ?',
            (date_str,)
        ) as cursor:
            slots = await cursor.fetchall()
            return [slot[0] for slot in slots]

async def get_user_bookings(user_id: int) -> list:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            '''SELECT id, service_name, booking_date, booking_time 
               FROM bookings 
               WHERE user_id = ? AND status = 'active' 
               ORDER BY booking_date, booking_time''',
            (user_id,)
        ) as cursor:
            return await cursor.fetchall()

async def cancel_booking(booking_id: int, user_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            'SELECT id FROM bookings WHERE id = ? AND user_id = ? AND status = "active"',
            (booking_id, user_id)
        ) as cursor:
            if not await cursor.fetchone():
                return False
        
        await db.execute('UPDATE bookings SET status = "cancelled" WHERE id = ?', (booking_id,))
        await db.execute('DELETE FROM occupied_slots WHERE booking_id = ?', (booking_id,))
        await db.commit()
        return True

async def get_bookings_for_reminders():
    now = datetime.now()
    
    async with aiosqlite.connect(DB_NAME) as db:
        target_24h = (now + timedelta(hours=24)).strftime("%Y-%m-%d")
        async with db.execute(
            '''SELECT id, user_id, service_name, booking_date, booking_time 
               FROM bookings 
               WHERE status = 'active' 
               AND booking_date = ? 
               AND reminder_24h_sent = 0''',
            (target_24h,)
        ) as cursor:
            reminders_24h = await cursor.fetchall()
        
        target_2h_date = (now + timedelta(hours=2)).strftime("%Y-%m-%d")
        target_2h_time = (now + timedelta(hours=2)).strftime("%H:00")
        
        async with db.execute(
            '''SELECT id, user_id, service_name, booking_date, booking_time 
               FROM bookings 
               WHERE status = 'active' 
               AND booking_date = ? 
               AND booking_time = ?
               AND reminder_2h_sent = 0''',
            (target_2h_date, target_2h_time)
        ) as cursor:
            reminders_2h = await cursor.fetchall()
        
        return reminders_24h, reminders_2h

async def mark_reminder_sent(booking_id: int, reminder_type: str):
    async with aiosqlite.connect(DB_NAME) as db:
        if reminder_type == "24h":
            await db.execute('UPDATE bookings SET reminder_24h_sent = 1 WHERE id = ?', (booking_id,))
        elif reminder_type == "2h":
            await db.execute('UPDATE bookings SET reminder_2h_sent = 1 WHERE id = ?', (booking_id,))
        await db.commit()

# --- ФУНКЦИИ БЛОКИРОВКИ ВРЕМЕНИ ---
async def get_blocked_slots(date_str: str = None) -> list:
    async with aiosqlite.connect(DB_NAME) as db:
        if date_str:
            async with db.execute(
                'SELECT slot_time, reason, is_full_day FROM blocked_slots WHERE slot_date = ?',
                (date_str,)
            ) as cursor:
                return await cursor.fetchall()
        else:
            async with db.execute(
                'SELECT slot_date, slot_time, reason, is_full_day FROM blocked_slots ORDER BY slot_date, slot_time'
            ) as cursor:
                return await cursor.fetchall()

async def get_blocked_dates_for_month(year: int, month: int) -> set:
    """Возвращает множество дат, которые полностью заблокированы в указанном месяце"""
    start_date = date(year, month, 1)
    if month == 12:
        end_date = date(year + 1, 1, 1)
    else:
        end_date = date(year, month + 1, 1)
    
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            'SELECT slot_date FROM blocked_slots WHERE slot_date >= ? AND slot_date < ? AND is_full_day = 1',
            (start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
        ) as cursor:
            rows = await cursor.fetchall()
            return {row[0] for row in rows}

async def has_bookings_on_date(date_str: str) -> bool:
    """Проверяет, есть ли записи на указанную дату"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            'SELECT 1 FROM bookings WHERE booking_date = ? AND status = "active"',
            (date_str,)
        ) as cursor:
            return await cursor.fetchone() is not None

async def get_bookings_on_date(date_str: str) -> list:
    """Возвращает список записей на указанную дату"""
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            'SELECT id, user_name, service_name, booking_time FROM bookings WHERE booking_date = ? AND status = "active"',
            (date_str,)
        ) as cursor:
            return await cursor.fetchall()

async def add_blocked_full_day(date_str: str, reason: str = "Выходной") -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            # Проверяем, не заблокирован ли уже день
            async with db.execute(
                'SELECT 1 FROM blocked_slots WHERE slot_date = ? AND is_full_day = 1',
                (date_str,)
            ) as cursor:
                if await cursor.fetchone():
                    return False
            
            # Удаляем часовые блокировки за этот день
            await db.execute('DELETE FROM blocked_slots WHERE slot_date = ? AND is_full_day = 0', (date_str,))
            
            # Добавляем блокировку всего дня
            await db.execute(
                'INSERT INTO blocked_slots (slot_date, slot_time, reason, is_full_day) VALUES (?, ?, ?, 1)',
                (date_str, "00:00", reason)
            )
            await db.commit()
            return True
        except Exception as e:
            print(f"Ошибка блокировки дня: {e}")
            return False

async def add_blocked_hour(date_str: str, time_slot: str, reason: str = "Заблокировано") -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            # Проверяем, не заблокирован ли весь день
            async with db.execute(
                'SELECT 1 FROM blocked_slots WHERE slot_date = ? AND is_full_day = 1',
                (date_str,)
            ) as cursor:
                if await cursor.fetchone():
                    return False
            
            # Проверяем, не заблокирован ли уже этот час
            async with db.execute(
                'SELECT 1 FROM blocked_slots WHERE slot_date = ? AND slot_time = ? AND is_full_day = 0',
                (date_str, time_slot)
            ) as cursor:
                if await cursor.fetchone():
                    return False
            
            await db.execute(
                'INSERT INTO blocked_slots (slot_date, slot_time, reason, is_full_day) VALUES (?, ?, ?, 0)',
                (date_str, time_slot, reason)
            )
            await db.commit()
            return True
        except Exception as e:
            print(f"Ошибка блокировки часа: {e}")
            return False

async def remove_blocked_slot(date_str: str, time_slot: str = None) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        if time_slot:
            await db.execute(
                'DELETE FROM blocked_slots WHERE slot_date = ? AND slot_time = ? AND is_full_day = 0',
                (date_str, time_slot)
            )
        else:
            await db.execute(
                'DELETE FROM blocked_slots WHERE slot_date = ? AND is_full_day = 1',
                (date_str,)
            )
        await db.commit()
        return True

async def is_day_blocked(date_str: str) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            'SELECT 1 FROM blocked_slots WHERE slot_date = ? AND is_full_day = 1',
            (date_str,)
        ) as cursor:
            return await cursor.fetchone() is not None

async def get_blocked_hours(date_str: str) -> list:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            'SELECT slot_time FROM blocked_slots WHERE slot_date = ? AND is_full_day = 0',
            (date_str,)
        ) as cursor:
            slots = await cursor.fetchall()
            return [slot[0] for slot in slots]

# --- ФОН ЗАДАЧА ДЛЯ НАПОМИНАНИЙ ---
async def reminder_checker():
    await asyncio.sleep(10)
    while True:
        try:
            reminders_24h, reminders_2h = await get_bookings_for_reminders()
            
            for booking_id, user_id, service_name, booking_date, booking_time in reminders_24h:
                try:
                    date_obj = datetime.strptime(booking_date, "%Y-%m-%d")
                    formatted_date = date_obj.strftime("%d.%m.%Y")
                    await bot.send_message(
                        user_id,
                        f"⏰ НАПОМИНАНИЕ ЗА 24 ЧАСА!\n\n"
                        f"Дорогая клиентка! 🌸\n\n"
                        f"Напоминаю, что завтра {formatted_date} в {booking_time}\n"
                        f"у вас запланирована процедура: {service_name}\n\n"
                        f"Жду вас с нетерпением! 💅\n"
                        f"📍 Адрес: {SALON_ADDRESS}\n\n"
                        f"Если возникнут вопросы, пишите мастеру: @{MASTER_USERNAME}"
                    )
                    await mark_reminder_sent(booking_id, "24h")
                    print(f"✅ Напоминание за 24ч для записи #{booking_id}")
                except Exception as e:
                    print(f"❌ Ошибка: {e}")
            
            for booking_id, user_id, service_name, booking_date, booking_time in reminders_2h:
                try:
                    date_obj = datetime.strptime(booking_date, "%Y-%m-%d")
                    formatted_date = date_obj.strftime("%d.%m.%Y")
                    await bot.send_message(
                        user_id,
                        f"⏰ НАПОМИНАНИЕ ЗА 2 ЧАСА!\n\n"
                        f"Дорогая клиентка! 💅\n\n"
                        f"Через 2 часа, {formatted_date} в {booking_time}\n"
                        f"у вас запланирована процедура: {service_name}\n\n"
                        f"До встречи! 🌸\n"
                        f"📍 Адрес: {SALON_ADDRESS}"
                    )
                    await mark_reminder_sent(booking_id, "2h")
                    print(f"✅ Напоминание за 2ч для записи #{booking_id}")
                except Exception as e:
                    print(f"❌ Ошибка: {e}")
            
            await asyncio.sleep(1800)
        except Exception as e:
            print(f"❌ Ошибка в reminder_checker: {e}")
            await asyncio.sleep(60)

# --- АДМИН-ФУНКЦИИ ---
async def get_all_bookings_for_admin() -> list:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            '''SELECT id, user_name, username, service_name, booking_date, booking_time 
               FROM bookings 
               WHERE status = 'active' 
               ORDER BY booking_date, booking_time'''
        ) as cursor:
            return await cursor.fetchall()

async def get_bookings_by_date(date_str: str) -> list:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            '''SELECT id, user_name, service_name, booking_time
               FROM bookings 
               WHERE booking_date = ? AND status = 'active'
               ORDER BY booking_time''',
            (date_str,)
        ) as cursor:
            return await cursor.fetchall()

async def admin_cancel_booking_by_id(booking_id: int) -> bool:
    async with aiosqlite.connect(DB_NAME) as db:
        async with db.execute(
            'SELECT user_id FROM bookings WHERE id = ? AND status = "active"',
            (booking_id,)
        ) as cursor:
            result = await cursor.fetchone()
        
        if not result:
            return False
        
        user_id = result[0]
        
        await db.execute('UPDATE bookings SET status = "cancelled" WHERE id = ?', (booking_id,))
        await db.execute('DELETE FROM occupied_slots WHERE booking_id = ?', (booking_id,))
        await db.commit()
        
        try:
            await bot.send_message(
                user_id,
                f"❌ ВНИМАНИЕ!\n\nМастер отменил вашу запись №{booking_id}.\nСвяжитесь с мастером: @{MASTER_USERNAME}"
            )
        except:
            pass
        
        return True

# --- КЛАВИАТУРЫ ---
def main_keyboard():
    buttons = [
        [KeyboardButton(text="📋 Услуги и цены"), KeyboardButton(text="📅 Записаться")],
        [KeyboardButton(text="📸 Мои работы"), KeyboardButton(text="📍 Контакты")],
        [KeyboardButton(text="📋 Мои последние записи")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def admin_main_keyboard():
    buttons = [
        [KeyboardButton(text="📋 Услуги и цены"), KeyboardButton(text="📅 Записаться")],
        [KeyboardButton(text="📸 Мои работы"), KeyboardButton(text="📍 Контакты")],
        [KeyboardButton(text="📋 Мои последние записи")],
        [KeyboardButton(text="👑 Админ-панель")]
    ]
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True)

def services_keyboard():
    buttons = []
    for key, service in services.items():
        buttons.append([InlineKeyboardButton(
            text=f"{service['name']} - {service['price']}₽",
            callback_data=f"service_{key}"
        )])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

async def generate_calendar(year: int, month: int, selected_service: str = None):
    """Асинхронная генерация календаря с учётом заблокированных дней и гибкого графика"""
    month_name_ru = {
        1: "Январь", 2: "Февраль", 3: "Март", 4: "Апрель",
        5: "Май", 6: "Июнь", 7: "Июль", 8: "Август",
        9: "Сентябрь", 10: "Октябрь", 11: "Ноябрь", 12: "Декабрь"
    }
    
    # Получаем заблокированные дни на этот месяц
    blocked_dates = await get_blocked_dates_for_month(year, month)
    
    month_matrix = monthcalendar(year, month)
    keyboard = []
    
    nav_row = []
    prev_month = month - 1 if month > 1 else 12
    prev_year = year if month > 1 else year - 1
    next_month = month + 1 if month < 12 else 1
    next_year = year if month < 12 else year + 1
    
    nav_row.append(InlineKeyboardButton(text="◀️", callback_data=f"cal_prev_{prev_year}_{prev_month}_{selected_service or ''}"))
    nav_row.append(InlineKeyboardButton(text=f"{month_name_ru[month]} {year}", callback_data="ignore"))
    nav_row.append(InlineKeyboardButton(text="▶️", callback_data=f"cal_next_{next_year}_{next_month}_{selected_service or ''}"))
    keyboard.append(nav_row)
    
    week_days = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
    week_row = [InlineKeyboardButton(text=day, callback_data="ignore") for day in week_days]
    keyboard.append(week_row)
    
    today = date.today()
    now = datetime.now()
    
    for week in month_matrix:
        row = []
        for day in week:
            if day == 0:
                row.append(InlineKeyboardButton(text=" ", callback_data="ignore"))
            else:
                cell_date = date(year, month, day)
                date_str = cell_date.strftime("%Y-%m-%d")
                weekday = cell_date.weekday()
                
                # Проверяем, рабочий ли день (по гибкому графику)
                is_working = is_work_day(weekday)
                
                # Проверяем, можно ли записаться на сегодня
                can_book_today = True
                if cell_date == today:
                    work_hours = get_all_work_hours_for_day(weekday)
                    if work_hours:
                        future_hours = [int(h.split(':')[0]) for h in work_hours if int(h.split(':')[0]) > now.hour]
                        if not future_hours:
                            can_book_today = False
                
                if date_str in blocked_dates:
                    text = f"🔴{day}"
                elif not is_working:
                    text = f"🔴{day}"
                elif cell_date < today:
                    text = f"❌{day}"
                elif cell_date == today and not can_book_today:
                    text = f"❌{day}"
                else:
                    text = f"{day}"
                
                row.append(InlineKeyboardButton(
                    text=text,
                    callback_data=f"date_{date_str}_{selected_service or ''}"
                ))
        keyboard.append(row)
    
    keyboard.append([InlineKeyboardButton(text="🔙 Назад к услугам", callback_data="back_to_services")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def time_keyboard(date_str: str, service_key: str = None):
    selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    weekday = selected_date.weekday()
    today = datetime.now().date()
    
    # Получаем рабочие часы для этого дня
    work_hours = get_all_work_hours_for_day(weekday)
    
    if not work_hours:
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ В этот день мастер не работает", callback_data="ignore")],
            [InlineKeyboardButton(text="📅 Назад к календарю", callback_data=f"back_to_calendar_{service_key or ''}")]
        ])
        return keyboard
    
    busy = await get_occupied_slots(date_str)
    blocked_hours = await get_blocked_hours(date_str)
    
    busy += blocked_hours
    free_hours = [h for h in work_hours if h not in busy]
    
    # Если это сегодняшний день - убираем прошедшие часы
    if selected_date == today:
        current_hour = datetime.now().hour
        free_hours = [h for h in free_hours if int(h.split(':')[0]) > current_hour]
    
    buttons = []
    row = []
    for i, hour in enumerate(free_hours):
        row.append(InlineKeyboardButton(text=hour, callback_data=f"time_{date_str}_{hour}_{service_key or ''}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    
    if not free_hours:
        buttons.append([InlineKeyboardButton(text="❌ Нет свободных окон", callback_data="ignore")])
    
    buttons.append([InlineKeyboardButton(text="📅 Назад к календарю", callback_data=f"back_to_calendar_{service_key or ''}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirmation_keyboard(service_name: str, date_str: str, time_slot: str, service_key: str):
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Подтвердить", callback_data=f"confirm_{service_key}_{date_str}_{time_slot}"),
            InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_booking_temp")
        ]
    ])
    return keyboard

# --- АДМИН-КЛАВИАТУРЫ ---
def admin_panel_keyboard():
    buttons = [
        [InlineKeyboardButton(text="📋 Все записи", callback_data="admin_all_bookings")],
        [InlineKeyboardButton(text="📅 Записи по датам", callback_data="admin_bookings_by_date")],
        [InlineKeyboardButton(text="🚫 Блокировка времени", callback_data="admin_block_menu")],
        [InlineKeyboardButton(text="🔙 Назад в меню", callback_data="admin_back_to_menu")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_bookings_by_date_keyboard():
    buttons = []
    today = datetime.now()
    
    for i in range(7):
        target_date = today + timedelta(days=i)
        date_str = target_date.strftime("%Y-%m-%d")
        weekday = DAYS_RU_SHORT[target_date.weekday()]
        display = target_date.strftime(f"%d.%m.%Y ({weekday})")
        buttons.append([InlineKeyboardButton(text=display, callback_data=f"admin_date_{date_str}")])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_block_keyboard(date_str: str = None):
    buttons = []
    
    if date_str:
        buttons.append([InlineKeyboardButton(text="🚫 Заблокировать весь день", callback_data=f"admin_block_full_day_{date_str}")])
        buttons.append([InlineKeyboardButton(text="⏰ Заблокировать конкретный час", callback_data=f"admin_block_hour_menu_{date_str}")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    else:
        today = datetime.now()
        for i in range(30):
            target_date = today + timedelta(days=i)
            date_str_btn = target_date.strftime("%Y-%m-%d")
            weekday = DAYS_RU_SHORT[target_date.weekday()]
            display = target_date.strftime(f"%d.%m.%Y ({weekday})")
            buttons.append([InlineKeyboardButton(text=f"📅 {display}", callback_data=f"admin_block_date_{date_str_btn}")])
        buttons.append([InlineKeyboardButton(text="📋 Показать заблокированные", callback_data="admin_show_blocked")])
        buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")])
    
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def admin_block_hour_keyboard(date_str: str):
    selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    weekday = selected_date.weekday()
    
    # Получаем рабочие часы для этого дня
    work_hours = get_all_work_hours_for_day(weekday)
    
    buttons = []
    for hour in work_hours:
        buttons.append([InlineKeyboardButton(text=f"🔒 {hour}", callback_data=f"admin_block_hour_{date_str}_{hour}")])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад к дате", callback_data=f"admin_block_date_back_{date_str}")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def confirm_block_day_keyboard(date_str: str):
    """Клавиатура подтверждения блокировки дня"""
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [
            InlineKeyboardButton(text="✅ Да, заблокировать", callback_data=f"admin_confirm_block_day_{date_str}"),
            InlineKeyboardButton(text="❌ Нет, отмена", callback_data=f"admin_cancel_block_day_{date_str}")
        ]
    ])
    return keyboard

# --- ОБРАБОТЧИКИ КЛИЕНТА ---
@dp.message(Command("start"))
async def cmd_start(message: Message):
    if message.from_user.id == ADMIN_ID:
        keyboard = admin_main_keyboard()
    else:
        keyboard = main_keyboard()
    
    await message.answer(
        "🌸 Привет! Я бот-помощник маникюрного мастера.\n\n"
        "Я помогу записаться, посмотреть цены и работы.\n"
        "Что желаете?",
        reply_markup=keyboard
    )

@dp.message(lambda m: m.text == "📋 Услуги и цены")
async def show_services(message: Message):
    text = "💰 *Наши услуги:*\n\n"
    for s in services.values():
        text += f"• {s['name']} — {s['price']}₽ (~{s['time']} мин)\n"
    text += "\nВыберите услугу для записи:"
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=services_keyboard())

@dp.message(lambda m: m.text == "📅 Записаться")
async def start_booking(message: Message):
    await message.answer("Выберите услугу:", reply_markup=services_keyboard())

@dp.message(lambda m: m.text == "📋 Мои последние записи")
async def show_my_bookings(message: Message):
    bookings = await get_user_bookings(message.from_user.id)
    
    if not bookings:
        await message.answer("📭 У вас пока нет активных записей.\n\nХотите записаться? Нажмите '📅 Записаться'")
        return
    
    text = "📋 *Ваши последние записи:*\n\n"
    buttons = []
    
    for booking_id, service_name, booking_date, booking_time in bookings:
        date_obj = datetime.strptime(booking_date, "%Y-%m-%d")
        formatted_date = date_obj.strftime("%d.%m.%Y")
        text += f"✂️ *{service_name}*\n"
        text += f"   📅 {formatted_date}\n"
        text += f"   ⏰ {booking_time}\n\n"
        
        buttons.append([InlineKeyboardButton(
            text=f"❌ Отменить {service_name} ({formatted_date} {booking_time})",
            callback_data=f"cancel_{booking_id}"
        )])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад в меню", callback_data="back_to_menu")])
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    await message.answer(text, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

@dp.message(lambda m: m.text == "📸 Мои работы")
async def show_works(message: Message):
    # Папка с фото - используем абсолютный путь
    bot_dir = os.path.dirname(os.path.abspath(__file__))
    photos_folder = os.path.join(bot_dir, "photos")
    photos_found = []
    
    # Проверяем, есть ли папка с локальными фото
    if os.path.exists(photos_folder):
        photos_found = [f for f in os.listdir(photos_folder) if f.endswith(('.jpg', '.jpeg', '.png', '.gif'))]
    
    # Если есть локальные фото
    if photos_found:
        # Отправляем первое фото из папки
        first_photo = FSInputFile(os.path.join(photos_folder, photos_found[0]))
        await message.answer_photo(
            photo=first_photo,
            caption=f"✨ *Мои работы*\n\n"
                    f"📸 Всего работ в портфолио: {len(photos_found)}\n\n"
                    f"💅 Все техники и дизайны\n\n"
                    f"📱 Больше фото в Instagram: [{MASTER_INSTAGRAM}](https://instagram.com/{MASTER_INSTAGRAM.replace('@', '')})\n\n"
                    f"✂️ Записаться: /start",
            parse_mode=ParseMode.MARKDOWN
        )
        
        # Если есть еще фото, отправляем их отдельным сообщением (по одному)
        if len(photos_found) > 1:
            await message.answer("📸 *Ещё примеры моих работ:*", parse_mode=ParseMode.MARKDOWN)
            for photo_file in photos_found[1:3]:
                photo = FSInputFile(os.path.join(photos_folder, photo_file))
                await message.answer_photo(photo=photo)
    else:
        # Если нет локальных фото, используем ссылку из config
        if PORTFOLIO_PHOTO_URL and PORTFOLIO_PHOTO_URL != "https://example.com/manicure_example.jpg":
            await message.answer_photo(
                photo=PORTFOLIO_PHOTO_URL,
                caption=f"{PORTFOLIO_CAPTION}\n\n"
                        f"📱 Больше фото в Instagram: [{MASTER_INSTAGRAM}](https://instagram.com/{MASTER_INSTAGRAM.replace('@', '')})\n\n"
                        f"✂️ Записаться: /start",
                parse_mode=ParseMode.MARKDOWN
            )
        else:
            await message.answer(
                f"📸 *Портфолио мастера*\n\n"
                f"Скоро здесь появятся фото моих работ! 💅\n\n"
                f"А пока приглашаю посмотреть мои работы в Instagram:\n"
                f"📱 [{MASTER_INSTAGRAM}](https://instagram.com/{MASTER_INSTAGRAM.replace('@', '')})\n\n"
                f"✂️ Записаться можно по кнопке '📅 Записаться'",
                parse_mode=ParseMode.MARKDOWN
            )
            
@dp.message(lambda m: m.text == "📍 Контакты")
async def show_contacts(message: Message):
    # Формируем строку с рабочими днями и часами
    full_days_names = ["понедельник", "вторник", "среда", "четверг", "пятница", "суббота", "воскресенье"]
    
    schedule_lines = []
    for i in range(7):
        intervals = get_work_hours_for_day(i)
        if intervals:
            hours_str = []
            for start, end in intervals:
                hours_str.append(f"{start}:00-{end}:00")
            schedule_lines.append(f"{full_days_names[i].capitalize()}: {', '.join(hours_str)}")
    
    work_schedule = "\n".join(schedule_lines) if schedule_lines else "Выходной"
    
    await message.answer(
        f"📍 Адрес: {SALON_ADDRESS}\n"
        f"📞 Телефон: {MASTER_PHONE}\n"
        f"📱 Instagram: {MASTER_INSTAGRAM}\n"
        f"📅 График работы:\n{work_schedule}\n\n"
        f"📩 По всем вопросам: @{MASTER_USERNAME}"
    )

# --- ОБРАБОТЧИКИ ЗАПИСИ ---
@dp.callback_query(lambda c: c.data.startswith("service_"))
async def select_service(callback: CallbackQuery):
    service_key = callback.data.split("_")[1]
    user_id = callback.from_user.id
    
    if user_id not in user_data:
        user_data[user_id] = {}
    user_data[user_id]["service"] = service_key
    
    now = datetime.now()
    calendar = await generate_calendar(now.year, now.month, service_key)
    await callback.message.edit_text(
        f"✅ Выбрана услуга: {services[service_key]['name']} ({services[service_key]['price']}₽)\n\n"
        "📅 Выберите дату:\n"
        "🔴 - выходной/нерабочий день или заблокированный день\n"
        "❌ - прошедшая дата\n"
        "число - доступно для записи\n\n"
        "Используйте стрелки для навигации по месяцам:",
        reply_markup=calendar
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("cal_prev_") or c.data.startswith("cal_next_"))
async def change_month(callback: CallbackQuery):
    parts = callback.data.split("_")
    year = int(parts[2])
    month = int(parts[3])
    service_key = parts[4] if len(parts) > 4 else None
    
    calendar = await generate_calendar(year, month, service_key)
    await callback.message.edit_reply_markup(reply_markup=calendar)
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("date_"))
async def select_date(callback: CallbackQuery):
    parts = callback.data.split("_")
    date_str = parts[1]
    service_key = parts[2] if len(parts) > 2 else None
    
    user_id = callback.from_user.id
    
    if user_id not in user_data or "service" not in user_data[user_id]:
        await callback.answer("Пожалуйста, выберите услугу заново", show_alert=True)
        return
    
    selected_date = datetime.strptime(date_str, "%Y-%m-%d").date()
    weekday = selected_date.weekday()
    today = datetime.now().date()
    now = datetime.now()
    
    # Проверяем по гибкому графику
    if not is_work_day(weekday):
        await callback.answer("❌ В этот день мастер не работает!", show_alert=True)
        return
    
    if selected_date < today:
        await callback.answer("❌ Нельзя записаться на прошедшую дату!", show_alert=True)
        return
    
    # Если выбран сегодняшний день, проверяем, есть ли ещё часы
    if selected_date == today:
        work_hours = get_all_work_hours_for_day(weekday)
        future_hours = [int(h.split(':')[0]) for h in work_hours if int(h.split(':')[0]) > now.hour]
        if not future_hours:
            await callback.answer("❌ На сегодня уже нет свободного времени!", show_alert=True)
            return
    
    if await is_day_blocked(date_str):
        await callback.answer("❌ Этот день полностью заблокирован мастером!", show_alert=True)
        return
    
    user_data[user_id]["date"] = date_str
    
    await callback.message.edit_text(
        f"📅 Выбрана дата: {selected_date.strftime('%d.%m.%Y')}\n\n"
        f"⏰ Выберите свободное время:",
        reply_markup=await time_keyboard(date_str, service_key)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("time_"))
async def select_time(callback: CallbackQuery):
    parts = callback.data.split("_")
    date_str = parts[1]
    time_slot = parts[2]
    service_key = parts[3] if len(parts) > 3 else None
    
    user_id = callback.from_user.id
    
    if user_id not in user_data:
        await callback.answer("Ошибка, начните запись заново", show_alert=True)
        return
    
    service_key = user_data[user_id]["service"]
    service_name = services[service_key]["name"]
    selected_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    
    user_data[user_id]["temp_booking"] = {
        "date_str": date_str,
        "time_slot": time_slot,
        "service_key": service_key,
        "service_name": service_name,
        "selected_date": selected_date
    }
    
    await callback.message.edit_text(
        f"📝 Пожалуйста, подтвердите запись:\n\n"
        f"💅 Услуга: {service_name}\n"
        f"📅 Дата: {selected_date}\n"
        f"⏰ Время: {time_slot}\n\n"
        f"✅ Всё верно?",
        reply_markup=confirmation_keyboard(service_name, date_str, time_slot, service_key)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("confirm_"))
async def confirm_booking(callback: CallbackQuery):
    parts = callback.data.split("_")
    service_key = parts[1]
    date_str = parts[2]
    time_slot = parts[3]
    
    user_id = callback.from_user.id
    
    if user_id not in user_data:
        await callback.answer("Ошибка, начните запись заново", show_alert=True)
        return
    
    service_name = services[service_key]["name"]
    selected_date = datetime.strptime(date_str, "%Y-%m-%d").strftime("%d.%m.%Y")
    
    booking_id = await add_booking(
        user_id=user_id,
        user_name=callback.from_user.full_name,
        username=callback.from_user.username,
        service_key=service_key,
        service_name=service_name,
        booking_date=date_str,
        booking_time=time_slot
    )
    
    await bot.send_message(
        ADMIN_ID,
        f"🆕 НОВАЯ ЗАПИСЬ!\n\n"
        f"👤 Клиент: {callback.from_user.full_name}\n"
        f"🆔 Username: @{callback.from_user.username or 'не указан'}\n"
        f"💅 Услуга: {service_name}\n"
        f"💰 Цена: {services[service_key]['price']}₽\n"
        f"📅 Дата: {selected_date}\n"
        f"⏰ Время: {time_slot}\n"
        f"🆔 ID: {booking_id}"
    )
    
    await callback.message.delete()
    
    if callback.from_user.id == ADMIN_ID:
        reply_markup = admin_main_keyboard()
    else:
        reply_markup = main_keyboard()
    
    await callback.message.answer(
        f"✅ ВЫ УСПЕШНО ЗАПИСАНЫ!\n\n"
        f"💅 Услуга: {service_name}\n"
        f"📅 Дата: {selected_date}\n"
        f"⏰ Время: {time_slot}\n"
        f"🆔 Код: {booking_id}\n\n"
        f"📍 Адрес: {SALON_ADDRESS}\n\n"
        f"🌼 Жду вас! Если не сможете прийти — предупредите заранее.\n"
        f"📩 По всем вопросам: @{MASTER_USERNAME}\n\n"
        f"⏰ Важно: Я пришлю напоминание за 24 часа и за 2 часа до записи!",
        reply_markup=reply_markup
    )
    
    if user_id in user_data:
        del user_data[user_id]
    
    await callback.answer("✅ Запись создана!")

@dp.callback_query(lambda c: c.data == "cancel_booking_temp")
async def cancel_temp_booking(callback: CallbackQuery):
    user_id = callback.from_user.id
    
    await callback.message.delete()
    
    if callback.from_user.id == ADMIN_ID:
        reply_markup = admin_main_keyboard()
    else:
        reply_markup = main_keyboard()
    
    await callback.message.answer(
        "❌ Запись отменена. Возвращайтесь, когда будете готовы!",
        reply_markup=reply_markup
    )
    
    if user_id in user_data:
        del user_data[user_id]
    
    await callback.answer("Запись отменена")

@dp.callback_query(lambda c: c.data.startswith("cancel_"))
async def cancel_existing_booking(callback: CallbackQuery):
    booking_id = int(callback.data.split("_")[1])
    user_id = callback.from_user.id
    
    success = await cancel_booking(booking_id, user_id)
    
    await callback.message.delete()
    
    if success:
        if callback.from_user.id == ADMIN_ID:
            reply_markup = admin_main_keyboard()
        else:
            reply_markup = main_keyboard()
        
        await callback.message.answer(
            "✅ Запись успешно отменена!\n\n"
            "Освободившееся время стало доступно для других клиентов.",
            reply_markup=reply_markup
        )
        
        await bot.send_message(
            ADMIN_ID,
            f"❌ ОТМЕНА ЗАПИСИ!\n\nОтменена запись #{booking_id}\n👤 Клиент: {callback.from_user.full_name}"
        )
        
        await callback.answer("Запись отменена")
    else:
        if callback.from_user.id == ADMIN_ID:
            reply_markup = admin_main_keyboard()
        else:
            reply_markup = main_keyboard()
        
        await callback.message.answer(
            "❌ Не удалось отменить запись. Возможно, она уже была отменена.",
            reply_markup=reply_markup
        )
        await callback.answer("Ошибка отмены", show_alert=True)

@dp.callback_query(lambda c: c.data == "back_to_menu")
async def back_to_menu(callback: CallbackQuery):
    await callback.message.delete()
    
    if callback.from_user.id == ADMIN_ID:
        reply_markup = admin_main_keyboard()
    else:
        reply_markup = main_keyboard()
    
    await callback.message.answer("Главное меню:", reply_markup=reply_markup)
    await callback.answer()

@dp.callback_query(lambda c: c.data == "back_to_services")
async def back_to_services(callback: CallbackQuery):
    await callback.message.edit_text(
        "Выберите услугу:",
        reply_markup=services_keyboard()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data.startswith("back_to_calendar_"))
async def back_to_calendar(callback: CallbackQuery):
    service_key = callback.data.split("_")[3]
    now = datetime.now()
    
    calendar = await generate_calendar(now.year, now.month, service_key)
    await callback.message.edit_text(
        f"✅ Выбрана услуга: {services[service_key]['name']} ({services[service_key]['price']}₽)\n\n"
        "📅 Выберите дату:\n"
        "🔴 - выходной/нерабочий день или заблокированный день\n"
        "❌ - прошедшая дата\n"
        "число - доступно для записи",
        reply_markup=calendar
    )
    await callback.answer()

@dp.callback_query(lambda c: c.data == "ignore")
async def ignore_callback(callback: CallbackQuery):
    await callback.answer()

# --- АДМИН-ОБРАБОТЧИКИ ---
@dp.message(lambda m: m.text == "👑 Админ-панель" and m.from_user.id == ADMIN_ID)
async def admin_panel(message: Message):
    await message.answer(
        "👑 Админ-панель мастера\n\nВыберите действие:",
        reply_markup=admin_panel_keyboard()
    )

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data == "admin_all_bookings")
async def admin_show_all_bookings(callback: CallbackQuery):
    bookings = await get_all_bookings_for_admin()
    
    if not bookings:
        await callback.message.edit_text("📭 Нет активных записей")
        await callback.answer()
        return
    
    text = "📊 ВСЕ АКТИВНЫЕ ЗАПИСИ:\n\n"
    
    for booking_id, user_name, username, service_name, booking_date, booking_time in bookings:
        date_obj = datetime.strptime(booking_date, "%Y-%m-%d")
        formatted_date = date_obj.strftime("%d.%m.%Y")
        text += f"┌ ID: {booking_id}\n"
        text += f"├ 👤 Клиент: {user_name}\n"
        text += f"├ 💅 Услуга: {service_name}\n"
        text += f"├ 📅 Дата: {formatted_date}\n"
        text += f"└ ⏰ Время: {booking_time}\n\n"
    
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_back")]
    ])
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data == "admin_bookings_by_date")
async def admin_show_date_selector(callback: CallbackQuery):
    await callback.message.edit_text(
        "📅 Выберите дату:\n\nПоказаны следующие 7 дней:",
        reply_markup=admin_bookings_by_date_keyboard()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data.startswith("admin_date_"))
async def admin_show_bookings_by_date(callback: CallbackQuery):
    date_str = callback.data.split("_")[2]
    bookings = await get_bookings_by_date(date_str)
    
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    formatted_date = date_obj.strftime(f"%d.%m.%Y ({DAYS_RU[date_obj.weekday()]})")
    
    if not bookings:
        await callback.message.edit_text(
            f"📭 Записи на {formatted_date}:\n\nНет записей",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_bookings_by_date")]
            ])
        )
        await callback.answer()
        return
    
    text = f"📅 ЗАПИСИ НА {formatted_date}:\n\n"
    buttons = []
    
    for booking_id, user_name, service_name, booking_time in bookings:
        text += f"⏰ {booking_time}\n"
        text += f"   👤 {user_name}\n"
        text += f"   💅 {service_name}\n"
        text += f"   🆔 ID: {booking_id}\n\n"
        buttons.append([InlineKeyboardButton(
            text=f"❌ Отменить {booking_time} - {user_name}",
            callback_data=f"admin_cancel_{booking_id}"
        )])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_bookings_by_date")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

# --- БЛОКИРОВКА ВРЕМЕНИ (АДМИН) ---
@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data == "admin_block_menu")
async def admin_block_menu(callback: CallbackQuery):
    await callback.message.edit_text(
        "🚫 Блокировка времени\n\n"
        "Вы можете:\n"
        "• 🚫 Заблокировать весь день (выходной, отпуск)\n"
        "• ⏰ Заблокировать конкретный час (обед, личные дела)\n\n"
        "Выберите дату:",
        reply_markup=admin_block_keyboard()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data.startswith("admin_block_date_"))
async def admin_block_date(callback: CallbackQuery):
    date_str = callback.data.replace("admin_block_date_", "")
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    formatted_date = date_obj.strftime(f"%d.%m.%Y ({DAYS_RU[date_obj.weekday()]})")
    
    await callback.message.edit_text(
        f"🚫 Блокировка времени\n\n"
        f"📅 Дата: {formatted_date}\n\n"
        f"Выберите действие:",
        reply_markup=admin_block_keyboard(date_str)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data.startswith("admin_block_full_day_"))
async def admin_block_full_day(callback: CallbackQuery):
    date_str = callback.data.replace("admin_block_full_day_", "")
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    formatted_date = date_obj.strftime(f"%d.%m.%Y ({DAYS_RU[date_obj.weekday()]})")
    
    # Проверяем, есть ли записи на эту дату
    has_bookings = await has_bookings_on_date(date_str)
    
    if has_bookings:
        bookings = await get_bookings_on_date(date_str)
        bookings_text = "\n".join([f"⏰ {b[3]} - {b[1]} ({b[2]})" for b in bookings])
        
        await callback.message.edit_text(
            f"⚠️ ВНИМАНИЕ!\n\n"
            f"На дату {formatted_date} есть активные записи:\n\n"
            f"{bookings_text}\n\n"
            f"Если вы заблокируете этот день, эти записи будут отменены.\n\n"
            f"Вы уверены, что хотите заблокировать этот день?",
            reply_markup=confirm_block_day_keyboard(date_str)
        )
    else:
        success = await add_blocked_full_day(date_str, "Выходной")
        
        if success:
            await callback.message.edit_text(
                f"✅ День заблокирован!\n\n"
                f"📅 Дата: {formatted_date}\n\n"
                f"Клиенты не смогут записаться на этот день.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 В меню блокировки", callback_data="admin_block_menu")],
                    [InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_back")]
                ])
            )
        else:
            await callback.message.edit_text(
                f"❌ Ошибка!\n\nЭтот день уже заблокирован.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🔙 Попробовать снова", callback_data="admin_block_menu")],
                    [InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_back")]
                ])
            )
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data.startswith("admin_confirm_block_day_"))
async def admin_confirm_block_day(callback: CallbackQuery):
    date_str = callback.data.replace("admin_confirm_block_day_", "")
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    formatted_date = date_obj.strftime(f"%d.%m.%Y ({DAYS_RU[date_obj.weekday()]})")
    
    # Отменяем все записи на этот день
    bookings = await get_bookings_on_date(date_str)
    for booking_id, user_name, service_name, booking_time in bookings:
        await admin_cancel_booking_by_id(booking_id)
    
    # Блокируем день
    success = await add_blocked_full_day(date_str, "Выходной (записи отменены)")
    
    if success:
        await callback.message.edit_text(
            f"✅ День заблокирован!\n\n"
            f"📅 Дата: {formatted_date}\n\n"
            f"Все записи на этот день были отменены.\n"
            f"Клиенты получили уведомления.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 В меню блокировки", callback_data="admin_block_menu")],
                [InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_back")]
            ])
        )
    else:
        await callback.message.edit_text(
            f"❌ Ошибка при блокировке дня!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Попробовать снова", callback_data="admin_block_menu")],
                [InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_back")]
            ])
        )
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data.startswith("admin_cancel_block_day_"))
async def admin_cancel_block_day(callback: CallbackQuery):
    date_str = callback.data.replace("admin_cancel_block_day_", "")
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    formatted_date = date_obj.strftime(f"%d.%m.%Y ({DAYS_RU[date_obj.weekday()]})")
    
    await callback.message.edit_text(
        f"❌ Блокировка дня {formatted_date} отменена.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 В меню блокировки", callback_data="admin_block_menu")],
            [InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_back")]
        ])
    )
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data.startswith("admin_block_hour_menu_"))
async def admin_block_hour_menu(callback: CallbackQuery):
    date_str = callback.data.replace("admin_block_hour_menu_", "")
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    formatted_date = date_obj.strftime(f"%d.%m.%Y ({DAYS_RU[date_obj.weekday()]})")
    
    await callback.message.edit_text(
        f"🚫 Блокировка часа\n\n"
        f"📅 Дата: {formatted_date}\n\n"
        f"Выберите час для блокировки:",
        reply_markup=admin_block_hour_keyboard(date_str)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data.startswith("admin_block_hour_") and not c.data.startswith("admin_block_hour_menu"))
async def admin_block_hour(callback: CallbackQuery):
    parts = callback.data.split("_")
    date_str = parts[3]
    time_slot = parts[4]
    
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    formatted_date = date_obj.strftime(f"%d.%m.%Y ({DAYS_RU[date_obj.weekday()]})")
    
    success = await add_blocked_hour(date_str, time_slot, "Заблокировано мастером")
    
    if success:
        await callback.message.edit_text(
            f"✅ Время заблокировано!\n\n"
            f"📅 Дата: {formatted_date}\n"
            f"⏰ Время: {time_slot}\n\n"
            f"Клиенты не смогут записаться на это время.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 В меню блокировки", callback_data="admin_block_menu")],
                [InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_back")]
            ])
        )
    else:
        await callback.message.edit_text(
            f"❌ Ошибка!\n\nЭто время уже заблокировано или занято записью.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Попробовать снова", callback_data=f"admin_block_hour_menu_{date_str}")],
                [InlineKeyboardButton(text="🔙 В админ-панель", callback_data="admin_back")]
            ])
        )
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data.startswith("admin_block_date_back_"))
async def admin_block_date_back(callback: CallbackQuery):
    date_str = callback.data.replace("admin_block_date_back_", "")
    date_obj = datetime.strptime(date_str, "%Y-%m-%d")
    formatted_date = date_obj.strftime(f"%d.%m.%Y ({DAYS_RU[date_obj.weekday()]})")
    
    await callback.message.edit_text(
        f"🚫 Блокировка времени\n\n"
        f"📅 Дата: {formatted_date}\n\n"
        f"Выберите действие:",
        reply_markup=admin_block_keyboard(date_str)
    )
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data == "admin_show_blocked")
async def admin_show_blocked(callback: CallbackQuery):
    blocked = await get_blocked_slots()
    
    if not blocked:
        await callback.message.edit_text(
            "📭 Нет заблокированных слотов",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_block_menu")]
            ])
        )
        await callback.answer()
        return
    
    text = "🚫 ЗАБЛОКИРОВАННОЕ ВРЕМЯ:\n\n"
    buttons = []
    
    for slot_date, slot_time, reason, is_full_day in blocked:
        date_obj = datetime.strptime(slot_date, "%Y-%m-%d")
        formatted_date = date_obj.strftime(f"%d.%m.%Y ({DAYS_RU_SHORT[date_obj.weekday()]})")
        
        if is_full_day:
            text += f"📅 {formatted_date} - 🔴 Весь день\n"
            buttons.append([InlineKeyboardButton(
                text=f"🔓 Разблокировать {formatted_date} (весь день)",
                callback_data=f"admin_unblock_day_{slot_date}"
            )])
        else:
            text += f"📅 {formatted_date} - ⏰ {slot_time}\n"
            buttons.append([InlineKeyboardButton(
                text=f"🔓 Разблокировать {formatted_date} {slot_time}",
                callback_data=f"admin_unblock_hour_{slot_date}_{slot_time}"
            )])
    
    buttons.append([InlineKeyboardButton(text="🔙 Назад", callback_data="admin_block_menu")])
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
    
    await callback.message.edit_text(text, reply_markup=keyboard)
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data.startswith("admin_unblock_day_"))
async def admin_unblock_day(callback: CallbackQuery):
    date_str = callback.data.replace("admin_unblock_day_", "")
    
    await remove_blocked_slot(date_str)
    
    await callback.message.edit_text(
        f"✅ День разблокирован!\n\n"
        f"📅 {date_str}\n"
        f"Теперь клиенты могут записываться на этот день.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_show_blocked")]
        ])
    )
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data.startswith("admin_unblock_hour_"))
async def admin_unblock_hour(callback: CallbackQuery):
    parts = callback.data.split("_")
    date_str = parts[3]
    time_slot = parts[4]
    
    await remove_blocked_slot(date_str, time_slot)
    
    await callback.message.edit_text(
        f"✅ Время разблокировано!\n\n"
        f"📅 {date_str} ⏰ {time_slot}\n"
        f"Теперь клиенты могут записываться на это время.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_show_blocked")]
        ])
    )
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data.startswith("admin_cancel_"))
async def admin_cancel_booking(callback: CallbackQuery):
    booking_id = int(callback.data.split("_")[2])
    success = await admin_cancel_booking_by_id(booking_id)
    
    if success:
        await callback.message.edit_text(
            f"✅ Запись #{booking_id} отменена!",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔙 Назад", callback_data="admin_bookings_by_date")]
            ])
        )
        await callback.answer("Запись отменена")
    else:
        await callback.answer("❌ Запись не найдена", show_alert=True)

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data == "admin_back")
async def admin_back(callback: CallbackQuery):
    await callback.message.edit_text(
        "👑 Админ-панель мастера\n\nВыберите действие:",
        reply_markup=admin_panel_keyboard()
    )
    await callback.answer()

@dp.callback_query(lambda c: c.from_user.id == ADMIN_ID and c.data == "admin_back_to_menu")
async def admin_back_to_menu(callback: CallbackQuery):
    await callback.message.delete()
    await callback.message.answer(
        "Вы вернулись в главное меню",
        reply_markup=admin_main_keyboard()
    )
    await callback.answer()

# --- ЗАПУСК ---
async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    
    asyncio.create_task(reminder_checker())
    
    print("✅ Бот успешно запущен!")
    print(f"📁 База данных: {DB_NAME}")
    print(f"👑 Админ ID: {ADMIN_ID}")
    print(f"🚫 Блокировка времени: активна")
    print("\n📱 Для админа доступна кнопка '👑 Админ-панель'")
    
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())