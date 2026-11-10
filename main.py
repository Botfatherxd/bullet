"""
Roza Search — Telegram-бот поиска свободных @username (5-6 букв).
Проверка: t.me + fragment.com (приблизительная, удалённые аккаунты не учитываются).
Стек: aiogram 3.x, aiohttp, sqlite3.
"""
import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import string
import time
from datetime import datetime

import aiohttp
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import BaseFilter, Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup,
                           InputMediaPhoto, LabeledPrice, Message, PreCheckoutQuery, User)

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("roza")

# ───────────────────────── КОНФИГ ─────────────────────────
TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "ruxdev").lower().lstrip("@")
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x}
DB_PATH = os.getenv("DB_PATH", "roza.db")

CFG = {  # name: (подпись, значение по умолчанию)
    "price": ("Цена Premium (⭐)", 15),
    "premium_days": ("Срок Premium, дней (0 = навсегда)", 30),
    "daily_free": ("Бесплатных запросов в день", 3),
    "search_time": ("Макс. время поиска, сек", 90),
    "premium_emoji": ("Премиум-эмодзи (1 вкл / 0 выкл)", 1),
}

# ID премиум-эмодзи (из открытой документации и библиотек). Любой можно заменить в админке.
EID = {
    "ok": "5920052658743283381", "no": "5258226313285607065", "gem": "5807465992363710697",
    "star": "5874948844935974490", "bolt": "5843553939672274145", "back": "5877536313623711363",
    "gear": "5258420634785947640", "warn": "5258474669769497337", "info": "5258503720928288433",
    "gift": "5406756500108501710", "rocket": "5447410659077661506", "mega": "5424818078833715060",
}


def E(key, ch):
    return f'<tg-emoji emoji-id="{EID[key]}">{ch}</tg-emoji>'


# key: (подпись для админки, текст по умолчанию)
TEXTS = {
    "welcome": ("Приветствие (/start)",
                E("rocket", "🚀") + " <b>Добро пожаловать в Roza Search!</b>\nЗдесь ты сможешь найти @username быстро и бесплатно."),
    "choose_len": ("Выбор длины", "Выберите длину:"),
    "searching": ("Идёт поиск  {length} {name} {checked} {sec} {frame} {dots}",
                  "{frame} Ищу свободный юзернейм из {length} букв{dots}\n\nПроверяю: <code>@{name}</code>\nПроверено: {checked} · {sec} сек"),
    "results": ("Результат  {length} {items} {left}",
                E("ok", "✅") + " <b>Найден свободный юзернейм ({length} букв)</b>\n\n{items}\n\nОсталось запросов: {left}"),
    "item": ("Строка результата  {level} {name}", "{level}  <code>@{name}</code>"),
    "nothing": ("Ничего не найдено", E("warn", "⚠️") + " Свободный юзернейм не найден за отведённое время. Попробуйте ещё раз."),
    "stopped": ("Поиск остановлен", "Поиск остановлен."),
    "no_requests": ("Закончились запросы", "У вас закончились запросы. Они обновятся завтра, либо оформите Premium."),
    "locked": ("Категория только для Premium", E("warn", "⚠️") + " Поиск из 5 букв доступен только с Premium."),
    "premium": ("Экран Premium",
                E("gem", "💎") + " <b>Преимущества Premium</b>\n\n" + E("ok", "✅") + " Без лимитов на поиск\n"
                + E("ok", "✅") + " Доступен поиск из 5 букв\n" + E("ok", "✅") + " Приоритет в поддержке"),
    "premium_active": ("Premium активен  {until}", E("ok", "✅") + " Ваш Premium активен: {until}"),
    "paid": ("После оплаты  {until}", E("gem", "💎") + " Оплата прошла! Premium активен: {until}"),
    "inv_title": ("Заголовок счёта", "Roza Premium"),
    "inv_desc": ("Описание счёта", "Без лимитов на поиск, поиск из 5 букв, приоритет в поддержке"),
    "support_new": ("Поддержка: новый тикет", "Опишите проблему одним сообщением — ответ придёт сюда."),
    "support_write": ("Поддержка: написать в тикет", "Напишите сообщение в тикет:"),
    "support_open": ("Поддержка: тикет  {id} {dialog}", "<b>🎫 Тикет #{id}</b>\n\n{dialog}"),
    "ticket_created": ("Тикет создан  {id}", E("ok", "✅") + " Тикет #{id} создан."),
    "ticket_closed": ("Тикет закрыт", "Тикет закрыт."),
    "ticket_notice": ("Уведомление об ответе  {id}", E("mega", "📢") + " Новый ответ поддержки по тикету #{id}"),
    "gift_notice": ("Уведомление о выдаче Premium  {until}", E("gift", "🎁") + " Вам выдан Premium: {until}"),
    "banned": ("Бан", "Доступ ограничен."),
    "unavailable": ("Категория недоступна", "Категория недоступна."),
    "busy": ("Поиск уже идёт", "Поиск уже идёт, подождите."),
    "lvl0": ("Уровень 0", "💀 Ужас"),
    "lvl1": ("Уровень 1", "🙂 Старт"),
    "lvl2": ("Уровень 2", "✨ Красота"),
    "lvl3": ("Уровень 3", "💎 Идеал"),
}

BUTTONS = {
    "start_search": ("Начать поиск", "Начать поиск"),
    "premium": ("Premium", "Premium"),
    "support": ("Поддержка", "Поддержка"),
    "back": ("Назад", "Назад"),
    "pay": ("Оплата  {price}", "Оплатить {price} звёзд"),
    "more": ("Искать ещё", "Искать ещё"),
    "filter": ("Фильтр «Без символов»  {state}", "Без символов {state}"),
    "cancel": ("Отмена поиска", "Отмена"),
    "t_write": ("Написать в тикет", "✏️ Написать"),
    "t_close": ("Закрыть тикет", "Закрыть тикет"),
    "category": ("Кнопки категорий (только цвет)", ""),
}
# иконки-эмодзи на кнопках по умолчанию (Bot API 9.4+, icon_custom_emoji_id)
DEFAULT_ICON = {"start_search": EID["bolt"], "premium": EID["gem"], "back": EID["back"],
                "pay": EID["star"], "more": EID["rocket"], "cancel": EID["no"]}
STYLES = ["", "primary", "success", "danger"]
STYLE_NAMES = {"": "⚪ Обычная", "primary": "🔵 Синяя", "success": "🟢 Зелёная", "danger": "🔴 Красная"}

SCREENS = {
    "banner": "Общий баннер (все экраны)", "start": "Главный", "len": "Выбор длины",
    "search": "Идёт поиск", "results": "Результаты", "premium": "Premium", "support": "Поддержка",
}

# ───────────────────────── БАЗА ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row


def Q(sql, *a):
    cur = db.execute(sql, a)
    db.commit()
    return cur


def ONE(sql, *a):
    return db.execute(sql, a).fetchone()


def ALL(sql, *a):
    return db.execute(sql, a).fetchall()


