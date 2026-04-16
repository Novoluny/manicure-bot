import os
import asyncio
import aiosqlite

DB_NAME = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bookings.db")

async def update_db():
    async with aiosqlite.connect(DB_NAME) as db:
        try:
            await db.execute('ALTER TABLE blocked_slots ADD COLUMN is_full_day INTEGER DEFAULT 0')
            print("✅ Добавлена колонка is_full_day")
        except:
            print("⚠️ Колонка is_full_day уже существует")
        
        await db.commit()
        print("✅ База данных обновлена!")

if __name__ == "__main__":
    asyncio.run(update_db())