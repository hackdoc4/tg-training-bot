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
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application

logging.basicConfig(level=logging.INFO)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SHEET_CSV_URL = os.getenv("SHEET_CSV_URL", "")
USERS_CSV_URL = os.getenv("USERS_CSV_URL", "")
APPS_SCRIPT_URL = os.getenv("APPS_SCRIPT_URL", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

WEBHOOK_HOST = os.getenv("RENDER_EXTERNAL_URL", "").rstrip("/")
WEBHOOK_PATH = f"/webhook/{BOT_TOKEN}"
PORT = int(os.getenv("PORT", 8080))

CACHE_TTL = 60
_cache_data: List[Dict[str, str]] = []
_cache_time: float = 0.0

# Локальный кэш статусов пользователей, чтобы не ждать обновления CSV от Google после одобрения
_user_status_override: Dict[int, str] = {}


class RegState(StatesGroup):
    waiting_full_name = State()


class NavCallback(CallbackData, prefix="nav"):
    level: str        # "loc", "sec", "dev", "item"
    loc_idx: int = -1
    sec_idx: int = -1
    dev_idx: int = -1


class AdminCallback(CallbackData, prefix="adm"):
    decision: str     # "approve" or "reject"
    user_id: int


async def fetch_csv(url: str) -> List[Dict[str, str]]:
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=15) as resp:
            resp.raise_for_status()
            text = await resp.text(encoding="utf-8")

    reader = csv.DictReader(io.StringIO(text))
    rows: List[Dict[str, str]] = []
    for row in reader:
        cleaned = {
            (k or "").strip().lower(): (v or "").strip()
            for k, v in row.items()
        }
        rows.append(cleaned)
    return rows


async def get_rows(force: bool = False) -> List[Dict[str, str]]:
    global _cache_data, _cache_time
    now = time.time()
    if not force and _cache_data and (now - _cache_time < CACHE_TTL):
        return _cache_data

    raw_rows = await fetch_csv(SHEET_CSV_URL)
    rows = [
        r for r in raw_rows
        if r.get("location") and r.get("section") and r.get("device")
    ]
    _cache_data = rows
    _cache_time = now
    return rows


async def get_user_status(user_id: int) -> Optional[str]:
    if user_id == ADMIN_ID:
        return "approved"
    if user_id in _user_status_override:
        return _user_status_override[user_id]
    if not USERS_CSV_URL:
        return None

    try:
        users = await fetch_csv(USERS_CSV_URL)
        for u in users:
            if u.get("user_id") == str(user_id):
                status = u.get("status", "").lower()
                if status:
                    _user_status_override[user_id] = status
                    return status
    except Exception as e:
        logging.error(f"Ошибка чтения листа Users: {e}")
    return None


async def send_to_apps_script(payload: dict) -> bool:
    if not APPS_SCRIPT_URL:
        logging.error("Не задан APPS_SCRIPT_URL")
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(APPS_SCRIPT_URL, json=payload, timeout=15) as resp:
                return resp.status == 200
    except Exception as e:
        logging.error(f"Ошибка отправки в Apps Script: {e}")
        return False


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


dp = Dispatcher(storage=MemoryStorage())


async def ensure_approved(user_id: int, target: Message | CallbackQuery) -> bool:
    status = await get_user_status(user_id)
    if status == "approved":
        return True

    text = (
        "⏳ Твоя заявка ещё на рассмотрении у администратора."
        if status == "pending"
        else "🔒 У тебя нет доступа. Нажми /start для регистрации."
    )
    if isinstance(target, CallbackQuery):
        await target.answer(text, show_alert=True)
    else:
        await target.answer(text)
    return False


@dp.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext):
    await state.clear()
    user_id = message.from_user.id
    status = await get_user_status(user_id)

    if status == "approved":
        rows = await get_rows(force=True)
        if not rows:
            await message.answer("⚠️ Таблица пока пуста или недоступна.")
            return
        await message.answer(
            "Привет! 👋\nВыбери локацию, в которой ты работаешь:",
            reply_markup=build_locations_kb(rows)
        )
        return

    if status == "pending":
        await message.answer("⏳ Твоя заявка уже отправлена администратору. Как только её одобрят, я пришлю уведомление.")
        return

    if status == "rejected":
        await message.answer("❌ Твоя заявка была отклонена. Если это ошибка, свяжись с руководителем.")
        return

    await state.set_state(RegState.waiting_full_name)
    await message.answer(
        "Привет! 👋 Для доступа к учебным материалам нужно пройти короткую регистрацию.\n\n"
        "Напиши, пожалуйста, свои <b>Имя и Фамилию</b>:",
        parse_mode="HTML"
    )