db.executescript("""
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY, username TEXT DEFAULT '', first_name TEXT DEFAULT '',
  joined INTEGER, requests INTEGER DEFAULT 0, req_date TEXT DEFAULT '',
  premium_until INTEGER DEFAULT 0, main_id INTEGER DEFAULT 0, main_photo INTEGER DEFAULT 0,
  main_file TEXT DEFAULT '', banned INTEGER DEFAULT 0, blocked INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS categories(
  id INTEGER PRIMARY KEY AUTOINCREMENT, length INTEGER, title TEXT,
  premium INTEGER DEFAULT 0, enabled INTEGER DEFAULT 1, sort INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tickets(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, status TEXT DEFAULT 'open', created INTEGER);
CREATE TABLE IF NOT EXISTS ticket_msgs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, ticket_id INTEGER, sender TEXT, text TEXT, ts INTEGER);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, stars INTEGER, charge_id TEXT, ts INTEGER);
""")
db.executescript("""
CREATE TABLE IF NOT EXISTS activity(id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER, uid INTEGER, kind TEXT);
CREATE INDEX IF NOT EXISTS idx_act_ts ON activity(ts);
""")
for _col, _ddl in (("nosym", "INTEGER DEFAULT 1"), ("last_active", "INTEGER DEFAULT 0")):
    try:
        db.execute(f"ALTER TABLE users ADD COLUMN {_col} {_ddl}")
        db.commit()
    except sqlite3.OperationalError:
        pass
if not ONE("SELECT 1 FROM categories"):
    Q("INSERT INTO categories(length,title,premium) VALUES(5,'5 букв',1)")
    Q("INSERT INTO categories(length,title,premium) VALUES(6,'6 букв',0)")


def sget(k, d=None):
    r = ONE("SELECT value FROM settings WHERE key=?", k)
    return r["value"] if r else d


def sset(k, v):
    Q("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", k, str(v))


def sdel(k):
    Q("DELETE FROM settings WHERE key=?", k)


def inc(name, n=1):
    sset("c:" + name, int(sget("c:" + name, 0)) + n)


def cfg(name):
    v = sget("c:" + name)
    return int(v) if v is not None else CFG[name][1]


class SafeDict(dict):
    def __missing__(self, k):
        return "{" + k + "}"


def T(key, **kw):
    raw = sget("t:" + key) or TEXTS[key][1]
    try:
        return raw.format_map(SafeDict(kw))
    except Exception:
        return raw


def pe_on():
    return cfg("premium_emoji") == 1


_TG_EMOJI = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.S)


def strip_text(t):
    return _TG_EMOJI.sub(r"\1", t) if t else t


def strip_kb(kb):
    if not kb:
        return kb
    rows = []
    for row in kb.inline_keyboard:
        nr = []
        for b in row:
            d = b.model_dump(exclude_none=True)
            d.pop("icon_custom_emoji_id", None)
            nr.append(InlineKeyboardButton(**d))
        rows.append(nr)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def btn_icon(key):
    v = sget("e:" + key)
    if v == "0":
        return None
    return v or DEFAULT_ICON.get(key)


def icon_label(key):
    v = sget("e:" + key)
    if v == "0":
        return "нет"
    return f"{v} (своя)" if v else (f"{DEFAULT_ICON[key]} (по умолчанию)" if key in DEFAULT_ICON else "нет")


def btn_text(key, **kw):
    raw = sget("b:" + key) or BUTTONS[key][1]
    try:
        return raw.format_map(SafeDict(kw))
    except Exception:
        return raw


def B(key, cb=None, url=None, raw=None, **fmt):
    """Кнопка с настраиваемым текстом и цветом."""
    kw = {"text": raw if raw is not None else btn_text(key, **fmt)}
    if url:
        kw["url"] = url
    else:
        kw["callback_data"] = cb
    st = sget("s:" + key)
    if st:
        kw["style"] = st  # цвет кнопки (Bot API 9.4+), на старых клиентах игнорируется
    ic = btn_icon(key)
    if ic and pe_on():
        kw["icon_custom_emoji_id"] = ic  # премиум-эмодзи на кнопке
    return InlineKeyboardButton(**kw)


def kbd(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def esc(s):
    return html.escape(str(s or ""))


# ───────────────────────── ПОЛЬЗОВАТЕЛИ ─────────────────────────
def is_admin(u: User) -> bool:
    return u.id in ADMIN_IDS or (u.username or "").lower() == ADMIN_USERNAME


def admin_ids():
    return [int(x) for x in (sget("admins", "") or "").split(",") if x]


def touch(u: User):
    today = datetime.now().strftime("%Y-%m-%d")
    row = ONE("SELECT * FROM users WHERE id=?", u.id)
    if not row:
        Q("INSERT INTO users(id,username,first_name,joined,requests,req_date) VALUES(?,?,?,?,?,?)",
          u.id, u.username or "", u.first_name or "", int(time.time()), cfg("daily_free"), today)
    else:
        Q("UPDATE users SET username=?, first_name=?, blocked=0 WHERE id=?", u.username or "", u.first_name or "", u.id)
        if row["req_date"] != today:
            Q("UPDATE users SET requests=MAX(requests,?), req_date=? WHERE id=?", cfg("daily_free"), today, u.id)
    Q("UPDATE users SET last_active=? WHERE id=?", int(time.time()), u.id)
    if is_admin(u) and u.id not in admin_ids():
        sset("admins", ",".join(map(str, admin_ids() + [u.id])))
    return ONE("SELECT * FROM users WHERE id=?", u.id)


def log_act(uid, kind):
    Q("INSERT INTO activity(ts,uid,kind) VALUES(?,?,?)", int(time.time()), uid, kind)
    if random.random() < 0.01:
        Q("DELETE FROM activity WHERE ts<?", int(time.time()) - 90 * 86400)


def is_premium(row) -> bool:
    return row["premium_until"] == -1 or row["premium_until"] > time.time()


def fmt_until(v):
    return "навсегда" if v == -1 else datetime.fromtimestamp(v).strftime("%d.%m.%Y")


def grant_premium(uid, days):
    row = ONE("SELECT premium_until FROM users WHERE id=?", uid)
    cur = row["premium_until"] if row else 0
    new = -1 if (days <= 0 or cur == -1) else max(cur, int(time.time())) + days * 86400
    Q("UPDATE users SET premium_until=? WHERE id=?", new, uid)
    return new


# ───────────────────────── РЕНДЕР ЕДИНОГО СООБЩЕНИЯ ─────────────────────────
def get_img(screen):
    return sget("i:" + screen) or sget("i:banner") or None


async def pin_main(bot: Bot, uid, mid):
    try:
        await bot.pin_chat_message(uid, mid, disable_notification=True)
        try:
            await bot.delete_message(uid, mid + 1)  # служебное «сообщение закреплено»
        except Exception:
            pass
    except Exception as e:
        log.warning("pin failed: %s", e)


async def _render(bot: Bot, uid, screen, text, kb):
    """Всегда редактирует одно закреплённое сообщение. Новое — только если старого нет."""
    u = ONE("SELECT * FROM users WHERE id=?", uid)
    file = get_img(screen)
    mid = u["main_id"] if u else 0
    if mid:
        try:
            if file and u["main_photo"]:
                if file != u["main_file"]:
                    await bot.edit_message_media(chat_id=uid, message_id=mid,
                                                 media=InputMediaPhoto(media=file, caption=text), reply_markup=kb)
                    Q("UPDATE users SET main_file=? WHERE id=?", file, uid)
                else:
                    await bot.edit_message_caption(chat_id=uid, message_id=mid, caption=text, reply_markup=kb)
                return
            if not file and not u["main_photo"]:
                await bot.edit_message_text(text, chat_id=uid, message_id=mid, reply_markup=kb)
                return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
            log.info("edit failed (%s), resend", e)
        try:
            await bot.delete_message(uid, mid)
        except Exception:
            pass
    try:
        if file:
            m = await bot.send_photo(uid, file, caption=text, reply_markup=kb)
        else:
            m = await bot.send_message(uid, text, reply_markup=kb)
    except TelegramForbiddenError:
        Q("UPDATE users SET blocked=1 WHERE id=?", uid)
        return
    Q("UPDATE users SET main_id=?, main_photo=?, main_file=? WHERE id=?", m.message_id, 1 if file else 0, file or "", uid)
    await pin_main(bot, uid, m.message_id)


async def render(bot: Bot, uid, screen, text, kb):
    if not pe_on():
        text, kb = strip_text(text), strip_kb(kb)
    try:
        await _render(bot, uid, screen, text, kb)
    except TelegramBadRequest as e:
        if pe_on() and "emoji" in str(e).lower():
            sset("c:premium_emoji", 0)  # Telegram не принял премиум-эмодзи: отключаем и показываем обычные
            log.warning("custom emoji rejected, disabled: %s", e)
            await _render(bot, uid, screen, strip_text(text), strip_kb(kb))
        else:
            raise


async def render_retry(bot, uid, screen, text, kb):
    try:
        await render(bot, uid, screen, text, kb)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.5)
        await render(bot, uid, screen, text, kb)


