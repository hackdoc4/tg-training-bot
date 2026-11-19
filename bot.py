import asyncio
import csv
import io
import logging
import os
import time
from typing import Dict, List, Optional

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.filters.callback_data import CallbackData
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

# Переменные окружения (можно задать в системе или в настройках Render)
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SHEET_CSV_URL = os.getenv("SHEET_CSV_URL", "")
# Для Render (оставь пустым при локальном запуске)
WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
PORT = int(os.getenv("PORT", 8080))

# Кэш таблицы в памяти (обновляется раз в 60 секунд)
CACHE_TTL = 60
_cache_data: List[Dict[str, str]] = []
_cache_time: float = 0.0


class NavCallback(CallbackData, prefix="nav"):
    level: str        # "loc", "sec", "dev"
    loc_idx: int = -1
    sec_idx: int = -1
    dev_idx: int = -1


async def get_rows(force: bool = False) -> List[Dict[str, str]]:
    global _cache_data, _cache_time
    now = time.time()
    if not force and _cache_data and (now - _cache_time < CACHE_TTL):
        return _cache_data

    async with aiohttp.ClientSession() as session:
        async with session.get(SHEET_CSV_URL, timeout=15) as resp:
            resp.raise_for_status()
            text = await resp.text(encoding="utf-8")

    reader = csv.DictReader(io.StringIO(text))
    rows: List[Dict[str, str]] = []
    for row in reader:
        cleaned = {
            (k or "").strip().lower(): (v or "").strip()
            for k, v in row.items()
        }
        if cleaned.get("location") and cleaned.get("section") and cleaned.get("device"):
            rows.append(cleaned)

    _cache_data = rows
    _cache_time = now
    return rows