@dp.message(RegState.waiting_full_name)
async def process_full_name(message: Message, state: FSMContext, bot: Bot):
    full_name = (message.text or "").strip()
    if len(full_name) < 3:
        await message.answer("Пожалуйста, введи корректные Имя и Фамилию (минимум 3 символа):")
        return

    user = message.from_user
    username = f"@{user.username}" if user.username else "без username"

    _user_status_override[user.id] = "pending"
    await send_to_apps_script({
        "action": "register",
        "user_id": user.id,
        "username": username,
        "full_name": full_name
    })
    await state.clear()

    await message.answer(
        "✅ Заявка отправлена администратору! Как только доступ откроют, я сразу напишу тебе сюда."
    )

    if ADMIN_ID:
        admin_kb = InlineKeyboardMarkup(inline_keyboard=[[
            InlineKeyboardButton(
                text="✅ Одобрить",
                callback_data=AdminCallback(decision="approve", user_id=user.id).pack()
            ),
            InlineKeyboardButton(
                text="❌ Отклонить",
                callback_data=AdminCallback(decision="reject", user_id=user.id).pack()
            )
        ]])
        await bot.send_message(
            ADMIN_ID,
            f"🆕 <b>Новая заявка на доступ к боту</b>\n\n"
            f"👤 <b>ФИО:</b> {full_name}\n"
            f"🔗 <b>Telegram:</b> {username}\n"
            f"🆔 <b>ID:</b> <code>{user.id}</code>",
            parse_mode="HTML",
            reply_markup=admin_kb
        )


@dp.callback_query(AdminCallback.filter())
async def handle_admin_decision(callback: CallbackQuery, callback_data: AdminCallback, bot: Bot):
    if callback.from_user.id != ADMIN_ID:
        await callback.answer("Недостаточно прав", show_alert=True)
        return

    target_user_id = callback_data.user_id
    new_status = "approved" if callback_data.decision == "approve" else "rejected"
    _user_status_override[target_user_id] = new_status

    await send_to_apps_script({
        "action": "set_status",
        "user_id": target_user_id,
        "status": new_status
    })

    status_label = "✅ ОДОБРЕНО" if new_status == "approved" else "❌ ОТКЛОНЕНО"
    await callback.message.edit_text(
        f"{callback.message.text}\n\n<b>Решение:</b> {status_label}",
        parse_mode="HTML"
    )
    await callback.answer(f"Статус обновлён: {status_label}")

    try:
        if new_status == "approved":
            start_kb = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text="🚀 Начать обучение", callback_data=NavCallback(level="loc").pack())
            ]])
            await bot.send_message(
                target_user_id,
                "🎉 Твой доступ одобрен! Нажми кнопку ниже или отправь /start, чтобы открыть материалы.",
                reply_markup=start_kb
            )
        else:
            await bot.send_message(
                target_user_id,
                "❌ Заявка на доступ к обучающему боту отклонена."
            )
    except Exception as e:
        logging.warning(f"Не удалось уведомить пользователя {target_user_id}: {e}")


@dp.callback_query(NavCallback.filter(F.level == "loc"))
async def handle_locations(callback: CallbackQuery):
    if not await ensure_approved(callback.from_user.id, callback):
        return
    rows = await get_rows()
    await callback.message.edit_text(
        "Выбери локацию, в которой ты работаешь:",
        reply_markup=build_locations_kb(rows)
    )
    await callback.answer()


@dp.callback_query(NavCallback.filter(F.level == "sec"))
async def handle_sections(callback: CallbackQuery, callback_data: NavCallback):
    if not await ensure_approved(callback.from_user.id, callback):
        return
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
    if not await ensure_approved(callback.from_user.id, callback):
        return
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
    if not await ensure_approved(callback.from_user.id, callback):
        return
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
        dp.startup.register(on_startup)
        app = web.Application()
        app.router.add_get("/", healthcheck)
        SimpleRequestHandler(dispatcher=dp, bot=bot).register(app, path=WEBHOOK_PATH)
        setup_application(app, dp, bot=bot)
        web.run_app(app, host="0.0.0.0", port=PORT)
    else:
        async def run_polling():
            await bot.delete_webhook(drop_pending_updates=True)
            await dp.start_polling(bot)

        asyncio.run(run_polling())


if __name__ == "__main__":
    main()