BG = set()


def bg(coro):
    t = asyncio.create_task(coro)
    BG.add(t)
    t.add_done_callback(BG.discard)


async def tmp_notice(bot: Bot, uid, text, secs=8):
    """Короткое уведомление (приходит пуш) и само удаляется."""
    if not pe_on():
        text = strip_text(text)
    try:
        m = await bot.send_message(uid, text)
    except TelegramBadRequest:
        try:
            m = await bot.send_message(uid, strip_text(text))
        except Exception:
            return
    except Exception:
        return

    async def _d():
        await asyncio.sleep(secs)
        try:
            await bot.delete_message(uid, m.message_id)
        except Exception:
            pass
    bg(_d())


# ───────────────────────── ПОИСК ЮЗЕРНЕЙМОВ ─────────────────────────
VOW = set("aeiouy")
CONS = "bcdfghjklmnprstvz"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
CACHE = {}
SEM = asyncio.Semaphore(6)
_session = None


async def session():
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=8), headers=UA)
    return _session


def gen_name(n, nosym=True):
    if random.random() < 0.65:  # чередование согласная/гласная
        cv = random.choice([0, 1])
        out = []
        for i in range(n):
            vowel = (i + cv) % 2 == 0
            out.append(random.choice("aeiou") if vowel else random.choice(CONS))
        name = "".join(out)
    else:
        name = random.choice(string.ascii_lowercase) + "".join(random.choice(string.ascii_lowercase) for _ in range(n - 1))
    if not nosym and random.random() < 0.6:  # фильтр «Без символов» выключен: добавляем цифры/подчёркивание
        chars = list(name)
        for _ in range(random.choice([1, 1, 2])):
            pos = random.randrange(1, n)
            ch = random.choice(string.digits + "_")
            if ch == "_" and (pos == n - 1 or chars[pos - 1] == "_" or (pos + 1 < n and chars[pos + 1] == "_")):
                ch = random.choice(string.digits)
            chars[pos] = ch
        name = "".join(chars)
    return name


def score(name):
    """0 Ужас · 1 Старт · 2 Красота · 3 Идеал. Цифры и «_» понижают уровень."""
    n = len(name)
    alt = sum(1 for a, b in zip(name, name[1:]) if (a in VOW) != (b in VOW))
    ratio = alt / (n - 1)
    rep_ = any(a == b for a, b in zip(name, name[1:]))
    if ratio == 1 and not rep_:
        base = 3
    elif ratio >= 0.75 and not rep_:
        base = 2
    elif ratio >= 0.5:
        base = 1
    else:
        base = 0
    sym = not re.fullmatch(r"[a-z]+", name)
    return max(0, base - (2 if sym else 0))


async def tg_free(s, name):
    async with s.get(f"https://t.me/{name}") as r:
        if r.status != 200:
            return None
        t = await r.text()
    m = re.search(r'<meta property="og:title" content="([^"]*)"', t)
    if not m:
        return None
    return m.group(1).strip().lower() == f"telegram: contact @{name}"


async def fragment_free(s, name):
    async with s.get(f"https://fragment.com/username/{name}", allow_redirects=False) as r:
        if r.status in (301, 302, 303, 307, 308):
            return True   # редирект на поиск = на Fragment такого нет
        if r.status == 200:
            return False  # страница есть: аукцион/продано/продаётся
        return None


async def check_name(s, name):
    c = CACHE.get(name)
    if c and time.time() - c[0] < 900:
        return c[1]
    async with SEM:
        try:
            ok = await tg_free(s, name)
            if ok:
                fr = await fragment_free(s, name)
                if fr is None:
                    return None
                ok = fr
            elif ok is None:
                return None
        except Exception:
            return None
    if len(CACHE) > 5000:
        CACHE.clear()
    CACHE[name] = (time.time(), ok)
    return ok


# ───────────────────────── СОСТОЯНИЯ / ФИЛЬТРЫ ─────────────────────────
class UserS(StatesGroup):
    ticket = State()


class AdmS(StatesGroup):
    find = State()
    num = State()
    cat_new = State()
    cat_title = State()
    text = State()
    btn = State()
    img = State()
    cast = State()
    reply = State()
    emoji = State()


class IsAdmin(BaseFilter):
    async def __call__(self, event) -> bool:
        return bool(event.from_user) and is_admin(event.from_user)


class ClearState(BaseMiddleware):
    async def __call__(self, handler, event, data):
        st = data.get("state")
        if st:
            await st.clear()
        return await handler(event, data)


base, adm, usr, junk = Router(), Router(), Router(), Router()
adm.message.filter(IsAdmin())
adm.callback_query.filter(IsAdmin())
adm.callback_query.outer_middleware(ClearState())
usr.callback_query.outer_middleware(ClearState())

# ───────────────────────── ЭКРАНЫ ПОЛЬЗОВАТЕЛЯ ─────────────────────────


async def scr_home(bot, uid):
    kb = kbd([[B("start_search", "u:len")], [B("premium", "u:prem"), B("support", "u:sup")]])
    await render(bot, uid, "start", T("welcome"), kb)


async def scr_len(bot, uid):
    cats = ALL("SELECT * FROM categories WHERE enabled=1 ORDER BY sort, length")
    u = ONE("SELECT nosym FROM users WHERE id=?", uid)
    btns = [B("category", f"u:cat:{c['id']}", raw=c["title"]) for c in cats]
    rows = ([[B("filter", "u:flt", state="✅" if u["nosym"] else "❌")]]
            + [btns[i:i + 2] for i in range(0, len(btns), 2)] + [[B("back", "u:home")]])
    await render(bot, uid, "len", T("choose_len"), kbd(rows))


_links = {}


async def invoice_link(bot):
    price, title, desc = cfg("price"), T("inv_title")[:32], T("inv_desc")[:255]
    k = (price, title, desc)
    if k not in _links:
        _links[k] = await bot.create_invoice_link(title=title, description=desc, payload="premium",
                                                  provider_token="", currency="XTR",
                                                  prices=[LabeledPrice(label=title, amount=price)])
    return _links[k]