def unique_ordered(items: List[str]) -> List[str]:
    seen = set()
    result = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def build_locations_kb(rows: List[Dict[str, str]]) -> InlineKeyboardMarkup:
    locations = unique_ordered([r["location"] for r in rows])
    buttons = [
        [InlineKeyboardButton(
            text=f"🌍 {loc}",
            callback_data=NavCallback(level="sec", loc_idx=i).pack()
        )]
        for i, loc in enumerate(locations)
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_sections_kb(rows: List[Dict[str, str]], loc_idx: int) -> Optional[InlineKeyboardMarkup]:
    locations = unique_ordered([r["location"] for r in rows])
    if loc_idx < 0 or loc_idx >= len(locations):
        return None
    loc = locations[loc_idx]

    sections = unique_ordered([r["section"] for r in rows if r["location"] == loc])
    buttons = [
        [InlineKeyboardButton(
            text=f"📂 {sec}",
            callback_data=NavCallback(level="dev", loc_idx=loc_idx, sec_idx=i).pack()
        )]
        for i, sec in enumerate(sections)
    ]
    buttons.append([
        InlineKeyboardButton(text="⬅️ Выбрать другую страну", callback_data=NavCallback(level="loc").pack())
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def build_devices_kb(rows: List[Dict[str, str]], loc_idx: int, sec_idx: int) -> Optional[InlineKeyboardMarkup]:
    locations = unique_ordered([r["location"] for r in rows])
    if loc_idx < 0 or loc_idx >= len(locations):
        return None
    loc = locations[loc_idx]

    sections = unique_ordered([r["section"] for r in rows if r["location"] == loc])
    if sec_idx < 0 or sec_idx >= len(sections):
        return None
    sec = sections[sec_idx]

    devices = unique_ordered([
        r["device"] for r in rows
        if r["location"] == loc and r["section"] == sec
    ])
    buttons = [
        [InlineKeyboardButton(
            text=f"📱 {dev}",
            callback_data=NavCallback(level="item", loc_idx=loc_idx, sec_idx=sec_idx, dev_idx=i).pack()
        )]
        for i, dev in enumerate(devices)
    ]
    buttons.append([
        InlineKeyboardButton(text="⬅️ Назад к разделам", callback_data=NavCallback(level="sec", loc_idx=loc_idx).pack())
    ])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


dp = Dispatcher()


@dp.message(CommandStart())
async def cmd_start(message: Message):
    rows = await get_rows(force=True)
    if not rows:
        await message.answer("⚠️ Таблица пока пуста или недоступна. Проверь ссылку на CSV.")
        return

    await message.answer(
        "Привет! 👋\nВыбери локацию, в которой ты работаешь:",
        reply_markup=build_locations_kb(rows)
    )


@dp.callback_query(NavCallback.filter(F.level == "loc"))
async def handle_locations(callback: CallbackQuery):
    rows = await get_rows()
    await callback.message.edit_text(
        "Выбери локацию, в которой ты работаешь:",
        reply_markup=build_locations_kb(rows)
    )
    await callback.answer()


@dp.callback_query(NavCallback.filter(F.level == "sec"))
async def handle_sections(callback: CallbackQuery, callback_data: NavCallback):
    rows = await get_rows()
    locations = unique_ordered([r["location"] for r in rows])
    kb = build_sections_kb(rows, callback_data.loc_idx)
    if not kb:
        await callback.answer("Данные обновились, нажми /start", show_alert=True)
        return

    loc = locations[callback_data.loc_idx]
    await callback.message.edit_text(
        f"Локация: <b>{loc}</b>\nЧто ты хочешь сегодня узнать?",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(NavCallback.filter(F.level == "dev"))
async def handle_devices(callback: CallbackQuery, callback_data: NavCallback):
    rows = await get_rows()
    locations = unique_ordered([r["location"] for r in rows])
    if callback_data.loc_idx >= len(locations):
        await callback.answer("Данные обновились, нажми /start", show_alert=True)
        return
    loc = locations[callback_data.loc_idx]

    sections = unique_ordered([r["section"] for r in rows if r["location"] == loc])
    kb = build_devices_kb(rows, callback_data.loc_idx, callback_data.sec_idx)
    if not kb:
        await callback.answer("Данные обновились, нажми /start", show_alert=True)
        return

    sec = sections[callback_data.sec_idx]
    await callback.message.edit_text(
        f"Локация: <b>{loc}</b> → Раздел: <b>{sec}</b>\nС каким устройством хочешь ознакомиться?",
        parse_mode="HTML",
        reply_markup=kb
    )
    await callback.answer()


@dp.callback_query(NavCallback.filter(F.level == "item"))
async def handle_item(callback: CallbackQuery, callback_data: NavCallback):
    rows = await get_rows()
    locations = unique_ordered([r["location"] for r in rows])
    if callback_data.loc_idx >= len(locations):
        await callback.answer("Данные обновились, нажми /start", show_alert=True)
        return
    loc = locations[callback_data.loc_idx]

    sections = unique_ordered([r["section"] for r in rows if r["location"] == loc])
    if callback_data.sec_idx >= len(sections):
        await callback.answer("Данные обновились, нажми /start", show_alert=True)
        return
    sec = sections[callback_data.sec_idx]

    devices = unique_ordered([r["device"] for r in rows if r["location"] == loc and r["section"] == sec])
    if callback_data.dev_idx >= len(devices):
        await callback.answer("Данные обновились, нажми /start", show_alert=True)
        return
    dev = devices[callback_data.dev_idx]

    item = next(
        (r for r in rows if r["location"] == loc and r["section"] == sec and r["device"] == dev),
        None
    )
    if not item:
        await callback.answer("Запись не найдена", show_alert=True)
        return

    lines = [
        f"📍 <b>{loc}</b> | 📂 <b>{sec}</b> | 📱 <b>{dev}</b>",
        ""
    ]
    if item.get("video_url"):
        lines.append(f"🎬 <b>Видеоинструкция:</b>\n{item['video_url']}")
        lines.append("")
    if item.get("doc_url"):
        lines.append(f"📄 <b>Текстовая инструкция:</b>\n{item['doc_url']}")

    if not item.get("video_url") and not item.get("doc_url"):
        lines.append("Материалы для этого устройства пока не добавлены.")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(
            text="⬅️ Назад к устройствам",
            callback_data=NavCallback(level="dev", loc_idx=callback_data.loc_idx, sec_idx=callback_data.sec_idx).pack()
        )],
        [InlineKeyboardButton(
            text="🏠 В самое начало",
            callback_data=NavCallback(level="loc").pack()
        )]
    ])

    await callback.message.edit_text(
        "\n".join(lines),
        parse_mode="HTML",
        disable_web_page_preview=False,
        reply_markup=kb
    )
    await callback.answer()


async def on_startup(bot: Bot):
    if WEBHOOK_HOST:
        await bot.set_webhook(f"{WEBHOOK_HOST}{WEBHOOK_PATH}")
        logging.info(f"Webhook установлен: {WEBHOOK_HOST}{WEBHOOK_PATH}")


async def healthcheck(request: web.Request):
    return web.Response(text="OK")


def main():
    bot = Bot(token=BOT_TOKEN)

    if WEBHOOK_HOST:
        # Режим для Render (Webhook + Web Service)
        dp.startup.register(on_startup)
        app = web.Application()
        app.router.add_get("/", healthcheck)
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        web.run_app(app, host="0.0.0.0", port=PORT)
    else:
        # Локальный режим на ПК (Polling)
        async def run_polling():
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)

        asyncio.run(run_polling())


if __name__ == "__main__":
    main()