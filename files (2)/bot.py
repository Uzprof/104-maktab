import asyncio
import csv
import html
import io
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone

import aiohttp
import aiosqlite
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (BufferedInputFile, CallbackQuery, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from dotenv import load_dotenv

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNERS = {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()}
ADMINS: set[int] = set(OWNERS)       # bosh adminlar + bazadagi adminlar
REGISTERED: set[int] = set()         # ro'yxatdan o'tganlar
BLOCKED: set[int] = set()            # bloklanganlar
SCHOOL = os.getenv("SCHOOL_NAME", "Maktab")
NATIVE = os.getenv("NATIVE_COLORS", "1") == "1"
NEWS_CHANNEL = os.getenv("NEWS_CHANNEL", "qwe1234567123")
GEMINI_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-flash-latest")
DB = "school.db"
TZ = timezone(timedelta(hours=5))    # Toshkent vaqti
DAYS = ["Dushanba", "Seshanba", "Chorshanba", "Payshanba", "Juma", "Shanba"]
ROLES = {"student": "🎓 O'quvchi", "teacher": "👨‍🏫 O'qituvchi", "parent": "👪 Ota-ona"}
AI_SYSTEM = (f"Sen {SCHOOL} o'quvchilari uchun yordamchisan. O'zbek tilida, sodda va do'stona javob ber. "
             "Uy vazifasini shunchaki tayyor yechib berma, tushuntirib ber.")
esc = html.escape

# ---------- Rangli tugmalar va premium emoji ----------
STYLE = {"blue": "primary", "green": "success", "red": "danger"}
EMOJI = {"blue": "🔵", "green": "🟢", "red": "🔴"}
COLOR_NAMES = {"blue": "🔵 Ko'k", "green": "🟢 Yashil", "red": "🔴 Qizil", "none": "⚪ Oddiy"}

# Asosiy menyu tugmalari (admin panelda matn, rang, emoji, ko'rinishini o'zgartirish mumkin)
MENU = {
    "sch": {"label": "Dars jadvali", "emoji": "📅", "cb": "sch", "color": "blue"},
    "tdy": {"label": "Bugungi darslar", "emoji": "🗓", "cb": "tdy", "color": "green"},
    "hw": {"label": "Uy vazifalari", "emoji": "📚", "cb": "hw", "color": "blue"},
    "news": {"label": "Yangiliklar", "emoji": "📰", "cb": "news", "color": "green"},
    "ai": {"label": "AI yordamchi", "emoji": "🤖", "cb": "ai", "color": "blue"},
    "contact": {"label": "Aloqa / Murojaat", "emoji": "📞", "cb": "contact", "color": "green"},
    "profile": {"label": "Profil", "emoji": "👤", "cb": "profile", "color": "none"},
    "adm": {"label": "Admin panel", "emoji": "⚙️", "cb": "adm", "color": "red"},
}
MENU_ORDER = ["sch", "tdy", "hw", "news", "ai", "contact", "profile"]
GROUP_KEYS = {"class": "🏫 Sinf tugmalari", "day": "📆 Kun tugmalari",
              "panel": "🛠 Admin panel tugmalari", "back": "⬅️ Orqaga tugmalari"}
DEFAULT_COLORS = {k: v["color"] for k, v in MENU.items()}
DEFAULT_COLORS.update({"class": "blue", "day": "green", "panel": "green", "back": "red"})
COLORS = dict(DEFAULT_COLORS)
LABELS: dict[str, str] = {}
UEMOJI: dict[str, str] = {}   # oddiy emoji (yoki premium emoji uchun zaxira)
CEMOJI: dict[str, str] = {}   # premium (custom) emoji ID
HIDDEN: set[str] = set()


def btn(text: str, data: str, key: str | None = None, suffix: str = "") -> InlineKeyboardButton:
    """key — menyu tugmasi (MENU), guruh (GROUP_KEYS) yoki to'g'ridan-to'g'ri rang (blue/green/red)."""
    kw = {"callback_data": data}
    color = key if key in STYLE else COLORS.get(key)
    if key in MENU:
        label = LABELS.get(key, MENU[key]["label"])
        u = UEMOJI.get(key, MENU[key]["emoji"])
        cid = CEMOJI.get(key)
        text = label if cid else (f"{u} {label}" if u else label)
        if cid:
            kw["icon_custom_emoji_id"] = cid   # premium emoji ikonka
    text += suffix
    if color in STYLE:
        if NATIVE:
            kw["style"] = STYLE[color]         # Bot API 9.4+ rangli tugma
        else:
            text = f"{EMOJI[color]} {text}"
    return InlineKeyboardButton(text=text, **kw)


def urlbtn(text: str, url: str) -> InlineKeyboardButton:
    kw = {"style": "primary"} if NATIVE else {}
    return InlineKeyboardButton(text=text, url=url, **kw)


def kb(rows) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back(to="home", text="⬅️ Orqaga"):
    return [btn(text, to, "back")]


TG_EMOJI_RE = re.compile(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", re.S)


def strip_icons(markup):
    if not markup:
        return markup
    return InlineKeyboardMarkup(inline_keyboard=[
        [b.model_copy(update={"icon_custom_emoji_id": None}) for b in row] for row in markup.inline_keyboard])


async def say(m: Message, text: str, markup=None, **kw):
    """Xabar yuborish. Premium emoji ruxsat etilmasa, oddiy emoji bilan qayta urinadi."""
    try:
        return await m.answer(text, reply_markup=markup, **kw)
    except TelegramBadRequest:
        return await m.answer(TG_EMOJI_RE.sub(r"\1", text), reply_markup=strip_icons(markup), **kw)


async def edit(c: CallbackQuery, text: str, markup=None):
    try:
        await c.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest as e:
        if "not modified" in str(e):
            return
        try:
            await c.message.edit_text(TG_EMOJI_RE.sub(r"\1", text), reply_markup=strip_icons(markup))
        except TelegramBadRequest:
            pass


async def send_to(bot: Bot, uid: int, text: str, markup=None):
    try:
        return await bot.send_message(uid, text, reply_markup=markup)
    except TelegramBadRequest:
        return await bot.send_message(uid, TG_EMOJI_RE.sub(r"\1", text), reply_markup=strip_icons(markup))


# ---------- Baza ----------
async def q(sql, args=(), one=False, commit=False):
    async with aiosqlite.connect(DB) as db:
        cur = await db.execute(sql, args)
        if commit:
            await db.commit()
            return None
        rows = await cur.fetchall()
        return (rows[0] if rows else None) if one else rows


async def scalar(sql, args=()):
    r = await q(sql, args, one=True)
    return r[0] if r else 0


async def put(key, value):
    await q("INSERT OR REPLACE INTO settings VALUES(?,?)", (key, value), commit=True)


async def drop(*keys):
    for k in keys:
        await q("DELETE FROM settings WHERE key=?", (k,), commit=True)


async def get_setting(key, default=""):
    r = await q("SELECT value FROM settings WHERE key=?", (key,), one=True)
    return r[0] if r else default


async def incr(key):
    await q("INSERT INTO settings VALUES(?, '1') ON CONFLICT(key) DO UPDATE "
            "SET value=CAST(value AS INTEGER)+1", (key,), commit=True)


async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.executescript("""
        CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, name TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS admins(id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE IF NOT EXISTS classes(name TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS schedule(class TEXT, day INTEGER, text TEXT, PRIMARY KEY(class, day));
        CREATE TABLE IF NOT EXISTS homework(id INTEGER PRIMARY KEY AUTOINCREMENT, class TEXT, text TEXT, created TEXT);
        CREATE TABLE IF NOT EXISTS channel_news(chat_id INTEGER, message_id INTEGER, PRIMARY KEY(chat_id, message_id));
        CREATE TABLE IF NOT EXISTS sub_channels(chat_id INTEGER PRIMARY KEY, title TEXT, link TEXT);
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
        INSERT OR IGNORE INTO classes SELECT DISTINCT class FROM schedule;
        """)
        for col in ("created TEXT", "first_name TEXT", "last_name TEXT", "role TEXT",
                    "class TEXT", "blocked INTEGER DEFAULT 0"):
            try:
                await db.execute(f"ALTER TABLE users ADD COLUMN {col}")
            except Exception:
                pass
        await db.commit()
    ADMINS.update(r[0] for r in await q("SELECT id FROM admins"))
    REGISTERED.update(r[0] for r in await q("SELECT id FROM users WHERE COALESCE(first_name,'')<>''"))
    BLOCKED.update(r[0] for r in await q("SELECT id FROM users WHERE blocked=1"))
    for k, v in await q("SELECT key, value FROM settings"):
        if k.startswith("color:"):
            COLORS[k[6:]] = v
        elif k.startswith("label:"):
            LABELS[k[6:]] = v
        elif k.startswith("uemoji:"):
            UEMOJI[k[7:]] = v
        elif k.startswith("cemoji:"):
            CEMOJI[k[7:]] = v
        elif k.startswith("hidden:"):
            HIDDEN.add(k[7:])


def class_key(n: str):
    m = re.match(r"\d+", n)
    return (int(m.group()) if m else 999, n)


async def get_classes() -> list[str]:
    return sorted([r[0] for r in await q("SELECT name FROM classes")], key=class_key)


def class_buttons(classes, prefix, key="class"):
    b = InlineKeyboardBuilder()
    for cl in classes:
        b.add(btn(cl, f"{prefix}:{cl}", key))
    b.adjust(3)
    return b


async def get_user(uid: int) -> dict:
    r = await q("SELECT first_name, last_name, role, class, created, blocked, name FROM users WHERE id=?",
                (uid,), one=True)
    if not r:
        return {}
    return dict(zip(("first", "last", "role", "class", "created", "blocked", "tg"), r))


def fullname(u: dict) -> str:
    return (f"{u.get('first') or ''} {u.get('last') or ''}".strip()) or (u.get("tg") or "—")


def fmt_date(s: str | None) -> str:
    return f"{s[8:10]}.{s[5:7]}.{s[:4]}" if s and len(s) >= 10 else "—"


# ---------- Majburiy obuna ----------
SUB_CACHE: dict[int, float] = {}
SUB_TTL = 120
SUB_TEXT = "📢 Botdan foydalanish uchun quyidagi kanallarga obuna bo'ling, so'ng «Tekshirish» tugmasini bosing:"


async def missing_subs(bot: Bot, uid: int):
    if uid in ADMINS or time.time() - SUB_CACHE.get(uid, 0) < SUB_TTL:
        return []
    missing = []
    for chat_id, title, link in await q("SELECT chat_id, title, link FROM sub_channels ORDER BY rowid"):
        try:
            mem = await bot.get_chat_member(chat_id, uid)
            ok = mem.status in ("creator", "administrator", "member") or \
                (mem.status == "restricted" and getattr(mem, "is_member", False))
        except Exception:
            ok = True  # bot kanalda yo'q/xato bo'lsa, foydalanuvchini to'smaymiz
        if not ok:
            missing.append((chat_id, title, link))
    if not missing:
        SUB_CACHE[uid] = time.time()
    return missing


def sub_markup(missing):
    rows = [[urlbtn(f"📢 {t}", link)] for _, t, link in missing]
    rows.append([btn("✅ Tekshirish", "chk_sub", "green")])
    return kb(rows)


# ---------- Routerlar ----------
cmd_r = Router()      # /start, /cancel, obuna tekshiruvi — har qanday holatda ishlaydi
reg_r = Router()      # ro'yxatdan o'tish
channel_r = Router()  # kanal postlari
admin_r = Router()
user_r = Router()


async def is_admin(event) -> bool:
    return bool(event.from_user) and event.from_user.id in ADMINS


admin_r.message.filter(is_admin)
admin_r.callback_query.filter(is_admin)


async def deny(event, text):
    if isinstance(event, CallbackQuery):
        await event.answer(text, show_alert=True)
    else:
        await event.answer(text)


class Guard(BaseMiddleware):
    """Bloklangan / obuna bo'lmagan / ro'yxatdan o'tmagan foydalanuvchilarni to'xtatadi."""

    async def __call__(self, handler, event, data):
        uid = event.from_user.id
        if uid in BLOCKED:
            return await deny(event, "🚫 Siz botdan foydalanishdan cheklangansiz.")
        missing = await missing_subs(data["bot"], uid)
        if missing:
            if isinstance(event, CallbackQuery):
                await event.answer("📢 Avval kanallarga obuna bo'ling", show_alert=True)
                return await say(event.message, SUB_TEXT, sub_markup(missing))
            return await say(event, SUB_TEXT, sub_markup(missing))
        if uid not in REGISTERED:
            return await deny(event, "Avval /start bosib ro'yxatdan o'ting.")
        return await handler(event, data)


user_r.message.middleware(Guard())
user_r.callback_query.middleware(Guard())


def main_menu(uid: int) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    for k in MENU_ORDER:
        if k not in HIDDEN:
            b.add(btn("", MENU[k]["cb"], k))
    b.adjust(2)
    if uid in ADMINS:
        b.row(btn("", "adm", "adm"))
    return b.as_markup()


# ---------- /start va ro'yxatdan o'tish ----------
class Reg(StatesGroup):
    first = State()
    last = State()
    role = State()
    cls = State()


def valid_name(t: str) -> bool:
    t = t.strip()
    return 2 <= len(t) <= 30 and not any(ch.isdigit() for ch in t) and not t.startswith("/")


def cap(t: str) -> str:
    t = t.strip()
    return t[0].upper() + t[1:]


async def begin_registration(m: Message, state: FSMContext):
    await state.set_state(Reg.first)
    await say(m, f"👋 Assalomu alaykum! <b>{esc(SCHOOL)}</b> rasmiy botiga xush kelibsiz.\n\n"
                 "Ro'yxatdan o'tish uchun <b>ismingizni</b> yozing:")


@cmd_r.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    uid = m.from_user.id
    if uid in BLOCKED:
        return await m.answer("🚫 Siz botdan foydalanishdan cheklangansiz.")
    await q("INSERT OR IGNORE INTO users(id, name, created, blocked) VALUES(?,?,datetime('now','+5 hours'),0)",
            (uid, m.from_user.full_name), commit=True)
    await q("UPDATE users SET name=? WHERE id=?", (m.from_user.full_name, uid), commit=True)
    missing = await missing_subs(m.bot, uid)
    if missing:
        return await say(m, SUB_TEXT, sub_markup(missing))
    if uid in REGISTERED:
        u = await get_user(uid)
        return await say(m, f"👋 Assalomu alaykum, <b>{esc(u.get('first') or '')}</b>!\n"
                            f"🏫 <b>{esc(SCHOOL)}</b> rasmiy boti.", main_menu(uid))
    await begin_registration(m, state)


@cmd_r.message(Command("cancel"))
async def cancel(m: Message, state: FSMContext):
    await state.clear()
    if m.from_user.id in REGISTERED:
        await say(m, "Bekor qilindi.", main_menu(m.from_user.id))
    else:
        await m.answer("Bekor qilindi. Qaytadan boshlash uchun /start bosing.")


@cmd_r.callback_query(F.data == "chk_sub")
async def chk_sub(c: CallbackQuery, state: FSMContext):
    uid = c.from_user.id
    SUB_CACHE.pop(uid, None)
    missing = await missing_subs(c.bot, uid)
    if missing:
        await c.answer("❌ Hali hamma kanalga obuna bo'lmadingiz", show_alert=True)
        return await edit(c, SUB_TEXT, sub_markup(missing))
    await c.answer("✅ Rahmat!")
    if uid in REGISTERED:
        await edit(c, "🏫 Asosiy menyu:", main_menu(uid))
    else:
        await begin_registration(c.message, state)


@cmd_r.callback_query(F.data == "noop")
async def noop(c: CallbackQuery):
    await c.answer("Bu tugmaning ko'rinishi")


@reg_r.message(Reg.first, F.text)
async def reg_first(m: Message, state: FSMContext):
    if not valid_name(m.text):
        return await m.answer("Iltimos, ismingizni to'g'ri yozing (faqat harflar, 2–30 belgi):")
    await state.update_data(first=cap(m.text))
    await state.set_state(Reg.last)
    await m.answer("Endi <b>familyangizni</b> yozing:")


@reg_r.message(Reg.last, F.text)
async def reg_last(m: Message, state: FSMContext):
    if not valid_name(m.text):
        return await m.answer("Iltimos, familyangizni to'g'ri yozing (faqat harflar, 2–30 belgi):")
    await state.update_data(last=cap(m.text))
    await state.set_state(Reg.role)
    await m.answer("Siz kimsiz?", reply_markup=kb([
        [btn("🎓 O'quvchi", "rr:student", "blue")],
        [btn("👨‍🏫 O'qituvchi", "rr:teacher", "green")],
        [btn("👪 Ota-ona", "rr:parent", "blue")],
    ]))


@reg_r.message(Reg.role)
@reg_r.message(Reg.cls)
async def reg_wait_button(m: Message):
    await m.answer("Iltimos, yuqoridagi tugmalardan birini tanlang.")


@reg_r.callback_query(Reg.role, F.data.startswith("rr:"))
async def reg_role(c: CallbackQuery, state: FSMContext):
    role = c.data[3:]
    if role not in ROLES:
        return
    await state.update_data(role=role)
    classes = await get_classes()
    if role == "teacher" or not classes:
        return await finish_reg(c, state, None)
    await state.set_state(Reg.cls)
    b = class_buttons(classes, "rc")
    b.row(btn("O'tkazib yuborish", "rc_skip"))
    await edit(c, "Sinfingizni tanlang:" if role == "student" else "Farzandingiz sinfini tanlang:", b.as_markup())


@reg_r.callback_query(Reg.cls, F.data.startswith("rc"))
async def reg_cls(c: CallbackQuery, state: FSMContext):
    await finish_reg(c, state, None if c.data == "rc_skip" else c.data[3:])


async def finish_reg(c: CallbackQuery, state: FSMContext, cl):
    d = await state.get_data()
    uid = c.from_user.id
    await q("UPDATE users SET first_name=?, last_name=?, role=?, class=? WHERE id=?",
            (d["first"], d["last"], d["role"], cl, uid), commit=True)
    REGISTERED.add(uid)
    await state.clear()
    await edit(c, f"✅ Ro'yxatdan muvaffaqiyatli o'tdingiz, <b>{esc(d['first'])} {esc(d['last'])}</b>!")
    await say(c.message, "🏫 Asosiy menyu:", main_menu(uid))


# ---------- Asosiy menyu, profil ----------
@user_r.callback_query(F.data == "home")
async def home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(c, "🏫 Asosiy menyu:", main_menu(c.from_user.id))


@user_r.callback_query(F.data == "profile")
async def profile(c: CallbackQuery):
    u = await get_user(c.from_user.id)
    await edit(c, "👤 <b>Profil</b>\n\n"
                  f"Ism: <b>{esc(u.get('first') or '—')}</b>\n"
                  f"Familya: <b>{esc(u.get('last') or '—')}</b>\n"
                  f"Rol: <b>{ROLES.get(u.get('role'), '—')}</b>\n"
                  f"Sinf: <b>{esc(u.get('class') or '—')}</b>\n"
                  f"Ro'yxatdan o'tgan sana: <b>{fmt_date(u.get('created'))}</b>",
         kb([[btn("✏️ Ma'lumotlarni o'zgartirish", "reg_again", "green")], back()]))


@user_r.callback_query(F.data == "reg_again")
async def reg_again(c: CallbackQuery, state: FSMContext):
    await c.answer()
    await begin_registration(c.message, state)


# ---------- Dars jadvali ----------
async def day_text(cl: str, i: int) -> str:
    r = await q("SELECT text FROM schedule WHERE class=? AND day=?", (cl, i), one=True)
    body = r[0] if r else "Bu kun uchun jadval kiritilmagan."
    return f"📅 <b>{esc(cl)} — {DAYS[i]}</b>\n\n{body}"


@user_r.callback_query(F.data == "sch")
async def sch(c: CallbackQuery):
    classes = await get_classes()
    if not classes:
        return await edit(c, "Hozircha sinflar kiritilmagan.", kb([back()]))
    b = class_buttons(classes, "sc")
    b.row(*back())
    await edit(c, "Sinfingizni tanlang:", b.as_markup())


@user_r.callback_query(F.data.startswith("sc:"))
async def sc(c: CallbackQuery):
    cl = c.data[3:]
    b = InlineKeyboardBuilder()
    for i, d in enumerate(DAYS):
        b.add(btn(d, f"sd:{cl}:{i}", "day"))
    b.adjust(2)
    b.row(*back("sch"))
    await edit(c, f"<b>{esc(cl)}</b> sinf. Kunni tanlang:", b.as_markup())


@user_r.callback_query(F.data.startswith("sd:"))
async def sd(c: CallbackQuery):
    _, cl, i = c.data.split(":")
    await edit(c, await day_text(cl, int(i)), kb([back(f"sc:{cl}")]))


async def my_day(c: CallbackQuery, i: int, note: str = ""):
    cl = (await get_user(c.from_user.id)).get("class")
    if not cl:
        return await edit(c, "Sinfingiz tanlanmagan. Profil orqali sinfingizni kiriting "
                             "yoki jadvalni umumiy bo'limdan ko'ring.",
                          kb([[btn("", "sch", "sch")], [btn("", "profile", "profile")], back()]))
    await edit(c, note + await day_text(cl, i), kb([
        [btn("◀️", f"td:{(i - 1) % 6}", "day"), btn("▶️", f"td:{(i + 1) % 6}", "day")], back()]))


@user_r.callback_query(F.data == "tdy")
async def tdy(c: CallbackQuery):
    wd = datetime.now(TZ).weekday()
    note = "🌴 Bugun yakshanba — dam olish kuni. Dushanba darslari:\n\n" if wd == 6 else ""
    await my_day(c, wd if wd < 6 else 0, note)


@user_r.callback_query(F.data.startswith("td:"))
async def td(c: CallbackQuery):
    await my_day(c, int(c.data[3:]) % 6)


# ---------- Uy vazifalari ----------
async def show_hw(c: CallbackQuery, cl: str):
    rows = await q("SELECT text, created FROM homework WHERE class=? ORDER BY id DESC LIMIT 3", (cl,))
    body = "\n\n".join(f"📌 <i>{fmt_date(d)}</i>\n{t}" for t, d in rows) if rows else "Hozircha uy vazifasi yo'q."
    await edit(c, f"📚 <b>{esc(cl)} — uy vazifalari</b>\n\n{body}",
               kb([[btn("🏫 Boshqa sinf", "hws", "class")], back()]))


@user_r.callback_query(F.data == "hw")
async def hw(c: CallbackQuery):
    cl = (await get_user(c.from_user.id)).get("class")
    if cl:
        return await show_hw(c, cl)
    await hws(c)


@user_r.callback_query(F.data == "hws")
async def hws(c: CallbackQuery):
    classes = await get_classes()
    if not classes:
        return await edit(c, "Hozircha sinflar kiritilmagan.", kb([back()]))
    b = class_buttons(classes, "hv")
    b.row(*back())
    await edit(c, "Qaysi sinf uy vazifasini ko'rasiz?", b.as_markup())


@user_r.callback_query(F.data.startswith("hv:"))
async def hv(c: CallbackQuery):
    await show_hw(c, c.data[3:])


# ---------- Yangiliklar (kanaldan) ----------
def is_news_channel(m: Message) -> bool:
    ch = NEWS_CHANNEL.lstrip("@").lower()
    return (m.chat.username or "").lower() == ch or str(m.chat.id) == ch


BASE = "blocked=0 AND COALESCE(first_name,'')<>''"


async def target_ids(t: str) -> list[int]:
    if t == "all":
        rows = await q(f"SELECT id FROM users WHERE {BASE}")
    elif t.startswith("r:"):
        rows = await q(f"SELECT id FROM users WHERE {BASE} AND role=?", (t[2:],))
    elif t.startswith("c:"):
        rows = await q(f"SELECT id FROM users WHERE {BASE} AND class=?", (t[2:],))
    else:
        rows = []
    return [r[0] for r in rows]


def target_label(t: str) -> str:
    if t == "all":
        return "hammaga"
    if t.startswith("r:"):
        return {"student": "o'quvchilarga", "teacher": "o'qituvchilarga", "parent": "ota-onalarga"}.get(t[2:], t)
    return f"{t[2:]} sinfga"


async def broadcast(bot: Bot, from_chat: int, message_id: int, t: str = "all") -> tuple[int, int]:
    ok = fail = 0
    for uid in await target_ids(t):
        try:
            await bot.copy_message(uid, from_chat, message_id)
            ok += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.05)
    return ok, fail


async def notify_class(bot: Bot, cl: str, text: str):
    for uid in await target_ids(f"c:{cl}"):
        try:
            await send_to(bot, uid, text)
        except Exception:
            pass
        await asyncio.sleep(0.05)


@channel_r.channel_post()
async def on_channel_post(m: Message):
    if not is_news_channel(m):
        return
    await q("INSERT OR IGNORE INTO channel_news VALUES(?,?)", (m.chat.id, m.message_id), commit=True)
    asyncio.create_task(broadcast(m.bot, m.chat.id, m.message_id))


@user_r.callback_query(F.data == "news")
async def news(c: CallbackQuery):
    rows = await q("SELECT chat_id, message_id FROM channel_news ORDER BY rowid DESC LIMIT 5")
    if not rows:
        return await edit(c, "Hozircha yangiliklar yo'q.", kb([back()]))
    await edit(c, "📰 So'nggi yangiliklar:")
    for chat_id, mid in reversed(rows):
        try:
            await c.bot.copy_message(c.from_user.id, chat_id, mid)
        except Exception:
            await q("DELETE FROM channel_news WHERE chat_id=? AND message_id=?", (chat_id, mid), commit=True)
    await c.message.answer("👆 Barcha yangiliklar kanalimizda ham bor.", reply_markup=kb([back()]))


# ---------- Aloqa va murojaat ----------
class Contact(StatesGroup):
    text = State()


@user_r.callback_query(F.data == "contact")
async def contact(c: CallbackQuery):
    info = await get_setting("info", "Maktab haqida ma'lumot hali kiritilmagan.")
    await edit(c, f"📞 <b>Aloqa</b>\n\n{info}", kb([[btn("✉️ Murojaat yuborish", "msg", "green")], back()]))


@user_r.callback_query(F.data == "msg")
async def msg_on(c: CallbackQuery, state: FSMContext):
    await state.set_state(Contact.text)
    await edit(c, "✉️ Murojaatingizni yozing — u adminlarga yuboriladi.", kb([back("contact", "Bekor qilish")]))


@user_r.message(Contact.text, F.text)
async def msg_send(m: Message, state: FSMContext):
    u = await get_user(m.from_user.id)
    who = f"{esc(fullname(u))} ({ROLES.get(u.get('role'), '—')}" + (f", {esc(u['class'])}" if u.get("class") else "") + ")"
    head = f"📩 <b>Yangi murojaat</b>\n👤 {who}\n🆔 <code>{m.from_user.id}</code>\n\n"
    markup = kb([[btn("↩️ Javob berish", f"rp:{m.from_user.id}", "green")]])
    for a in ADMINS:
        try:
            await send_to(m.bot, a, head + m.html_text, markup)
        except Exception:
            pass
    await incr("msg_count")
    await state.clear()
    await say(m, "✅ Murojaatingiz adminlarga yuborildi. Javob shu yerga keladi.", main_menu(m.from_user.id))


# ---------- AI yordamchi (Gemini) ----------
class AI(StatesGroup):
    chat = State()


async def gemini(hist: list[dict], extra: str = "") -> str:
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"
    body = {
        "system_instruction": {"parts": [{"text": AI_SYSTEM + extra}]},
        "contents": [{"role": h["role"], "parts": [{"text": h["text"]}]} for h in hist],
    }
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
        async with s.post(url, json=body, headers={"x-goog-api-key": GEMINI_KEY}) as r:
            data = await r.json()
            if r.status != 200:
                raise RuntimeError(f"Gemini {r.status}: {data}")
    parts = data["candidates"][0]["content"]["parts"]
    return "".join(p.get("text", "") for p in parts).strip() or "Javob topilmadi."


@user_r.callback_query(F.data == "ai")
async def ai_on(c: CallbackQuery, state: FSMContext):
    await state.set_state(AI.chat)
    await state.update_data(hist=[])
    await edit(c, "🤖 Savolingizni yozing (darslar, uy vazifasi, tushuntirish...).", kb([back("home", "Chiqish")]))


@user_r.message(AI.chat, F.text)
async def ai_chat(m: Message, state: FSMContext):
    if not GEMINI_KEY:
        return await m.answer("AI hozircha sozlanmagan.")
    hist = (await state.get_data()).get("hist", [])
    hist.append({"role": "user", "text": m.text})
    await m.bot.send_chat_action(m.chat.id, ChatAction.TYPING)
    u = await get_user(m.from_user.id)
    extra = f"\nFoydalanuvchi: {fullname(u)}, {ROLES.get(u.get('role'), '')} {u.get('class') or ''}".rstrip()
    try:
        ans = await gemini(hist[-7:], extra)  # oxirgi 7 ta xabar (user bilan boshlanadi)
    except Exception:
        logging.exception("Gemini xato")
        return await m.answer("Kechirasiz, hozir javob bera olmadim. Keyinroq urinib ko'ring.")
    hist.append({"role": "model", "text": ans})
    await state.update_data(hist=hist[-7:])
    await incr("ai_count")
    await m.answer(ans[:4000], parse_mode=None, reply_markup=kb([back("home", "Chiqish")]))


# ================= ADMIN PANEL =================
class A(StatesGroup):
    text = State()
    cls_add = State()
    adm_add = State()
    bc = State()
    hw_text = State()
    info = State()
    search = State()
    reply = State()
    lbl = State()
    emo = State()
    sub = State()


def admin_menu():
    return kb([
        [btn("📅 Jadval qo'shish", "a_sch", "panel"), btn("🗑 Jadvalni o'chirish", "a_dsch", "red")],
        [btn("📚 Uy vazifasi qo'shish", "a_hw", "panel"), btn("🗑 Vazifani o'chirish", "a_dhw", "red")],
        [btn("🏫 Sinf qo'shish", "a_cls", "panel"), btn("🗑 Sinfni o'chirish", "a_dcls", "red")],
        [btn("👥 Foydalanuvchilar", "a_users", "panel"), btn("👮 Adminlar", "a_adm", "panel")],
        [btn("📢 Xabar yuborish", "a_bc", "panel"), btn("📊 Statistika", "a_stat", "panel")],
        [btn("🔔 Majburiy obuna", "a_sub", "panel"), btn("ℹ️ Maktab haqida", "a_info", "panel")],
        [btn("🎛 Tugmalarni sozlash", "a_btn", "panel")],
        back("home", "⬅️ Asosiy menyu"),
    ])


@admin_r.message(Command("admin"))
async def admin_cmd(m: Message, state: FSMContext):
    await state.clear()
    await say(m, "⚙️ Admin panel", admin_menu())


@admin_r.callback_query(F.data == "adm")
async def adm(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await edit(c, "⚙️ Admin panel", admin_menu())


# --- Statistika ---
@admin_r.callback_query(F.data == "a_stat")
async def a_stat(c: CallbackQuery):
    started = await scalar("SELECT COUNT(*) FROM users")
    reg = await scalar("SELECT COUNT(*) FROM users WHERE COALESCE(first_name,'')<>''")
    today = await scalar("SELECT COUNT(*) FROM users WHERE date(created)=date('now','+5 hours')")
    week = await scalar("SELECT COUNT(*) FROM users WHERE created >= datetime('now','+5 hours','-7 days')")
    blocked = await scalar("SELECT COUNT(*) FROM users WHERE blocked=1")
    roles = dict(await q("SELECT role, COUNT(*) FROM users WHERE COALESCE(first_name,'')<>'' GROUP BY role"))
    per_class = sorted(await q("SELECT class, COUNT(*) FROM users WHERE COALESCE(class,'')<>'' GROUP BY class"),
                       key=lambda r: class_key(r[0]))
    classes = await scalar("SELECT COUNT(*) FROM classes")
    lessons = await scalar("SELECT COUNT(*) FROM schedule")
    hws = await scalar("SELECT COUNT(*) FROM homework")
    posts = await scalar("SELECT COUNT(*) FROM channel_news")
    subs = await scalar("SELECT COUNT(*) FROM sub_channels")
    ai = await get_setting("ai_count", "0")
    msgs = await get_setting("msg_count", "0")
    cls_lines = "\n".join(f"   • {esc(cl)}: <b>{n}</b>" for cl, n in per_class[:20]) or "   —"
    await edit(c, "📊 <b>Statistika</b>\n\n"
                  f"👤 Botga kirganlar: <b>{started}</b>\n"
                  f"✅ Ro'yxatdan o'tganlar: <b>{reg}</b>\n"
                  f"   • bugun: <b>{today}</b>, 7 kunda: <b>{week}</b>\n"
                  f"   • 🎓 {roles.get('student', 0)}  👨‍🏫 {roles.get('teacher', 0)}  👪 {roles.get('parent', 0)}\n"
                  f"🚫 Bloklanganlar: <b>{blocked}</b>\n\n"
                  f"🏫 Sinflar: <b>{classes}</b>\n{cls_lines}\n\n"
                  f"📅 Jadval kunlari: <b>{lessons}</b>\n"
                  f"📚 Uy vazifalari: <b>{hws}</b>\n"
                  f"📰 Kanal yangiliklari: <b>{posts}</b>\n"
                  f"🔔 Majburiy obuna kanallari: <b>{subs}</b>\n"
                  f"🤖 AI savollari: <b>{ai}</b>\n"
                  f"✉️ Murojaatlar: <b>{msgs}</b>\n"
                  f"👮 Adminlar: <b>{len(ADMINS)}</b>", kb([back("adm")]))


# --- Sinflar ---
@admin_r.callback_query(F.data == "a_cls")
async def a_cls(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.cls_add)
    await edit(c, "Sinf nomini yozing (masalan: <b>7-A</b>).\nBir nechta sinfni vergul bilan yozish mumkin: "
                  "<code>5-A, 5-B, 6-A</code>\nBekor qilish: /cancel")


@admin_r.message(A.cls_add, F.text)
async def a_cls_save(m: Message, state: FSMContext):
    names = [n.strip().replace(":", "-")[:20] for n in re.split(r"[,\n]", m.text) if n.strip()]
    for n in names:
        await q("INSERT OR IGNORE INTO classes VALUES(?)", (n,), commit=True)
    await state.clear()
    await say(m, f"✅ Qo'shildi: {esc(', '.join(names))}", admin_menu())


@admin_r.callback_query(F.data == "a_dcls")
async def a_dcls(c: CallbackQuery):
    classes = await get_classes()
    if not classes:
        return await edit(c, "Sinflar yo'q.", kb([back("adm")]))
    b = InlineKeyboardBuilder()
    for cl in classes:
        b.add(btn(f"🗑 {cl}", f"dl:{cl}", "red"))
    b.adjust(3)
    b.row(*back("adm"))
    await edit(c, "Qaysi sinfni o'chirasiz? (jadvali va uy vazifalari ham o'chadi)", b.as_markup())


@admin_r.callback_query(F.data.startswith("dl:"))
async def dl(c: CallbackQuery):
    cl = c.data[3:]
    await q("DELETE FROM classes WHERE name=?", (cl,), commit=True)
    await q("DELETE FROM schedule WHERE class=?", (cl,), commit=True)
    await q("DELETE FROM homework WHERE class=?", (cl,), commit=True)
    await edit(c, f"✅ {esc(cl)} o'chirildi.", admin_menu())


# --- Jadval ---
@admin_r.callback_query(F.data == "a_sch")
async def a_sch(c: CallbackQuery):
    classes = await get_classes()
    if not classes:
        return await edit(c, "Avval «🏫 Sinf qo'shish» orqali sinf kiriting.", kb([back("adm")]))
    b = class_buttons(classes, "as")
    b.row(*back("adm"))
    await edit(c, "Qaysi sinf uchun jadval kiritasiz?", b.as_markup())


def day_buttons():
    b = InlineKeyboardBuilder()
    for i, d in enumerate(DAYS):
        b.add(btn(d, f"ad:{i}", "day"))
    b.adjust(2)
    b.row(*back("a_sch"))
    return b.as_markup()


@admin_r.callback_query(F.data.startswith("as:"))
async def a_pick_class(c: CallbackQuery, state: FSMContext):
    await state.update_data(cls=c.data[3:])
    await edit(c, f"<b>{esc(c.data[3:])}</b> sinf. Kunni tanlang:", day_buttons())


@admin_r.callback_query(F.data.startswith("ad:"))
async def a_day(c: CallbackQuery, state: FSMContext):
    d = await state.get_data()
    if "cls" not in d:
        return await c.answer("Avval sinfni tanlang", show_alert=True)
    day = int(c.data[3:])
    await state.update_data(day=day)
    await state.set_state(A.text)
    await edit(c, f"<b>{esc(d['cls'])} — {DAYS[day]}</b>\nDarslarni yuboring, har bir dars yangi qatorda:\n"
                  "<code>1. Matematika (8:00)\n2. Ona tili (8:50)</code>\n"
                  "Premium emoji ham ishlatish mumkin.\nBekor qilish: /cancel")


@admin_r.message(A.text, F.text)
async def a_text(m: Message, state: FSMContext):
    d = await state.get_data()
    await q("INSERT OR REPLACE INTO schedule VALUES(?,?,?)", (d["cls"], d["day"], m.html_text.strip()), commit=True)
    await state.set_state(None)  # sinf eslab qoladi
    await say(m, f"✅ {esc(d['cls'])} — {DAYS[d['day']]} saqlandi.\nYana kun tanlang:", day_buttons())


@admin_r.callback_query(F.data == "a_dsch")
async def a_dsch(c: CallbackQuery):
    classes = sorted([r[0] for r in await q("SELECT DISTINCT class FROM schedule")], key=class_key)
    if not classes:
        return await edit(c, "Kiritilgan jadval yo'q.", kb([back("adm")]))
    b = InlineKeyboardBuilder()
    for cl in classes:
        b.add(btn(f"🗑 {cl}", f"dc:{cl}", "red"))
    b.adjust(3)
    b.row(*back("adm"))
    await edit(c, "Qaysi sinfning jadvalini o'chirasiz?", b.as_markup())


@admin_r.callback_query(F.data.startswith("dc:"))
async def dc(c: CallbackQuery):
    await q("DELETE FROM schedule WHERE class=?", (c.data[3:],), commit=True)
    await edit(c, "✅ Jadval o'chirildi.", admin_menu())


# --- Uy vazifasi ---
@admin_r.callback_query(F.data == "a_hw")
async def a_hw(c: CallbackQuery):
    classes = await get_classes()
    if not classes:
        return await edit(c, "Avval «🏫 Sinf qo'shish» orqali sinf kiriting.", kb([back("adm")]))
    b = class_buttons(classes, "hc")
    b.row(*back("adm"))
    await edit(c, "Qaysi sinf uchun uy vazifasi qo'shasiz?", b.as_markup())


@admin_r.callback_query(F.data.startswith("hc:"))
async def hc(c: CallbackQuery, state: FSMContext):
    await state.update_data(hcls=c.data[3:])
    await state.set_state(A.hw_text)
    await edit(c, f"📚 <b>{esc(c.data[3:])}</b> uchun uy vazifasini yozing.\n"
                  "Saqlangach, sinf o'quvchilariga avtomatik xabar boradi.\nBekor qilish: /cancel")


@admin_r.message(A.hw_text, F.text)
async def hw_save(m: Message, state: FSMContext):
    cl = (await state.get_data())["hcls"]
    txt = m.html_text.strip()
    await q("INSERT INTO homework(class, text, created) VALUES(?,?,datetime('now','+5 hours'))", (cl, txt), commit=True)
    await state.clear()
    await say(m, "✅ Uy vazifasi qo'shildi. Sinf a'zolariga xabar yuborilmoqda.", admin_menu())
    asyncio.create_task(notify_class(m.bot, cl, f"📚 <b>Yangi uy vazifasi — {esc(cl)}</b>\n\n{txt}"))


@admin_r.callback_query(F.data == "a_dhw")
async def a_dhw(c: CallbackQuery):
    rows = await q("SELECT id, class, text FROM homework ORDER BY id DESC LIMIT 10")
    if not rows:
        return await edit(c, "Uy vazifalari yo'q.", kb([back("adm")]))
    b = InlineKeyboardBuilder()
    for i, cl, t in rows:
        short = html.unescape(re.sub(r"<[^>]+>", "", t)).replace("\n", " ")[:25]
        b.row(btn(f"🗑 {cl}: {short}", f"dh:{i}", "red"))
    b.row(*back("adm"))
    await edit(c, "O'chiriladigan uy vazifasini tanlang:", b.as_markup())


@admin_r.callback_query(F.data.startswith("dh:"))
async def dh(c: CallbackQuery):
    await q("DELETE FROM homework WHERE id=?", (int(c.data[3:]),), commit=True)
    await c.answer("O'chirildi")
    await a_dhw(c)


# --- Maktab haqida ---
@admin_r.callback_query(F.data == "a_info")
async def a_info(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.info)
    cur = await get_setting("info", "— kiritilmagan —")
    await edit(c, f"ℹ️ <b>Maktab haqida / aloqa</b> matni (foydalanuvchilar «Aloqa» bo'limida ko'radi).\n\n"
                  f"Hozirgi matn:\n{cur}\n\nYangi matnni yuboring (manzil, telefon, ish vaqti...). "
                  "Premium emoji ham mumkin.\nBekor qilish: /cancel")


@admin_r.message(A.info, F.text)
async def info_save(m: Message, state: FSMContext):
    await put("info", m.html_text.strip())
    await state.clear()
    await say(m, "✅ Saqlandi.", admin_menu())


# --- Majburiy obuna ---
@admin_r.callback_query(F.data == "a_sub")
async def a_sub(c: CallbackQuery, state: FSMContext):
    await state.clear()
    rows = await q("SELECT chat_id, title FROM sub_channels ORDER BY rowid")
    text = ("🔔 <b>Majburiy obuna</b>\n\nFoydalanuvchilar quyidagi kanallarga obuna bo'lmaguncha botdan "
            "foydalana olmaydi.\n⚠️ Bot har bir kanalda <b>administrator</b> bo'lishi shart.\n\n")
    text += "\n".join(f"• {esc(t)}" for _, t in rows) if rows else "Hozircha kanal qo'shilmagan (obuna talab qilinmaydi)."
    buttons = [[btn("➕ Kanal qo'shish", "a_addsub", "green")]]
    buttons += [[btn(f"❌ {t}", f"sr:{i}", "red")] for i, t in rows]
    buttons.append(back("adm"))
    await edit(c, text, kb(buttons))


@admin_r.callback_query(F.data == "a_addsub")
async def a_addsub(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.sub)
    await edit(c, "Kanalni qo'shish uchun quyidagilardan birini yuboring:\n"
                  "• kanal <b>@username</b> yoki <code>t.me/username</code>\n"
                  "• kanal ID (<code>-100...</code>)\n"
                  "• kanaldan biror postni <b>forward</b> qiling (yopiq kanal uchun)\n\n"
                  "Avval botni kanalga administrator qilib qo'shing!\nBekor qilish: /cancel")


@admin_r.message(A.sub)
async def sub_add(m: Message, state: FSMContext):
    ref = None
    fo = m.forward_origin
    if fo is not None and getattr(fo, "chat", None):
        ref = fo.chat.id
    elif m.text:
        t = re.sub(r"^https?://t\.me/", "", m.text.strip()).lstrip("@").split("/")[0]
        if re.fullmatch(r"-?\d+", t):
            ref = int(t)
        elif t and not t.startswith("+"):
            ref = "@" + t
    if ref is None:
        return await m.answer("❌ Tushunmadim. @username, kanal ID yoki kanaldan forward qilingan post yuboring.")
    try:
        chat = await m.bot.get_chat(ref)
    except Exception:
        return await m.answer("❌ Kanal topilmadi. Username/ID ni tekshiring va botni kanalga admin qiling.")
    try:
        me = await m.bot.get_chat_member(chat.id, m.bot.id)
        is_adm = me.status in ("administrator", "creator")
    except Exception:
        is_adm = False
    if not is_adm:
        return await m.answer("⚠️ Bot bu kanalda administrator emas. Avval botni kanalga admin qilib, "
                              "qayta yuboring.")
    link = f"https://t.me/{chat.username}" if chat.username else None
    if not link:
        try:
            link = await m.bot.export_chat_invite_link(chat.id)
        except Exception:
            link = None
    if not link:
        return await m.answer("❌ Kanal havolasini olib bo'lmadi. Botga «Havola orqali taklif qilish» huquqini bering.")
    await q("INSERT OR REPLACE INTO sub_channels VALUES(?,?,?)", (chat.id, chat.title or str(chat.id), link), commit=True)
    SUB_CACHE.clear()
    await state.clear()
    await say(m, f"✅ <b>{esc(chat.title or '')}</b> majburiy obunaga qo'shildi.", admin_menu())


@admin_r.callback_query(F.data.startswith("sr:"))
async def sr(c: CallbackQuery, state: FSMContext):
    await q("DELETE FROM sub_channels WHERE chat_id=?", (int(c.data[3:]),), commit=True)
    SUB_CACHE.clear()
    await c.answer("Kanal o'chirildi")
    await a_sub(c, state)


# --- Foydalanuvchilar ---
@admin_r.callback_query(F.data == "a_users")
async def a_users(c: CallbackQuery):
    reg = await scalar("SELECT COUNT(*) FROM users WHERE COALESCE(first_name,'')<>''")
    await edit(c, f"👥 <b>Foydalanuvchilar</b>\n\nRo'yxatdan o'tganlar: <b>{reg}</b>",
               kb([[btn("📥 Ro'yxatni yuklab olish (CSV)", "a_csv", "panel")],
                   [btn("🔍 Foydalanuvchini qidirish", "a_find", "panel")], back("adm")]))


@admin_r.callback_query(F.data == "a_csv")
async def a_csv(c: CallbackQuery):
    rows = await q("SELECT id, first_name, last_name, role, class, name, created, blocked FROM users ORDER BY created")
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["ID", "Ism", "Familya", "Rol", "Sinf", "Telegram nomi", "Sana", "Bloklangan"])
    for i, f, l, role, cl, tg, cr, bl in rows:
        w.writerow([i, f or "", l or "", re.sub(r"^\S+ ", "", ROLES.get(role, "")), cl or "", tg or "", cr or "",
                    "ha" if bl else ""])
    await c.message.answer_document(BufferedInputFile(buf.getvalue().encode("utf-8-sig"), "foydalanuvchilar.csv"))
    await c.answer()


@admin_r.callback_query(F.data == "a_find")
async def a_find(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.search)
    await edit(c, "🔍 Ism, familya yoki Telegram ID yozing.\nBekor qilish: /cancel")


@admin_r.message(A.search, F.text)
async def find_do(m: Message, state: FSMContext):
    t = m.text.strip()
    like = f"%{t}%"
    rows = await q("SELECT id, first_name, last_name, class FROM users WHERE id=? OR first_name LIKE ? "
                   "OR last_name LIKE ? OR name LIKE ? LIMIT 8", (int(t) if t.isdigit() else -1, like, like, like))
    await state.clear()
    if not rows:
        return await say(m, "Hech narsa topilmadi.", kb([back("a_users")]))
    b = InlineKeyboardBuilder()
    for i, f, l, cl in rows:
        b.row(btn(f"{f or ''} {l or ''} {('(' + cl + ')') if cl else ''}".strip() or str(i), f"uv:{i}", "panel"))
    b.row(*back("a_users"))
    await say(m, "Topilganlar:", b.as_markup())


async def user_card(uid: int):
    u = await get_user(uid)
    blocked = uid in BLOCKED
    text = (f"👤 <b>{esc(fullname(u))}</b>\n🆔 <code>{uid}</code>\n"
            f"Rol: {ROLES.get(u.get('role'), '—')}\nSinf: {esc(u.get('class') or '—')}\n"
            f"Sana: {fmt_date(u.get('created'))}\nHolat: {'🚫 bloklangan' if blocked else '✅ faol'}")
    rows = [[btn("✉️ Xabar yozish", f"rp:{uid}", "green")]]
    if uid not in ADMINS:
        rows.append([btn("✅ Blokdan chiqarish" if blocked else "🚫 Bloklash", f"ub:{uid}", "green" if blocked else "red")])
    rows.append(back("a_users"))
    return text, kb(rows)


@admin_r.callback_query(F.data.startswith("uv:"))
async def uv(c: CallbackQuery):
    await edit(c, *await user_card(int(c.data[3:])))


@admin_r.callback_query(F.data.startswith("ub:"))
async def ub(c: CallbackQuery):
    uid = int(c.data[3:])
    if uid in ADMINS:
        return await c.answer("Adminni bloklab bo'lmaydi", show_alert=True)
    now_blocked = uid not in BLOCKED
    await q("UPDATE users SET blocked=? WHERE id=?", (1 if now_blocked else 0, uid), commit=True)
    (BLOCKED.add if now_blocked else BLOCKED.discard)(uid)
    await c.answer("Bloklandi" if now_blocked else "Blokdan chiqarildi")
    await edit(c, *await user_card(uid))


# --- Murojaatga javob ---
@admin_r.callback_query(F.data.startswith("rp:"))
async def rp(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.reply)
    await state.update_data(to=int(c.data[3:]))
    await c.answer()
    await c.message.answer("↩️ Javobingizni yozing.\nBekor qilish: /cancel")


@admin_r.message(A.reply, F.text)
async def rp_send(m: Message, state: FSMContext):
    uid = (await state.get_data())["to"]
    await state.clear()
    try:
        await send_to(m.bot, uid, f"📩 <b>Admin javobi:</b>\n\n{m.html_text}")
        await m.answer("✅ Javob yuborildi.")
    except Exception:
        await m.answer("❌ Yuborib bo'lmadi (foydalanuvchi botni bloklagan bo'lishi mumkin).")


# --- Tugmalarni sozlash ---
def key_title(k: str) -> str:
    return GROUP_KEYS.get(k) or LABELS.get(k, MENU[k]["label"])


@admin_r.callback_query(F.data == "a_btn")
async def a_btn(c: CallbackQuery, state: FSMContext):
    await state.clear()
    b = InlineKeyboardBuilder()
    for k in MENU_ORDER + ["adm"]:
        b.add(btn("", f"bm:{k}", k, " (yashirin)" if k in HIDDEN else ""))
    b.adjust(2)
    b.row(btn("🎨 Boshqa tugmalar rangi", "a_col", "panel"))
    b.row(*back("adm"))
    await edit(c, "🎛 <b>Tugmalarni sozlash</b>\n\nO'zgartirmoqchi bo'lgan tugmani tanlang. "
                  "Matn, rang, premium emoji va ko'rinishini o'zgartirish mumkin.", b.as_markup())


def btn_detail(k: str):
    m = MENU[k]
    label = LABELS.get(k, m["label"])
    cid, u = CEMOJI.get(k), UEMOJI.get(k, m["emoji"])
    emo = f"⭐ premium <code>{cid}</code> (zaxira: {u or '—'})" if cid else (u or "yo'q")
    text = (f"🎛 <b>{esc(label)}</b> tugmasi\n\n✏️ Matn: {esc(label)}\n"
            f"🎨 Rang: {COLOR_NAMES[COLORS.get(k, 'none')]}\n😀 Emoji: {emo}\n"
            f"👁 Holat: {'🙈 yashirin' if k in HIDDEN else 'ko`rinadi'}\n\n"
            "Pastdagi birinchi tugma — hozirgi ko'rinishi.")
    rows = [[btn("", "noop", k)],
            [btn("✏️ Matnni o'zgartirish", f"bl:{k}", "panel")],
            [btn("🎨 Rangni o'zgartirish", f"cc:{k}", "panel")],
            [btn("😀 Emoji / Premium emoji", f"be:{k}", "panel")]]
    if k != "adm":
        rows.append([btn("👁 Ko'rsatish" if k in HIDDEN else "🙈 Yashirish", f"bh:{k}", "panel")])
    rows.append([btn("♻️ Standart holatga qaytarish", f"br:{k}", "blue")])
    rows.append(back("a_btn"))
    return text, kb(rows)


@admin_r.callback_query(F.data.startswith("bm:"))
async def bm(c: CallbackQuery, state: FSMContext):
    await state.clear()
    k = c.data[3:]
    if k in MENU:
        await edit(c, *btn_detail(k))


@admin_r.callback_query(F.data.startswith("bl:"))
async def bl(c: CallbackQuery, state: FSMContext):
    k = c.data[3:]
    if k not in MENU:
        return
    await state.set_state(A.lbl)
    await state.update_data(key=k)
    await edit(c, "✏️ Tugma uchun yangi matnni yozing (emojisiz, 1–25 belgi).\nBekor qilish: /cancel")


@admin_r.message(A.lbl, F.text)
async def bl_save(m: Message, state: FSMContext):
    k = (await state.get_data())["key"]
    t = m.text.strip()
    if not 1 <= len(t) <= 25:
        return await m.answer("Matn 1–25 belgidan iborat bo'lsin.")
    LABELS[k] = t
    await put(f"label:{k}", t)
    await state.clear()
    await say(m, *btn_detail(k))


@admin_r.callback_query(F.data.startswith("be:"))
async def be(c: CallbackQuery, state: FSMContext):
    k = c.data[3:]
    if k not in MENU:
        return
    await state.set_state(A.emo)
    await state.update_data(key=k)
    await edit(c, "😀 Tugma uchun emoji yuboring:\n"
                  "• <b>Premium emoji</b> yuborsangiz — tugmada animatsiyali ikonka bo'ladi\n"
                  "• oddiy emoji ham mumkin\n\n"
                  "⚠️ Premium emoji tugmada ko'rinishi uchun <b>bot egasining</b> (botni @BotFather da "
                  "yaratgan akkaunt) Telegram Premium obunasi bo'lishi kerak.",
         kb([[btn("🚫 Emojisiz", f"bx:{k}", "red")], back(f"bm:{k}")]))


@admin_r.message(A.emo, F.text)
async def be_save(m: Message, state: FSMContext):
    k = (await state.get_data())["key"]
    ent = next((e for e in (m.entities or []) if e.type == "custom_emoji"), None)
    t = m.text.strip()
    if ent:
        CEMOJI[k] = ent.custom_emoji_id
        UEMOJI[k] = ent.extract_from(m.text) or MENU[k]["emoji"]
    elif t.isdigit() and len(t) >= 10:   # to'g'ridan-to'g'ri emoji ID
        CEMOJI[k] = t
    else:
        CEMOJI.pop(k, None)
        UEMOJI[k] = t[:8]
    if k in CEMOJI:
        await put(f"cemoji:{k}", CEMOJI[k])
    else:
        await drop(f"cemoji:{k}")
    if k in UEMOJI:
        await put(f"uemoji:{k}", UEMOJI[k])
    await state.clear()
    await say(m, *btn_detail(k))


@admin_r.callback_query(F.data.startswith("bx:"))
async def bx(c: CallbackQuery, state: FSMContext):
    k = c.data[3:]
    if k not in MENU:
        return
    CEMOJI.pop(k, None)
    UEMOJI[k] = ""
    await drop(f"cemoji:{k}")
    await put(f"uemoji:{k}", "")
    await state.clear()
    await edit(c, *btn_detail(k))


@admin_r.callback_query(F.data.startswith("bh:"))
async def bh(c: CallbackQuery):
    k = c.data[3:]
    if k not in MENU or k == "adm":
        return
    if k in HIDDEN:
        HIDDEN.discard(k)
        await drop(f"hidden:{k}")
    else:
        HIDDEN.add(k)
        await put(f"hidden:{k}", "1")
    await edit(c, *btn_detail(k))


@admin_r.callback_query(F.data.startswith("br:"))
async def br(c: CallbackQuery):
    k = c.data[3:]
    if k not in MENU:
        return
    for d in (LABELS, UEMOJI, CEMOJI):
        d.pop(k, None)
    HIDDEN.discard(k)
    COLORS[k] = DEFAULT_COLORS[k]
    await drop(f"label:{k}", f"uemoji:{k}", f"cemoji:{k}", f"hidden:{k}", f"color:{k}")
    await c.answer("♻️ Standart holat")
    await edit(c, *btn_detail(k))


# --- Ranglar ---
@admin_r.callback_query(F.data == "a_col")
async def a_col(c: CallbackQuery):
    rows = [[btn(f"{label}: {COLOR_NAMES[COLORS.get(k, 'none')]}", f"cc:{k}", k)] for k, label in GROUP_KEYS.items()]
    rows.append([btn("♻️ Standart ranglar", "cr", "blue")])
    rows.append(back("a_btn"))
    await edit(c, "🎨 Qaysi tugmalar guruhi rangini o'zgartirasiz?", kb(rows))


@admin_r.callback_query(F.data.startswith("cc:"))
async def cc(c: CallbackQuery):
    k = c.data[3:]
    if k not in MENU and k not in GROUP_KEYS:
        return
    rows = [
        [btn("Ko'k", f"cs:{k}:blue", "blue"), btn("Yashil", f"cs:{k}:green", "green")],
        [btn("Qizil", f"cs:{k}:red", "red"), btn("Oddiy", f"cs:{k}:none")],
        back(f"bm:{k}" if k in MENU else "a_col"),
    ]
    await edit(c, f"<b>{esc(key_title(k))}</b>\nHozirgi rang: {COLOR_NAMES[COLORS.get(k, 'none')]}\n"
                  "Yangi rangni tanlang:", kb(rows))


@admin_r.callback_query(F.data.startswith("cs:"))
async def cs(c: CallbackQuery):
    _, k, color = c.data.split(":")
    if (k not in MENU and k not in GROUP_KEYS) or color not in COLOR_NAMES:
        return
    COLORS[k] = color
    await put(f"color:{k}", color)
    await c.answer("✅ Rang o'zgartirildi")
    if k in MENU:
        await edit(c, *btn_detail(k))
    else:
        await a_col(c)


@admin_r.callback_query(F.data == "cr")
async def cr(c: CallbackQuery):
    for k in GROUP_KEYS:
        COLORS[k] = DEFAULT_COLORS[k]
        await drop(f"color:{k}")
    await c.answer("♻️ Standart ranglar qaytarildi")
    await a_col(c)


# --- Adminlar ---
async def user_name(uid: int) -> str:
    u = await get_user(uid)
    return esc(fullname(u)) if u else str(uid)


async def owners_only(c: CallbackQuery) -> bool:
    if c.from_user.id not in OWNERS:
        await c.answer("Bu amal faqat bosh admin uchun", show_alert=True)
        return False
    return True


@admin_r.callback_query(F.data == "a_adm")
async def a_adm(c: CallbackQuery):
    lines = ["👑 <b>Bosh adminlar:</b>"] + [f"• {await user_name(i)} (<code>{i}</code>)" for i in OWNERS]
    extra = sorted(ADMINS - OWNERS)
    lines.append("\n👤 <b>Adminlar:</b>" if extra else "\nQo'shimcha adminlar yo'q.")
    lines += [f"• {await user_name(i)} (<code>{i}</code>)" for i in extra]
    rows = []
    if c.from_user.id in OWNERS:
        rows.append([btn("➕ Admin qo'shish", "a_addadm", "green")])
        for i in extra:
            rows.append([btn(f"❌ {re.sub('<[^>]+>', '', await user_name(i))}", f"ra:{i}", "red")])
    rows.append(back("adm"))
    await edit(c, "\n".join(lines), kb(rows))


@admin_r.callback_query(F.data == "a_addadm")
async def a_addadm(c: CallbackQuery, state: FSMContext):
    if not await owners_only(c):
        return
    await state.set_state(A.adm_add)
    await edit(c, "Yangi adminning <b>Telegram ID</b> raqamini yuboring.\n"
                  "(ID ni @userinfobot orqali bilish mumkin. U botga avval /start bosgan bo'lishi kerak.)\n"
                  "Bekor qilish: /cancel")


@admin_r.message(A.adm_add, F.text)
async def adm_add_save(m: Message, state: FSMContext):
    if m.from_user.id not in OWNERS:
        return
    if not m.text.strip().isdigit():
        return await m.answer("Faqat raqamli ID yuboring.")
    uid = int(m.text.strip())
    await q("INSERT OR IGNORE INTO admins(id, name) VALUES(?,?)", (uid, ""), commit=True)
    ADMINS.add(uid)
    await state.clear()
    try:
        await m.bot.send_message(uid, "✅ Siz botda admin etib tayinlandingiz. /admin buyrug'ini yuboring.")
    except Exception:
        pass
    await say(m, f"✅ <code>{uid}</code> admin qilindi.", admin_menu())


@admin_r.callback_query(F.data.startswith("ra:"))
async def ra(c: CallbackQuery):
    if not await owners_only(c):
        return
    uid = int(c.data[3:])
    if uid not in OWNERS:
        await q("DELETE FROM admins WHERE id=?", (uid,), commit=True)
        ADMINS.discard(uid)
    await c.answer("Admin o'chirildi")
    await a_adm(c)


# --- Ommaviy xabar ---
@admin_r.callback_query(F.data == "a_bc")
async def a_bc(c: CallbackQuery, state: FSMContext):
    await state.set_state(A.bc)
    await edit(c, "📢 Yuboriladigan xabarni yuboring (matn, rasm, fayl — istalgan; premium emoji ham mumkin).\n"
                  "Keyin kimga yuborishni tanlaysiz.\nBekor qilish: /cancel")


@admin_r.message(A.bc)
async def bc_preview(m: Message, state: FSMContext):
    await state.update_data(mid=m.message_id)
    await m.copy_to(m.chat.id)
    await say(m, "Yuqoridagi xabarni kimga yuboramiz?", kb([
        [btn("👥 Hammaga", "bt:all", "green")],
        [btn("🎓 O'quvchilarga", "bt:r:student", "blue"), btn("👨‍🏫 O'qituvchilarga", "bt:r:teacher", "blue")],
        [btn("👪 Ota-onalarga", "bt:r:parent", "blue"), btn("🏫 Sinf bo'yicha", "bt:pickcls", "blue")],
        [btn("❌ Bekor", "adm", "red")],
    ]))


@admin_r.callback_query(F.data.startswith("bt:"))
async def bt(c: CallbackQuery):
    t = c.data[3:]
    if t == "pickcls":
        classes = await get_classes()
        if not classes:
            return await c.answer("Sinflar yo'q", show_alert=True)
        b = class_buttons(classes, "bt:c")
        b.row(btn("❌ Bekor", "adm", "red"))
        return await edit(c, "Qaysi sinfga yuboramiz?", b.as_markup())
    n = len(await target_ids(t))
    await edit(c, f"Xabar <b>{target_label(t)}</b> yuboriladi ({n} ta foydalanuvchi). Tasdiqlaysizmi?",
               kb([[btn("✅ Yuborish", f"bk:{t}", "green"), btn("❌ Bekor", "adm", "red")]]))


@admin_r.callback_query(F.data.startswith("bk:"))
async def bk(c: CallbackQuery, state: FSMContext):
    t = c.data[3:]
    mid = (await state.get_data()).get("mid")
    await state.clear()
    if not mid:
        return await c.answer("Xabar topilmadi", show_alert=True)
    await edit(c, "⏳ Yuborilmoqda...")
    ok, fail = await broadcast(c.bot, c.message.chat.id, mid, t)
    await say(c.message, f"✅ Yuborildi: {ok}\n❌ Xato: {fail}", admin_menu())


async def main():
    logging.basicConfig(level=logging.INFO)
    await init_db()
    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher()
    dp.include_routers(cmd_r, reg_r, channel_r, admin_r, user_r)
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