async def scr_premium(bot, uid, locked=False, note=""):
    u = ONE("SELECT * FROM users WHERE id=?", uid)
    text = (note + "\n\n" if note else "") + T("premium")
    if locked:
        text += "\n\n" + T("locked")
    if is_premium(u) and not note:
        text += "\n\n" + T("premium_active", until=fmt_until(u["premium_until"]))
    rows = []
    try:
        rows.append([B("pay", url=await invoice_link(bot), price=cfg("price"))])
    except Exception as e:
        log.error("invoice link: %s", e)
    rows.append([B("back", "u:home")])
    await render(bot, uid, "premium", text, kbd(rows))


def dialog(tid, me="user"):
    rows = ALL("SELECT sender,text FROM ticket_msgs WHERE ticket_id=? ORDER BY id DESC LIMIT 8", tid)[::-1]
    out = []
    for r in rows:
        who = "Вы" if r["sender"] == me else ("Поддержка" if me == "user" else "Пользователь")
        out.append(f"<b>{who}:</b> {esc(r['text'][:400])}")
    return "\n\n".join(out)


def open_ticket(uid):
    return ONE("SELECT * FROM tickets WHERE user_id=? AND status='open' ORDER BY id DESC LIMIT 1", uid)


async def scr_support(bot, uid, state, note=""):
    t = open_ticket(uid)
    if t:
        text = (note + "\n\n" if note else "") + T("support_open", id=t["id"], dialog=dialog(t["id"]))
        kb = kbd([[B("t_write", "u:tw"), B("t_close", "u:tc")], [B("back", "u:home")]])
    else:
        await state.set_state(UserS.ticket)
        text = (note + "\n\n" if note else "") + T("support_new")
        kb = kbd([[B("back", "u:home")]])
    await render(bot, uid, "support", text, kb)


JOBS = {}


async def do_search(c: CallbackQuery, bot: Bot, cat_id):
    uid = c.from_user.id
    u = ONE("SELECT * FROM users WHERE id=?", uid)
    cat = ONE("SELECT * FROM categories WHERE id=?", cat_id)
    if not cat or not cat["enabled"]:
        return await c.answer(T("unavailable"), show_alert=True)
    prem = is_premium(u)
    if cat["premium"] and not prem:
        await c.answer()
        return await scr_premium(bot, uid, locked=True)
    if not prem and u["requests"] <= 0:
        await c.answer()
        return await render(bot, uid, "len", T("no_requests"),
                            kbd([[B("premium", "u:prem")], [B("back", "u:len")]]))
    if uid in JOBS:
        return await c.answer(T("busy"), show_alert=False)
    await c.answer()

    length, nosym = cat["length"], bool(u["nosym"])
    st = {"name": gen_name(length, nosym), "checked": 0, "found": None, "stop": False, "done": False, "seen": set()}
    JOBS[uid] = st
    log_act(uid, "search")
    inc("searches")
    limit = max(10, cfg("search_time"))
    stop_kb = kbd([[B("cancel", "u:stop")]])

    async def worker():
        s = await session()
        while not st["done"]:
            name = gen_name(length, nosym)
            if name in st["seen"]:
                continue
            st["seen"].add(name)
            st["name"] = name
            ok = await check_name(s, name)
            st["checked"] += 1
            if ok and st["found"] is None:
                st["found"] = name
                st["done"] = True
            elif ok is None:
                await asyncio.sleep(0.5)

    tasks = [asyncio.create_task(worker()) for _ in range(6)]
    t0, tick = time.time(), 0
    try:
        while not st["done"] and not st["stop"] and time.time() - t0 < limit:
            text = T("searching", length=length, name=st["name"], checked=st["checked"],
                     sec=int(time.time() - t0), frame="🔍🔎"[tick % 2], dots="." * (tick % 3 + 1))
            try:
                await render(bot, uid, "search", text, stop_kb)  # сообщение меняется каждую секунду
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after)
            except Exception as e:
                log.info("anim: %s", e)
            tick += 1
            for _ in range(10):
                if st["done"] or st["stop"]:
                    break
                await asyncio.sleep(0.1)
    finally:
        st["done"] = True
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        JOBS.pop(uid, None)

    kb = kbd([[B("more", f"u:cat:{cat_id}")], [B("back", "u:len")]])
    found = st["found"]
    if found:
        log_act(uid, "found")
        if not prem:
            Q("UPDATE users SET requests=MAX(0,requests-1) WHERE id=?", uid)
        u = ONE("SELECT * FROM users WHERE id=?", uid)
        left = "∞" if prem else u["requests"]
        item = T("item", level=T(f"lvl{score(found)}"), name=found)
        await render_retry(bot, uid, "results", T("results", length=length, items=item, left=left), kb)
    elif st["stop"]:
        await render_retry(bot, uid, "results", T("stopped"), kb)
    else:
        await render_retry(bot, uid, "results", T("nothing"), kb)


# ───────────────────────── ХЭНДЛЕРЫ ПОЛЬЗОВАТЕЛЯ ─────────────────────────
@base.message(CommandStart())
async def h_start(m: Message, bot: Bot, state: FSMContext):
    await state.clear()
    u = touch(m.from_user)
    try:
        await m.delete()
    except Exception:
        pass
    if u["banned"]:
        return await bot.send_message(m.from_user.id, T("banned"))
    if u["main_id"]:
        try:
            await bot.delete_message(m.from_user.id, u["main_id"])
        except Exception:
            pass
        Q("UPDATE users SET main_id=0 WHERE id=?", m.from_user.id)
    log_act(m.from_user.id, "start")
    await scr_home(bot, m.from_user.id)


@base.message(Command("admin"), IsAdmin())
async def h_admin(m: Message, bot: Bot, state: FSMContext):
    await state.clear()
    touch(m.from_user)
    uid = m.from_user.id
    for x in (m.message_id, sget(f"adm:{uid}")):
        try:
            if x:
                await bot.delete_message(uid, int(x))
        except Exception:
            pass
    sdel(f"adm:{uid}")
    await admin_home(bot, uid)


@usr.callback_query(F.data.startswith("u:"))
async def u_cb(c: CallbackQuery, bot: Bot, state: FSMContext):
    u = touch(c.from_user)
    if u["banned"]:
        return await c.answer(T("banned"), show_alert=True)
    uid = c.from_user.id
    p = c.data.split(":")
    cmd = p[1]
    if cmd == "cat":
        return await do_search(c, bot, int(p[2]))
    await c.answer()
    if cmd == "home":
        await scr_home(bot, uid)
    elif cmd == "stop":
        if uid in JOBS:
            JOBS[uid]["stop"] = True
    elif cmd == "flt":
        Q("UPDATE users SET nosym=1-nosym WHERE id=?", uid)
        await scr_len(bot, uid)
    elif cmd == "len":
        await scr_len(bot, uid)
    elif cmd == "prem":
        await scr_premium(bot, uid)
    elif cmd == "sup":
        await scr_support(bot, uid, state)
    elif cmd == "tw":
        await state.set_state(UserS.ticket)
        await render(bot, uid, "support", T("support_write"), kbd([[B("back", "u:sup")]]))
    elif cmd == "tc":
        t = open_ticket(uid)
        if t:
            Q("UPDATE tickets SET status='closed' WHERE id=?", t["id"])
        await scr_support(bot, uid, state, note=T("ticket_closed"))


@usr.message(UserS.ticket, F.text)
async def u_ticket(m: Message, bot: Bot, state: FSMContext):
    u = touch(m.from_user)
    uid = m.from_user.id
    try:
        await m.delete()
    except Exception:
        pass
    if u["banned"]:
        return
    text = m.text.strip()[:2000]
    t, new = open_ticket(uid), False
    if not t:
        tid = Q("INSERT INTO tickets(user_id,created) VALUES(?,?)", uid, int(time.time())).lastrowid
        new = True
        log_act(uid, "ticket")
    else:
        tid = t["id"]
    Q("INSERT INTO ticket_msgs(ticket_id,sender,text,ts) VALUES(?,?,?,?)", tid, "user", text, int(time.time()))
    await state.clear()
    await scr_support(bot, uid, state, note=T("ticket_created", id=tid) if new else "")
    star = "⭐ " if is_premium(u) else ""
    who = f"@{u['username']}" if u["username"] else esc(u["first_name"])
    for a in admin_ids():
        try:
            await bot.send_message(a, f"🎫 {star}<b>Тикет #{tid}</b> от {who} (<code>{uid}</code>)\n\n{esc(text[:500])}",
                                   reply_markup=kbd([[InlineKeyboardButton(text="Открыть", callback_data=f"a:tk:{tid}")]]))
        except Exception:
            pass


@usr.pre_checkout_query()
async def pre_checkout(q: PreCheckoutQuery):
    await q.answer(ok=True)


@usr.message(F.successful_payment)
async def paid(m: Message, bot: Bot):
    sp = m.successful_payment
    if sp.invoice_payload != "premium":
        return
    touch(m.from_user)
    uid = m.from_user.id
    until = grant_premium(uid, cfg("premium_days"))
    Q("INSERT INTO payments(user_id,stars,charge_id,ts) VALUES(?,?,?,?)",
      uid, sp.total_amount, sp.telegram_payment_charge_id, int(time.time()))
    inc("payments")
    log_act(uid, "pay")
    inc("stars", sp.total_amount)
    try:
        await m.delete()
    except Exception:
        pass
    await scr_premium(bot, uid, note=T("paid", until=fmt_until(until)))


@junk.message(F.chat.type == "private")
async def junk_msg(m: Message):
    try:
        await m.delete()  # чат остаётся чистым: живёт только закреплённое сообщение
    except Exception:
        pass


# ───────────────────────── АДМИН-ПАНЕЛЬ ─────────────────────────
async def arender(bot, uid, text, rows):
    kb = kbd([[InlineKeyboardButton(text=t, callback_data=d) for t, d in row] for row in rows])
    mid = sget(f"adm:{uid}")
    if mid:
        try:
            await bot.edit_message_text(text, chat_id=uid, message_id=int(mid), reply_markup=kb)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
    m = await bot.send_message(uid, text, reply_markup=kb)
    sset(f"adm:{uid}", m.message_id)


async def prompt(bot, uid, state, st, text, back, **data):
    await state.set_state(st)
    await state.update_data(prompt=text, back=back, **data)
    await arender(bot, uid, text, [[("↩️ Отмена", back)]])


async def admin_home(bot, uid, note=""):
    await arender(bot, uid, note + "<b>⚙️ Админ-панель Roza Search</b>", [
        [("👥 Пользователи", "a:usr"), ("📂 Категории", "a:cats")],
        [("📝 Тексты", "a:txt:0"), ("🎨 Кнопки", "a:btn")],
        [("🖼 Изображения", "a:img"), ("📣 Рассылка", "a:cast")],
        [("🎫 Тикеты", "a:tks"), ("⚙️ Настройки", "a:cfg")],
        [("📈 Активность", "a:act"), ("📊 Статистика", "a:stats")]])


def uname(r):
    return f"@{r['username']}" if r["username"] else (esc(r["first_name"]) or str(r["uid"]))


KIND = {"start": "▶️ /start", "search": "🔍 поиск", "found": "✅ нашёл", "pay": "⭐ оплата", "ticket": "🎫 тикет"}


def user_card(uid):
    r = ONE("SELECT * FROM users WHERE id=?", uid)
    prem = f"до {fmt_until(r['premium_until'])}" if is_premium(r) else "нет"
    uname_ = f"@{r['username']}" if r["username"] else "—"
    seen = datetime.fromtimestamp(r["last_active"]).strftime("%d.%m.%Y %H:%M") if r["last_active"] else "—"
    n_s = ONE("SELECT COUNT(*) FROM activity WHERE uid=? AND kind='search'", uid)[0]
    n_f = ONE("SELECT COUNT(*) FROM activity WHERE uid=? AND kind='found'", uid)[0]
    return (f"<b>👤 Пользователь</b>\nID: <code>{r['id']}</code>\nUsername: {uname_}\nИмя: {esc(r['first_name'])}\n"
            f"Premium: {prem}\nЗапросов: {r['requests']}\nБан: {'да' if r['banned'] else 'нет'}\n"
            f"Был(а): {seen}\nПоисков: {n_s} (найдено: {n_f})")


def activity_text():
    now = int(time.time())
    d1, d7, d30 = now - 86400, now - 7 * 86400, now - 30 * 86400
    c = lambda sql, *a: ONE(sql, *a)[0]
    online = c("SELECT COUNT(*) FROM users WHERE last_active>?", now - 300)
    a24 = c("SELECT COUNT(*) FROM users WHERE last_active>?", d1)
    a7 = c("SELECT COUNT(*) FROM users WHERE last_active>?", d7)
    a30 = c("SELECT COUNT(*) FROM users WHERE last_active>?", d30)
    n24 = c("SELECT COUNT(*) FROM users WHERE joined>?", d1)
    n7 = c("SELECT COUNT(*) FROM users WHERE joined>?", d7)

    def ev(kind, since):
        return c("SELECT COUNT(*) FROM activity WHERE kind=? AND ts>?", kind, since)
    s24, s7, f24, f7 = ev("search", d1), ev("search", d7), ev("found", d1), ev("found", d7)
    rate = f"{round(f24 * 100 / s24)}%" if s24 else "—"
    top = ALL("SELECT a.uid, COUNT(*) n, u.username, u.first_name FROM activity a LEFT JOIN users u ON u.id=a.uid "
              "WHERE a.kind='search' AND a.ts>? GROUP BY a.uid ORDER BY n DESC LIMIT 5", d7)
    last = ALL("SELECT a.ts, a.kind, a.uid, u.username, u.first_name FROM activity a LEFT JOIN users u ON u.id=a.uid "
               "ORDER BY a.id DESC LIMIT 10")
    top_s = "\n".join(f"{i}. {uname(r)} — {r['n']}" for i, r in enumerate(top, 1)) or "—"
    last_s = "\n".join(f"{datetime.fromtimestamp(r['ts']).strftime('%d.%m %H:%M')} {KIND.get(r['kind'], r['kind'])} · {uname(r)}"
                       for r in last) or "—"
    return (f"<b>📈 Активность</b>\n\n🟢 Онлайн (5 мин): {online}\n"
            f"👥 Активны: 24ч {a24} · 7д {a7} · 30д {a30}\n🆕 Новые: 24ч {n24} · 7д {n7}\n\n"
            f"🔍 Поиски: 24ч {s24} · 7д {s7}\n✅ Найдено: 24ч {f24} · 7д {f7}\n🎯 Успешность (24ч): {rate}\n"
            f"⭐ Оплаты: 24ч {ev('pay', d1)} · 7д {ev('pay', d7)}\n🎫 Тикеты: 24ч {ev('ticket', d1)} · 7д {ev('ticket', d7)}\n"
            f"▶️ Запуски: 24ч {ev('start', d1)}\n\n<b>Топ по поискам (7д)</b>\n{top_s}\n\n"
            f"<b>Последние события</b>\n{last_s}\n\n<i>Время сервера</i>")


async def adm_route(bot: Bot, uid, d, state: FSMContext, note=""):
    p = d.split(":", 2)
    cmd = p[1] if len(p) > 1 else "home"
    arg = p[2] if len(p) > 2 else ""
    home = [("↩️ В меню", "a:home")]
    N = note

    if cmd == "home":
        return await admin_home(bot, uid, N)

    if cmd == "stats":
        now = int(time.time())
        s = lambda sql, *a: ONE(sql, *a)[0]
        n_users = s("SELECT COUNT(*) FROM users")
        n_prem = s("SELECT COUNT(*) FROM users WHERE premium_until=-1 OR premium_until>?", now)
        n_blocked = s("SELECT COUNT(*) FROM users WHERE blocked=1")
        n_tickets = s("SELECT COUNT(*) FROM tickets WHERE status='open'")
        text = (f"<b>📊 Статистика</b>\n\nПользователей: {n_users}\n"
                f"Premium: {n_prem}\n"
                f"Заблокировали бота: {n_blocked}\n"
                f"Открытых тикетов: {n_tickets}\n"
                f"Поисков: {sget('c:searches', 0)}\nОплат: {sget('c:payments', 0)} (⭐ {sget('c:stars', 0)})")
        return await arender(bot, uid, N + text, [home])

    if cmd == "act":
        return await arender(bot, uid, N + activity_text(), [[("🔄 Обновить", "a:act")], home])

    # ── пользователи ──
    if cmd == "usr":
        return await arender(bot, uid, N + "<b>👥 Пользователи</b>", [
            [("🔎 Найти пользователя", "a:find")],
            [("⭐ Premium всем", "a:mp"), ("➕ Запросы всем", "a:mr")], home])
    if cmd == "find":
        return await prompt(bot, uid, state, AdmS.find, N + "Отправьте ID или @username пользователя:", "a:usr")
    if cmd == "u":
        i = int(arg)
        return await arender(bot, uid, N + user_card(i), [
            [("⭐ Выдать Premium", f"a:pg:{i}"), ("🚫 Забрать", f"a:pr:{i}")],
            [("➕ Запросы", f"a:rq:{i}"), ("⛔ Бан/разбан", f"a:bn:{i}")],
            [("↩️ Назад", "a:usr")]])
    if cmd == "pg":
        return await prompt(bot, uid, state, AdmS.num, "На сколько дней выдать Premium? (0 = навсегда)", f"a:u:{arg}",
                            action="prem_user", target=int(arg))
    if cmd == "rq":
        return await prompt(bot, uid, state, AdmS.num, "Сколько запросов добавить? (можно отрицательное)", f"a:u:{arg}",
                            action="req_user", target=int(arg))
    if cmd == "pr":
        Q("UPDATE users SET premium_until=0 WHERE id=?", int(arg))
        return await adm_route(bot, uid, f"a:u:{arg}", state, "✅ Premium снят\n\n")
    if cmd == "bn":
        Q("UPDATE users SET banned=1-banned WHERE id=?", int(arg))
        return await adm_route(bot, uid, f"a:u:{arg}", state, "✅ Готово\n\n")
    if cmd == "mp":
        return await prompt(bot, uid, state, AdmS.num, "На сколько дней выдать Premium ВСЕМ? (0 = навсегда)", "a:usr",
                            action="prem_all")
    if cmd == "mr":
        return await prompt(bot, uid, state, AdmS.num, "Сколько запросов выдать ВСЕМ?", "a:usr", action="req_all")

    # ── категории ──
    if cmd == "cats":
        rows = [[(f"{c['length']} · {c['title']} {'🔒' if c['premium'] else '🆓'}{'' if c['enabled'] else ' ⏸'}",
                  f"a:cat:{c['id']}")] for c in ALL("SELECT * FROM categories ORDER BY sort,length")]
        rows += [[("➕ Добавить категорию", "a:cn")], home]
        return await arender(bot, uid, N + "<b>📂 Категории поиска</b>", rows)
    if cmd == "cn":
        return await prompt(bot, uid, state, AdmS.cat_new, "Отправьте длину юзернейма числом (3-32):", "a:cats")
    if cmd == "cat":
        c = ONE("SELECT * FROM categories WHERE id=?", int(arg))
        if not c:
            return await adm_route(bot, uid, "a:cats", state)
        return await arender(bot, uid, N + f"<b>Категория #{c['id']}</b>\nДлина: {c['length']}\nНазвание: {esc(c['title'])}\n"
                             f"Только Premium: {'да' if c['premium'] else 'нет'}\nВключена: {'да' if c['enabled'] else 'нет'}", [
                                 [("✏️ Название", f"a:ctt:{c['id']}")],
                                 [("🔒 Premium вкл/выкл", f"a:ct:{c['id']}"), ("⏯ Вкл/выкл", f"a:ce:{c['id']}")],
                                 [("🗑 Удалить", f"a:cd:{c['id']}")], [("↩️ Назад", "a:cats")]])
    if cmd == "ctt":
        return await prompt(bot, uid, state, AdmS.cat_title, "Отправьте новое название кнопки:", f"a:cat:{arg}", cid=int(arg))
    if cmd == "ct":
        Q("UPDATE categories SET premium=1-premium WHERE id=?", int(arg))
        return await adm_route(bot, uid, f"a:cat:{arg}", state)
    if cmd == "ce":
        Q("UPDATE categories SET enabled=1-enabled WHERE id=?", int(arg))
        return await adm_route(bot, uid, f"a:cat:{arg}", state)
    if cmd == "cd":
        Q("DELETE FROM categories WHERE id=?", int(arg))
        return await adm_route(bot, uid, "a:cats", state, "🗑 Удалено\n\n")

    # ── тексты ──
    if cmd == "txt":
        page, per = int(arg or 0), 8
        keys = list(TEXTS)
        chunk = keys[page * per:(page + 1) * per]
        rows = [[(("✏️ " if sget("t:" + k) else "") + TEXTS[k][0], f"a:te:{k}")] for k in chunk]
        nav = []
        if page > 0:
            nav.append(("⬅️", f"a:txt:{page - 1}"))
        if (page + 1) * per < len(keys):
            nav.append(("➡️", f"a:txt:{page + 1}"))
        rows += ([nav] if nav else []) + [home]
        return await arender(bot, uid, N + "<b>📝 Тексты бота</b>\n✏️ — изменено вами", rows)
    if cmd == "te":
        cur = sget("t:" + arg) or TEXTS[arg][1]
        return await arender(bot, uid, N + f"<b>{esc(TEXTS[arg][0])}</b>\n\nСейчас:\n<code>{esc(cur)}</code>", [
            [("✏️ Изменить", f"a:tset:{arg}"), ("♻️ Сбросить", f"a:tr:{arg}")], [("↩️ Назад", "a:txt:0")]])
    if cmd == "tset":
        return await prompt(bot, uid, state, AdmS.text,
                            "Отправьте новый текст. Можно форматировать (жирный, курсив, моно) — сохранится как есть. "
                            "Подстановки в {фигурных скобках} (см. название) оставьте.", f"a:te:{arg}", key=arg)
    if cmd == "tr":
        sdel("t:" + arg)
        return await adm_route(bot, uid, f"a:te:{arg}", state, "♻️ Сброшено\n\n")

    # ── кнопки ──
    if cmd == "btn":
        rows = [[(BUTTONS[k][0], f"a:bk:{k}")] for k in BUTTONS]
        return await arender(bot, uid, N + "<b>🎨 Кнопки</b>", rows + [home])
    if cmd == "bk":
        txt = f"Текст: {esc(sget('b:' + arg) or BUTTONS[arg][1])}\n" if arg != "category" else "Текст: задаётся в «Категории»\n"
        rows = ([] if arg == "category" else [[("✏️ Текст", f"a:bt:{arg}")]]) + [
            [("🎨 Цвет", f"a:bc:{arg}"), ("♻️ Сброс", f"a:br:{arg}")],
            [("💎 Эмодзи-иконка", f"a:be:{arg}"), ("🚫 Без иконки", f"a:bx:{arg}")], [("↩️ Назад", "a:btn")]]
        return await arender(bot, uid, N + f"<b>{esc(BUTTONS[arg][0])}</b>\n{txt}Цвет: {STYLE_NAMES[sget('s:' + arg, '')]}\nИконка: {icon_label(arg)}\n\n"
                             "<i>Цвета видны в свежих версиях Telegram.</i>", rows)
    if cmd == "bt":
        return await prompt(bot, uid, state, AdmS.btn, "Отправьте новый текст кнопки (можно эмодзи):", f"a:bk:{arg}", key=arg)
    if cmd == "bc":
        cur = sget("s:" + arg, "")
        sset("s:" + arg, STYLES[(STYLES.index(cur) + 1) % len(STYLES)])
        return await adm_route(bot, uid, f"a:bk:{arg}", state)
    if cmd == "be":
        return await prompt(bot, uid, state, AdmS.emoji,
                            "Отправьте сообщение с премиум-эмодзи (или его числовой ID). «0» — убрать иконку.",
                            f"a:bk:{arg}", key=arg)
    if cmd == "bx":
        sset("e:" + arg, "0")
        return await adm_route(bot, uid, f"a:bk:{arg}", state, "✅ Иконка убрана\n\n")
    if cmd == "br":
        sdel("b:" + arg)
        sdel("s:" + arg)
        sdel("e:" + arg)
        return await adm_route(bot, uid, f"a:bk:{arg}", state, "♻️ Сброшено\n\n")

    # ── изображения ──
    if cmd == "img":
        rows = [[(("🖼 " if sget("i:" + k) else "▫️ ") + v, f"a:ik:{k}")] for k, v in SCREENS.items()]
        return await arender(bot, uid, N + "<b>🖼 Изображения экранов</b>\nЕсли задан общий баннер или хотя бы одна картинка, "
                             "сообщение становится фото с подписью. Рекомендую баннер для всех экранов.", rows + [home])
    if cmd == "ik":
        return await arender(bot, uid, N + f"<b>{SCREENS[arg]}</b>\nКартинка: {'задана' if sget('i:' + arg) else 'нет'}", [
            [("📷 Загрузить", f"a:iu:{arg}"), ("🗑 Убрать", f"a:id:{arg}")], [("↩️ Назад", "a:img")]])
    if cmd == "iu":
        return await prompt(bot, uid, state, AdmS.img, "Отправьте фото:", f"a:ik:{arg}", screen=arg)
    if cmd == "id":
        sdel("i:" + arg)
        return await adm_route(bot, uid, f"a:ik:{arg}", state, "🗑 Убрано\n\n")

    # ── рассылка ──
    if cmd == "cast":
        return await prompt(bot, uid, state, AdmS.cast,
                            "📣 Отправьте сообщение для рассылки (текст, фото с подписью, видео и т.д.):", "a:home")
    if cmd == "castc":
        n_all = ONE("SELECT COUNT(*) FROM users WHERE banned=0 AND blocked=0")[0]
        n_pr = ONE("SELECT COUNT(*) FROM users WHERE banned=0 AND blocked=0 AND (premium_until=-1 OR premium_until>?)", int(time.time()))[0]
        return await arender(bot, uid, N + "Кому отправить?", [
            [(f"Всем ({n_all})", "a:castgo:all")], [(f"Только Premium ({n_pr})", "a:castgo:prem")],
            [(f"Без Premium ({n_all - n_pr})", "a:castgo:free")], [("↩️ Отмена", "a:home")]])
    if cmd == "castgo":
        bg(do_cast(bot, uid, arg))
        return await arender(bot, uid, "🚀 Рассылка запущена, по окончании пришлю отчёт.", [home])

    # ── тикеты ──
    if cmd == "tks":
        rs = ALL("SELECT t.id, u.username, u.first_name, u.premium_until FROM tickets t JOIN users u ON u.id=t.user_id "
                 "WHERE t.status='open' ORDER BY (u.premium_until=-1 OR u.premium_until>?) DESC, t.id", int(time.time()))
        rows = [[(f"{'⭐ ' if (r['premium_until'] == -1 or r['premium_until'] > time.time()) else ''}#{r['id']} · "
                  f"{('@' + r['username']) if r['username'] else r['first_name']}", f"a:tk:{r['id']}")] for r in rs[:30]]
        return await arender(bot, uid, N + f"<b>🎫 Открытые тикеты: {len(rs)}</b>", rows + [home])
    if cmd == "tk":
        t = ONE("SELECT * FROM tickets WHERE id=?", int(arg))
        if not t:
            return await adm_route(bot, uid, "a:tks", state)
        rows = [[("💬 Ответить", f"a:tkr:{arg}"), ("✅ Закрыть", f"a:tkc:{arg}")],
                [("👤 Пользователь", f"a:u:{t['user_id']}"), ("↩️ Назад", "a:tks")]]
        return await arender(bot, uid, N + f"<b>🎫 Тикет #{arg}</b> ({'открыт' if t['status'] == 'open' else 'закрыт'})\n\n"
                             f"{dialog(int(arg), me='admin')}", rows)
    if cmd == "tkr":
        return await prompt(bot, uid, state, AdmS.reply, f"Напишите ответ по тикету #{arg}:", f"a:tk:{arg}", tid=int(arg))
    if cmd == "tkc":
        Q("UPDATE tickets SET status='closed' WHERE id=?", int(arg))
        return await adm_route(bot, uid, "a:tks", state, "✅ Тикет закрыт\n\n")

    # ── настройки ──
    if cmd == "cfg":
        rows = [[(f"{CFG[k][0]}: {cfg(k)}", f"a:cf:{k}")] for k in CFG]
        return await arender(bot, uid, N + "<b>⚙️ Настройки</b>", rows + [home])
    if cmd == "cf":
        return await prompt(bot, uid, state, AdmS.num, f"{CFG[arg][0]}\nТекущее: {cfg(arg)}\nОтправьте число:", "a:cfg",
                            action="cfg:" + arg)

    await admin_home(bot, uid, N)


@adm.callback_query(F.data.startswith("a:"))
async def a_cb(c: CallbackQuery, bot: Bot, state: FSMContext):
    await c.answer()
    touch(c.from_user)
    await adm_route(bot, c.from_user.id, c.data, state)


async def _del(m: Message):
    try:
        await m.delete()
    except Exception:
        pass


@adm.message(AdmS.find, F.text)
async def h_find(m: Message, bot: Bot, state: FSMContext):
    await _del(m)
    s = m.text.strip().lstrip("@")
    row = ONE("SELECT id FROM users WHERE id=?", int(s)) if s.isdigit() else ONE("SELECT id FROM users WHERE lower(username)=?", s.lower())
    if row:
        await state.clear()
        await adm_route(bot, m.from_user.id, f"a:u:{row['id']}", state)
    else:
        await adm_route(bot, m.from_user.id, "a:find", state, "❌ Не найден (он должен был запустить бота).\n\n")


@adm.message(AdmS.num, F.text)
async def h_num(m: Message, bot: Bot, state: FSMContext):
    await _del(m)
    d = await state.get_data()
    uid = m.from_user.id
    try:
        n = int(m.text.strip())
    except ValueError:
        return await arender(bot, uid, "❌ Нужно целое число.\n\n" + d["prompt"], [[("↩️ Отмена", d["back"])]])
    act, tgt, note = d["action"], d.get("target"), "✅ Готово\n\n"
    if act == "prem_user":
        until = grant_premium(tgt, n)
        note = f"✅ Premium: {fmt_until(until)}\n\n"
        await tmp_notice(bot, tgt, T("gift_notice", until=fmt_until(until)))
    elif act == "req_user":
        Q("UPDATE users SET requests=MAX(0,requests+?) WHERE id=?", n, tgt)
    elif act == "prem_all":
        if n <= 0:
            Q("UPDATE users SET premium_until=-1")
        else:
            Q("UPDATE users SET premium_until=CASE WHEN premium_until=-1 THEN -1 ELSE MAX(premium_until,?)+? END",
              int(time.time()), n * 86400)
        note = "✅ Premium выдан всем\n\n"
    elif act == "req_all":
        Q("UPDATE users SET requests=MAX(0,requests+?)", n)
        note = "✅ Запросы выданы всем\n\n"
    elif act.startswith("cfg:"):
        sset("c:" + act[4:], max(0, n))
    await state.clear()
    await adm_route(bot, uid, d["back"], state, note)


@adm.message(AdmS.cat_new, F.text)
async def h_cat_new(m: Message, bot: Bot, state: FSMContext):
    await _del(m)
    s = m.text.strip()
    if not s.isdigit() or not 3 <= int(s) <= 32:
        return await adm_route(bot, m.from_user.id, "a:cn", state, "❌ Число от 3 до 32.\n\n")
    cid = Q("INSERT INTO categories(length,title) VALUES(?,?)", int(s), f"{s} букв").lastrowid
    await state.clear()
    await adm_route(bot, m.from_user.id, f"a:cat:{cid}", state, "✅ Создана\n\n")


@adm.message(AdmS.cat_title, F.text)
async def h_cat_title(m: Message, bot: Bot, state: FSMContext):
    await _del(m)
    d = await state.get_data()
    Q("UPDATE categories SET title=? WHERE id=?", m.text.strip()[:40], d["cid"])
    await state.clear()
    await adm_route(bot, m.from_user.id, d["back"], state, "✅ Сохранено\n\n")


@adm.message(AdmS.text, F.text)
async def h_text(m: Message, bot: Bot, state: FSMContext):
    await _del(m)
    d = await state.get_data()
    sset("t:" + d["key"], m.html_text)
    await state.clear()
    await adm_route(bot, m.from_user.id, d["back"], state, "✅ Сохранено\n\n")


@adm.message(AdmS.btn, F.text)
async def h_btn(m: Message, bot: Bot, state: FSMContext):
    await _del(m)
    d = await state.get_data()
    sset("b:" + d["key"], m.text.strip()[:60])
    await state.clear()
    await adm_route(bot, m.from_user.id, d["back"], state, "✅ Сохранено\n\n")


@adm.message(AdmS.img, F.photo)
async def h_img(m: Message, bot: Bot, state: FSMContext):
    d = await state.get_data()
    sset("i:" + d["screen"], m.photo[-1].file_id)
    await _del(m)
    await state.clear()
    await adm_route(bot, m.from_user.id, d["back"], state, "✅ Картинка сохранена\n\n")


@adm.message(AdmS.emoji)
async def h_emoji(m: Message, bot: Bot, state: FSMContext):
    d = await state.get_data()
    eid = ""
    for e in (m.entities or []):
        if e.type == "custom_emoji":
            eid = e.custom_emoji_id
            break
    txt = (m.text or "").strip()
    if not eid and txt.isdigit():
        eid = "0" if txt == "0" else txt
    await _del(m)
    if not eid:
        return await arender(bot, m.from_user.id, "❌ Нужно премиум-эмодзи или ID.\n\n" + d["prompt"],
                             [[("↩️ Отмена", d["back"])]])
    sset("e:" + d["key"], eid)
    await state.clear()
    await adm_route(bot, m.from_user.id, d["back"], state, "✅ Иконка сохранена\n\n")


@adm.message(AdmS.reply, F.text)
async def h_reply(m: Message, bot: Bot, state: FSMContext):
    await _del(m)
    d = await state.get_data()
    t = ONE("SELECT * FROM tickets WHERE id=?", d["tid"])
    Q("INSERT INTO ticket_msgs(ticket_id,sender,text,ts) VALUES(?,?,?,?)", d["tid"], "admin", m.text[:2000], int(time.time()))
    await state.clear()
    if t:
        await tmp_notice(bot, t["user_id"], T("ticket_notice", id=d["tid"]))
    await adm_route(bot, m.from_user.id, d["back"], state, "✅ Ответ отправлен\n\n")


@adm.message(AdmS.cast)
async def h_cast(m: Message, bot: Bot, state: FSMContext):
    sset("c:cast_src", f"{m.chat.id}:{m.message_id}")  # само сообщение не удаляем: его копируем
    await state.clear()
    await adm_route(bot, m.from_user.id, "a:castc", state)


async def do_cast(bot: Bot, admin, aud):
    chat, mid = map(int, sget("c:cast_src").split(":"))
    where = "banned=0 AND blocked=0"
    if aud == "prem":
        where += f" AND (premium_until=-1 OR premium_until>{int(time.time())})"
    elif aud == "free":
        where += f" AND NOT (premium_until=-1 OR premium_until>{int(time.time())})"
    ids = [r["id"] for r in ALL(f"SELECT id FROM users WHERE {where}")]
    ok = fail = 0
    for i in ids:
        for attempt in (1, 2):
            try:
                await bot.copy_message(i, chat, mid)
                ok += 1
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
                continue
            except TelegramForbiddenError:
                Q("UPDATE users SET blocked=1 WHERE id=?", i)
                fail += 1
            except Exception:
                fail += 1
            break
        await asyncio.sleep(0.05)
    try:
        await bot.delete_message(chat, mid)
    except Exception:
        pass
    await arender(bot, admin, f"📣 Рассылка завершена\nДоставлено: {ok}\nНе доставлено: {fail}", [[("↩️ В меню", "a:home")]])


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    if not TOKEN:
        raise SystemExit("Задайте переменную окружения BOT_TOKEN")
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML, link_preview_is_disabled=True))
    dp = Dispatcher(storage=MemoryStorage())
    for r in (base, adm, usr, junk):
        dp.include_router(r)
    await bot.delete_webhook(drop_pending_updates=True)
    log.info("Roza Search started")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
