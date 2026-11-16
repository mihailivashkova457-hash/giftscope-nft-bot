from contextlib import contextmanager
import os
import json
import re
import logging
import asyncio
import sqlite3
from statistics import median

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen
from urllib.parse import quote, urlencode
from urllib.error import HTTPError, URLError

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# =====================================
# CONFIG
# =====================================

PRODUCT_NAME = "GIFTS HUNTER"
PRODUCT_VERSION = "1.0.3"

TOKEN = os.getenv("BOT_TOKEN")
API = "https://starprice.net/api/v1/gifts"
PORTALS_API = "https://portal-market.com/partners"
PORTALS_API_KEY = os.getenv("PORTALS_API_KEY")
# Keep the legacy default filename to preserve existing history and alerts.
DB_PATH = os.getenv("DB_PATH", "tonhunter.db")
CHECK_INTERVAL = 300

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("GiftsHunter")


# =====================================
# DATABASE
# =====================================




@contextmanager
def database():
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()



def init_db():
    folder = os.path.dirname(os.path.abspath(DB_PATH))
    os.makedirs(folder, exist_ok=True)

    with database() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                gift TEXT NOT NULL,
                model TEXT DEFAULT '',
                price REAL,
                rub REAL,
                currency TEXT,
                recorded_at TEXT NOT NULL
            )
        """)

        conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                gift TEXT NOT NULL,
                model TEXT DEFAULT '',
                target REAL NOT NULL,
                active INTEGER DEFAULT 1,
                created_at TEXT NOT NULL
            )
        """)

        conn.commit()


def save_history(gift, model, floor):
    if not isinstance(floor, dict):
        return

    price = floor.get("price")

    if price is None:
        return

    with database() as conn:
        conn.execute("""
            INSERT INTO history
            (gift, model, price, rub, currency, recorded_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (
            gift,
            model or "",
            float(price),
            float(floor["rub"]) if floor.get("rub") is not None else None,
            floor.get("currency", ""),
            datetime.now(timezone.utc).isoformat()
        ))
        conn.commit()


def get_history(gift, model):
    with database() as conn:
        return conn.execute("""
            SELECT * FROM history
            WHERE lower(gift)=lower(?)
            AND lower(model)=lower(?)
            ORDER BY id DESC LIMIT 15
        """, (gift, model or "")).fetchall()


def add_alert(chat_id, gift, model, target):
    with database() as conn:
        cur = conn.execute("""
            INSERT INTO alerts
            (chat_id, gift, model, target, active, created_at)
            VALUES (?, ?, ?, ?, 1, ?)
        """, (
            chat_id,
            gift,
            model or "",
            target,
            datetime.now(timezone.utc).isoformat()
        ))
        conn.commit()
        return cur.lastrowid


def get_alerts(chat_id):
    with database() as conn:
        return conn.execute("""
            SELECT * FROM alerts
            WHERE chat_id=? AND active=1
            ORDER BY id DESC
        """, (chat_id,)).fetchall()


def all_alerts():
    with database() as conn:
        return conn.execute("""
            SELECT * FROM alerts WHERE active=1
        """).fetchall()


def remove_alert(alert_id, chat_id):
    with database() as conn:
        cur = conn.execute("""
            UPDATE alerts SET active=0
            WHERE id=? AND chat_id=?
        """, (alert_id, chat_id))
        conn.commit()
        return cur.rowcount > 0


# =====================================
# API
# =====================================

def get_json(path=""):
    request = Request(
        API + path,
        headers={
            "User-Agent": "GiftsHunter/1.0.1",
            "Accept": "application/json"
        }
    )

    with urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


async def api_request(path=""):
    try:
        return await asyncio.to_thread(get_json, path)

    except HTTPError as e:
        if e.code == 404:
            return {"error": "not_found"}
        if e.code == 429:
            return {"error": "rate_limit"}

        logger.exception("API HTTP error")
        return {"error": "api_error"}

    except (URLError, TimeoutError, ValueError, OSError):
        logger.exception("API connection error")
        return {"error": "connection"}

    except Exception:
        logger.exception("Unexpected API error")
        return {"error": "api_error"}


def api_path(gift, model=None):
    path = "/" + quote(gift.strip(), safe="")

    if model:
        path += "/" + quote(model.strip(), safe="")

    return path



# =====================================
# PORTALS SEARCH BY GIFT NUMBER
# =====================================

def get_portals_json(params):
    if not PORTALS_API_KEY:
        return {"error": "portals_missing_key"}

    url = PORTALS_API + "/nfts/search?" + urlencode(params)
    request = Request(
        url,
        headers={
            "User-Agent": "GiftsHunter/1.0.1",
            "Accept": "application/json",
            "Authorization": f"partners {PORTALS_API_KEY}",
        },
    )

    try:
        with urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode("utf-8"))
        except Exception:
            payload = {}

        code = payload.get("code", "")
        if exc.code == 401:
            return {"error": "portals_auth"}
        if exc.code == 403:
            return {"error": "portals_access", "details": payload.get("message", code)}
        if exc.code == 429:
            return {"error": "portals_rate_limit"}
        logger.warning("Portals HTTP %s: %s", exc.code, payload)
        return {"error": "portals_api"}
    except (URLError, TimeoutError, ValueError, OSError):
        logger.exception("Portals connection error")
        return {"error": "portals_connection"}


async def portals_search_number(number_value, collection=None):
    """Search listed Portals NFTs by exact collection number.

    Portals treats external_collection_number as a prefix filter, so exact
    matching is applied locally after fetching each page.
    """
    if not PORTALS_API_KEY:
        return {"error": "portals_missing_key"}

    params = {
        "external_collection_number": str(number_value),
        "limit": 100,
        "offset": 0,
        "sort_by": "price asc",
        "status": "listed",
    }
    if collection:
        params["name_filter"] = collection

    found = []
    offset = 0
    max_pages = 5

    for page in range(max_pages):
        page_params = dict(params, offset=offset)
        data = await asyncio.to_thread(get_portals_json, page_params)

        if isinstance(data, dict) and data.get("error"):
            return data
        if not isinstance(data, dict):
            return {"error": "portals_bad_response"}

        results = data.get("results", [])
        if not isinstance(results, list):
            return {"error": "portals_bad_response"}

        for item in results:
            if not isinstance(item, dict):
                continue
            try:
                item_number = int(item.get("external_collection_number"))
            except (TypeError, ValueError):
                continue

            # Avoid returning prefix collisions, e.g. 123 for a search of 12.
            if item_number == int(number_value):
                found.append(item)

        offset += len(results)
        total = data.get("total_count", 0)
        if len(results) < 100 or offset >= total or len(found) >= 20:
            break

        # The free Portals plan allows one request per second.
        await asyncio.sleep(1.05)

    return {"results": found[:20], "total_count": len(found)}



async def portals_fast_flip_scan(collection):
    """
    Снимок активных объявлений Portals.
    TON и GRAM считаются одной единицей (1 TON = 1 GRAM).
    Это оценка глубины предложения, а не подтверждённого оборота продаж.
    """
    if not PORTALS_API_KEY:
        return {"error": "portals_missing_key"}

    listings = []
    offset = 0
    max_pages = 3

    for page in range(max_pages):
        params = {
            "name_filter": collection,
            "limit": 100,
            "offset": offset,
            "sort_by": "price asc",
            "status": "listed",
        }
        data = await asyncio.to_thread(get_portals_json, params)

        if isinstance(data, dict) and data.get("error"):
            return data
        if not isinstance(data, dict) or not isinstance(data.get("results", []), list):
            return {"error": "portals_bad_response"}

        page_items = data.get("results", [])
        listings.extend(x for x in page_items if isinstance(x, dict))
        offset += len(page_items)

        total = data.get("total_count", 0)
        if len(page_items) < 100 or (isinstance(total, int) and offset >= total):
            break
        if page < max_pages - 1:
            await asyncio.sleep(1.05)

    priced = []
    skipped_currency = 0

    for item in listings:
        raw_price = item.get("price")
        try:
            price = Decimal(str(raw_price))
            if not price.is_finite() or price <= 0:
                continue
        except (InvalidOperation, ValueError, TypeError):
            continue

        raw_currency = str(
            item.get("currency") or item.get("price_currency") or "GRAM"
        ).strip().upper()

        # Portals отдаёт TON; в интерфейсе проекта это отображается как GRAM.
        if raw_currency not in ("TON", "GRAM"):
            skipped_currency += 1
            continue

        priced.append((price, "GRAM", item))

    if not priced:
        return {
            "results": [],
            "listing_count": len(listings),
            "priced_count": 0,
            "skipped_currency": skipped_currency,
        }

    prices = sorted(x[0] for x in priced)
    floor = prices[0]
    middle = len(prices) // 2

    if len(prices) % 2:
        median_price = prices[middle]
    else:
        median_price = (prices[middle - 1] + prices[middle]) / Decimal("2")

    listing_spread = median_price - floor
    listing_spread_pct = (
        listing_spread / floor * Decimal("100")
        if floor > 0 else Decimal("0")
    )

    # Глубина около флора: сколько лотов выставлено не дороже флора + 5% / 10%.
    near_5_limit = floor * Decimal("1.05")
    near_10_limit = floor * Decimal("1.10")
    near_5 = sum(1 for value in prices if value <= near_5_limit)
    near_10 = sum(1 for value in prices if value <= near_10_limit)

    count = len(priced)
    if count <= 5:
        competition = "Низкая"
    elif count <= 20:
        competition = "Средняя"
    else:
        competition = "Высокая"

    # Это только прокси ликвидности по глубине текущих объявлений.
    if near_10 >= 10 and count >= 20:
        liquidity = "Высокая глубина предложений"
    elif near_10 >= 4:
        liquidity = "Средняя глубина предложений"
    else:
        liquidity = "Низкая глубина предложений"

    sample = sorted(priced, key=lambda x: x[0])[:5]

    return {
        "collection": collection,
        "listing_count": len(listings),
        "priced_count": count,
        "skipped_currency": skipped_currency,
        "currency": "GRAM",
        "floor": floor,
        "median": median_price,
        "listing_spread": listing_spread,
        "listing_spread_pct": listing_spread_pct,
        "competition": competition,
        "near_5": near_5,
        "near_10": near_10,
        "liquidity": liquidity,
        "sample": sample,
    }


async def show_fast_flip(update, collection):
    await update.message.reply_text(
        f"⚡ Сканирую активные объявления Portals: {collection}..."
    )
    data = await portals_fast_flip_scan(collection)

    if isinstance(data, dict) and data.get("error"):
        if data["error"] == "portals_mixed_currency":
            await update.message.reply_text(
                "⚠️ Не удалось привести валюты объявлений к GRAM.",
                reply_markup=menu(),
            )
        else:
            await update.message.reply_text(
                portals_error_message(data["error"]), reply_markup=menu()
            )
        return

    if not data.get("priced_count"):
        await update.message.reply_text(
            f"По коллекции «{collection}» не найдено активных объявлений "
            "с ценой в GRAM.",
            reply_markup=menu(),
        )
        return

    floor = data["floor"]
    med = data["median"]
    spread = data["listing_spread"]
    pct = data["listing_spread_pct"]

    if pct >= Decimal("5"):
        signal = "🟢 Разрыв больше 5% — стоит проверить самый дешёвый лот"
    elif pct > 0:
        signal = "🟡 Небольшой разрыв — проверь комиссии и спрос"
    else:
        signal = "🔴 Выраженного ценового разрыва нет"

    lines = [
        f"⚡ FAST FLIP + LIQUIDITY — {collection}",
        "",
        f"📋 Активных объявлений получено: {data['listing_count']}",
        f"💰 Минимальная цена: {number(floor)} GRAM",
        f"📊 Медианная цена: {number(med)} GRAM",
        f"📈 Разница медиана/минимум: {number(spread)} GRAM ({number(pct, 2)}%)",
        "",
        f"🥊 Конкуренция: {data['competition']}",
        f"💧 Ликвидность по глубине объявлений: {data['liquidity']}",
        f"📦 Лотов в пределах +5% от флора: {data['near_5']}",
        f"📦 Лотов в пределах +10% от флора: {data['near_10']}",
        "",
        signal,
        "",
        "Пять самых дешёвых объявлений:",
    ]

    for price, _, item in data["sample"]:
        name = item.get("name") or collection
        item_number = item.get("external_collection_number")
        suffix = f" №{item_number}" if item_number is not None else ""
        lines.append(f"• {name}{suffix} — {number(price)} GRAM")

    if data.get("skipped_currency"):
        lines.extend([
            "",
            f"ℹ️ Пропущено объявлений в другой валюте: {data['skipped_currency']}",
        ])

    lines.extend([
        "",
        "⚠️ Важно: это анализ активных объявлений Portals, а не завершённых продаж.",
        "Глубина объявлений — лишь косвенный показатель ликвидности.",
        "Для настоящей ликвидности нужны объём и скорость завершённых продаж.",
        "Разница между медианой и флором не является гарантированной прибылью.",
        "Перед покупкой проверь сам лот, комиссии и возможность перепродажи.",
    ])

    await update.message.reply_text(
        "\n".join(lines), reply_markup=menu(), disable_web_page_preview=True
    )

def portals_error_message(error):
    messages = {
        "portals_missing_key": (
            "⚠️ Не найден PORTALS_API_KEY. Добавь ключ в Variables проекта Railway."
        ),
        "portals_auth": (
            "❌ Portals отклонил ключ. Проверь PORTALS_API_KEY в Railway."
        ),
        "portals_access": (
            "⛔ Portals не разрешил запрос. Проверь доступ и тариф Partner API."
        ),
        "portals_rate_limit": (
            "⏳ Portals ограничил частоту запросов. Попробуй немного позже."
        ),
        "portals_connection": "⚠️ Не удалось подключиться к Portals.",
        "portals_api": "⚠️ Ошибка Portals API.",
        "portals_bad_response": "⚠️ Portals вернул ответ неожиданного формата.",
    }
    return messages.get(error, "⚠️ Не удалось выполнить поиск в Portals.")


async def show_portals_number_search(update, number_value, collection=None):
    if not number_value.isdigit():
        await update.message.reply_text(
            "Номер должен состоять только из цифр. Примеры:\n"
            "1234\n"
            "Plush Pepe / 1234"
        )
        return

    await update.message.reply_text("🔎 Ищу подарок на Portals...")
    data = await portals_search_number(number_value, collection)

    if isinstance(data, dict) and data.get("error"):
        await update.message.reply_text(portals_error_message(data["error"]))
        return

    results = data.get("results", []) if isinstance(data, dict) else []
    if not results:
        scope = f" в коллекции «{collection}»" if collection else " во всех коллекциях"
        await update.message.reply_text(
            f"Подарок №{number_value}{scope} не найден среди выставленных на Portals."
        )
        return

    heading = (
        f"🎁 РЕЗУЛЬТАТЫ PORTALS\n"
        f"Номер: {number_value}\n"
        f"Коллекция: {collection or 'все коллекции'}\n"
        f"Найдено: {len(results)}"
    )
    await update.message.reply_text(
        heading,
        reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("💎 Открыть маркет Portals", url="https://portals.tg/")]
        ]),
        disable_web_page_preview=True,
    )

    for item in results[:20]:
        name = item.get("name", "Без названия")
        status = item.get("status", "неизвестно")
        price = item.get("price")
        floor_price = item.get("floor_price")
        item_number = item.get("external_collection_number", number_value)

        caption_lines = [
            f"🎁 {name} №{item_number}",
            f"💰 Цена: {money(price, 'GRAM') if price is not None else 'не выставлен'}",
            f"📉 Флор коллекции: {money(floor_price, 'GRAM')}",
            f"📌 Статус: {status}",
        ]

        attributes = item.get("attributes", [])
        if isinstance(attributes, list):
            for attribute in attributes:
                if not isinstance(attribute, dict):
                    continue
                attr_name = attribute.get("type") or attribute.get("name") or "Атрибут"
                attr_value = attribute.get("value") or attribute.get("name_value")
                if attr_value:
                    caption_lines.append(f"• {attr_name}: {attr_value}")

        tg_id = item.get("tg_id")
        buttons = []
        if isinstance(tg_id, str) and tg_id.strip():
            # Telegram gift deep links use the gift slug/identifier returned by Portals.
            safe_tg_id = tg_id.strip()
            if re.fullmatch(r"[A-Za-z0-9_-]+", safe_tg_id):
                buttons.append(InlineKeyboardButton(
                    "🎁 Открыть подарок в Telegram",
                    url=f"https://t.me/nft/{quote(safe_tg_id, safe='_-')}"
                ))

        reply_markup = InlineKeyboardMarkup([buttons]) if buttons else None
        photo_url = item.get("photo_url")
        caption = "\n".join(caption_lines)

        if isinstance(photo_url, str) and photo_url.startswith(("https://", "http://")):
            try:
                await update.message.reply_photo(
                    photo=photo_url,
                    caption=caption[:1024],
                    reply_markup=reply_markup,
                )
                continue
            except Exception:
                logger.warning("Could not send Portals gift photo; falling back to text", exc_info=True)

        await update.message.reply_text(
            caption,
            reply_markup=reply_markup,
            disable_web_page_preview=True,
        )

    await update.message.reply_text("Готово. Выбери следующий поиск или вернись в меню:", reply_markup=menu())


# =====================================
# FORMATTING
# =====================================

def number(value, decimals=6):
    if value is None:
        return "нет данных"

    try:
        value = Decimal(str(value))

        if not value.is_finite():
            return "нет данных"

        result = f"{value:,.{decimals}f}"
        result = result.rstrip("0").rstrip(".")

        return result.replace(",", " ").replace(".", ",")

    except (InvalidOperation, ValueError, TypeError):
        return str(value)


def display_currency(currency):
    """Единый формат проекта: TON и GRAM показываем как GRAM."""
    value = str(currency or "").strip().upper()
    if value in ("", "TON", "GRAM"):
        return "GRAM"
    return value


def money(value, currency=""):
    if value is None:
        return "нет данных в GRAM"

    shown_currency = display_currency(currency)
    if shown_currency != "GRAM":
        return "нет курса пересчёта в GRAM"

    return f"{number(value)} GRAM"


def rub_text(value):
    if value is None:
        return ""

    return f" (≈ {number(value, 2)} ₽)"


def floor_text(floor):
    if not isinstance(floor, dict):
        return "Нет активных предложений"

    result = money(
        floor.get("price"),
        floor.get("currency", "")
    )

    return result


def offers_text(offers):
    if not isinstance(offers, list) or not offers:
        return "Нет предложений"

    names = {
        "telegram": "Telegram",
        "fragment": "Fragment",
        "portals": "Portals"
    }

    lines = []

    for offer in offers:
        if not isinstance(offer, dict):
            continue

        market = names.get(
            offer.get("market"),
            offer.get("market", "Рынок")
        )

        price = money(
            offer.get("price"),
            offer.get("currency", "")
        )

        line = f"• {market}: {price}"

        if offer.get("lots") is not None:
            line += f"\n  Лотов: {offer['lots']}"

        if offer.get("url"):
            line += f"\n  Ссылка: {offer['url']}"

        lines.append(line)

    return "\n".join(lines) if lines else "Нет предложений"


def get_item(data):
    if not isinstance(data, dict):
        return {}, {}

    if isinstance(data.get("model"), dict):
        return data["model"], data.get("gift", {})

    return data, data


def error_message(error):
    errors = {
        "not_found": "❌ Подарок или модель не найдены.",
        "rate_limit": "⏳ Слишком много запросов. Попробуй позже.",
        "connection": "⚠️ Нет соединения с источником цен.",
        "api_error": "⚠️ Ошибка StarPrice API."
    }

    return errors.get(error, "Не удалось получить данные.")


# =====================================
# MENU
# =====================================

def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Оценить подарок", callback_data="price")],
        [InlineKeyboardButton("🔢 Поиск подарка по номеру (Portals)", callback_data="portals_number")],
        [InlineKeyboardButton("📊 Сравнить цены", callback_data="compare")],
        [InlineKeyboardButton("⚡ FAST FLIP + Liquidity Scanner", callback_data="fast_flip")],
        [InlineKeyboardButton("💎 Редкие подарки", callback_data="rare")],
        [InlineKeyboardButton("🏆 Топ подарков", callback_data="top")],
        [InlineKeyboardButton("📈 История цен", callback_data="history")],
        [InlineKeyboardButton("🔔 Уведомления", callback_data="alerts")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")]
    ])


# =====================================
# START
# =====================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()

    await update.message.reply_text(
        "💎 Добро пожаловать в Gifts Hunter!\n\n"
        "Анализатор NFT-подарков Telegram.\n\n"
        "🔎 Оценка стоимости\n"
        "🔢 Поиск подарка по номеру на Portals\n"
        "📊 Сравнение площадок\n"
        "💎 Редкость моделей\n"
        "🏆 Топ коллекций\n"
        "📈 История цен\n"
        "🔔 Уведомления о снижении цены\n\n"
        "Выбери нужный раздел:",
        reply_markup=menu()
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "ℹ️ GIFTS HUNTER\n\n"
        "/start — главное меню\n"
        "/alert — создать уведомление\n"
        "/alerts — мои уведомления\n"
        "/delalert ID — удалить уведомление\n"
        "/refresh — обновить цену\n"
        "FAST FLIP — анализ объявлений и глубины рынка Portals\n"
        "/help — помощь\n\n"
        "Поиск Portals по номеру:\n"
        "1234 — по всем коллекциям\n"
        "Plush Pepe / 1234 — в конкретной коллекции\n\n"
        "Для уведомления отправь:\n"
        "Plush Pepe / Frozen / 50000\n\n"
        "Цены TON/GRAM показываются только в GRAM (1:1).\n"
        "Другие валюты без надёжного курса не пересчитываются.\n"
        "Уведомления пока задаются в рублях.",
        reply_markup=menu()
    )


# =====================================
# PRICE AND MULTIPLE MARKETS
# =====================================

async def show_price(update, gift, model, mode):
    data = await api_request(api_path(gift, model))

    if isinstance(data, dict) and data.get("error"):
        await update.message.reply_text(
            error_message(data["error"]),
            reply_markup=menu()
        )
        return

    item, gift_info = get_item(data)

    if not item:
        await update.message.reply_text("Некорректный ответ API.")
        return

    gift_name = gift_info.get("name", gift)
    model_name = item.get("name", model or gift)

    title = gift_name

    if model:
        title += f" / {model_name}"

    floor = item.get("floor")
    offers = item.get("offers", [])

    save_history(gift_name, model_name if model else "", floor)

    lines = []

    if mode == "compare":
        lines.append("📊 СРАВНЕНИЕ ЦЕН")

    lines.extend([
        f"🎁 {title}",
        "",
        f"💰 Минимальная цена: {floor_text(floor)}",
        "",
        "📊 ПРЕДЛОЖЕНИЯ НА ПЛОЩАДКАХ:",
        offers_text(offers)
    ])

    if item.get("rarity_pct") is not None:
        lines.extend([
            "",
            f"💎 Редкость: {number(item['rarity_pct'], 4)}%"
        ])

    if item.get("rarity_permille") is not None:
        lines.append(
            f"Редкость: {number(item['rarity_permille'], 4)}‰"
        )

    if item.get("craft_rarity") is not None:
        lines.append(
            f"Крафтовая редкость: {item['craft_rarity']}"
        )

    if item.get("issued") is not None:
        lines.append(f"📦 Выпущено: {number(item['issued'], 0)}")

    if item.get("models_count") is not None:
        lines.append(f"🎨 Моделей: {item['models_count']}")

    lines.extend([
        "",
        "Источник: StarPrice",
        "https://starprice.net/gifts"
    ])

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=menu(),
        disable_web_page_preview=True
    )


# =====================================
# TOP
# =====================================

async def show_top(message):
    await message.reply_text("⏳ Загружаю топ коллекций...")

    data = await api_request()

    if isinstance(data, dict) and data.get("error"):
        await message.reply_text(error_message(data["error"]))
        return

    if isinstance(data, dict):
        items = data.get("gifts", data.get("data", []))
    else:
        items = data

    if not isinstance(items, list):
        await message.reply_text("Некорректный каталог.")
        return

    items = [
        x for x in items
        if isinstance(x, dict)
        and isinstance(x.get("floor"), dict)
        and x["floor"].get("rub") is not None
    ]

    items.sort(
        key=lambda x: float(x["floor"]["rub"]),
        reverse=True
    )

    lines = ["🏆 ТОП КОЛЛЕКЦИЙ ПО ЦЕНЕ\n"]

    for i, item in enumerate(items[:10], 1):
        lines.append(
            f"{i}. {item.get('name', 'Без названия')}\n"
            f"Флор: {floor_text(item.get('floor'))}"
        )

    if not items:
        lines.append("Пока нет доступных цен.")

    await message.reply_text(
        "\n\n".join(lines),
        reply_markup=menu()
    )


# =====================================
# RARITY
# =====================================

async def show_rare(update, gift):
    data = await api_request(api_path(gift))

    if isinstance(data, dict) and data.get("error"):
        await update.message.reply_text(error_message(data["error"]))
        return

    if not isinstance(data, dict):
        await update.message.reply_text("Некорректный ответ API.")
        return

    models = data.get("models", [])

    if not isinstance(models, list):
        models = []

    models = [
        x for x in models
        if isinstance(x, dict)
    ]

    models.sort(
        key=lambda x: (
            float(x["rarity_pct"])
            if x.get("rarity_pct") is not None
            else float("inf")
        )
    )

    lines = [f"💎 РЕДКИЕ МОДЕЛИ: {data.get('name', gift)}\n"]

    for item in models[:15]:
        rarity = item.get("rarity_pct")

        rarity_value = (
            f"{number(rarity, 4)}%"
            if rarity is not None
            else item.get("craft_rarity", "нет данных")
        )

        lines.append(
            f"• {item.get('name', 'Без названия')}\n"
            f"  Редкость: {rarity_value}\n"
            f"  Флор: {floor_text(item.get('floor'))}"
        )

    if not models:
        lines.append("Данные о моделях отсутствуют.")

    await update.message.reply_text(
        "\n\n".join(lines),
        reply_markup=menu()
    )


# =====================================
# PRICE HISTORY
# =====================================

async def show_history(update, gift, model):
    data = await api_request(api_path(gift, model))

    if isinstance(data, dict) and data.get("error"):
        await update.message.reply_text(error_message(data["error"]))
        return

    item, gift_info = get_item(data)

    floor = item.get("floor")

    save_history(
        gift_info.get("name", gift),
        item.get("name", model or ""),
        floor
    )

    local = get_history(gift, model)

    lines = [
        f"📈 ИСТОРИЯ ЦЕН: {gift_info.get('name', gift)}"
    ]

    if model:
        lines[0] += f" / {item.get('name', model)}"

    api_history = item.get("history")

    if not isinstance(api_history, list):
        api_history = []

    if api_history:
        lines.append("\nИстория StarPrice:")

        for entry in api_history[-10:]:
            if not isinstance(entry, dict):
                continue

            lines.append(
                f"\n📅 {entry.get('date', '?')}"
                f"\nFragment: {money(entry.get('fragment'), 'GRAM')}"
                f"\nPortals: {money(entry.get('portals'), 'GRAM')}"
            )

    if local:
        lines.append("\n📊 Наблюдения Gifts Hunter:")

        for row in reversed(local):
            date = row["recorded_at"][:16].replace("T", " ")

            price = money(row["price"], row["currency"])


            lines.append(f"\n{date}: {price}")

    if not local and not api_history:
        lines.append(
            "\nИстория пока накапливается. "
            "Проверяй подарок повторно."
        )

    await update.message.reply_text(
        "\n".join(lines),
        reply_markup=menu()
    )


# =====================================
# ALERTS
# =====================================

async def create_alert_from_text(update, text):
    parts = [x.strip() for x in text.split("/")]

    if len(parts) != 3:
        await update.message.reply_text(
            "Формат:\n"
            "Plush Pepe / Frozen / 50000\n\n"
            "Последнее число — целевая цена в рублях (уведомления пока работают в ₽)."
        )
        return

    gift, model, target_text = parts

    model = "" if model in ("", "-") else model

    try:
        target = float(
            target_text.replace(" ", "").replace(",", ".")
        )

        if target <= 0:
            raise ValueError

    except ValueError:
        await update.message.reply_text("Некорректная целевая цена.")
        return

    alert_id = add_alert(
        update.effective_chat.id,
        gift,
        model,
        target
    )

    await update.message.reply_text(
        "🔔 УВЕДОМЛЕНИЕ СОЗДАНО!\n\n"
        f"ID: {alert_id}\n"
        f"Подарок: {gift}\n"
        f"Модель: {model or 'Вся коллекция'}\n"
        f"Целевая цена: {number(target, 2)} ₽\n\n"
        "Проверка выполняется автоматически каждые 5 минут.",
        reply_markup=menu()
    )


async def show_alerts(message, chat_id):
    alerts = get_alerts(chat_id)

    if not alerts:
        await message.reply_text(
            "🔔 Активных уведомлений нет.\n\n"
            "Для создания используй /alert."
        )
        return

    lines = ["🔔 ТВОИ УВЕДОМЛЕНИЯ\n"]

    for alert in alerts:
        lines.append(
            f"ID: {alert['id']}\n"
            f"Подарок: {alert['gift']}\n"
            f"Модель: {alert['model'] or 'Вся коллекция'}\n"
            f"Цена: {number(alert['target'], 2)} ₽\n"
            f"Удалить: /delalert {alert['id']}"
        )

    await message.reply_text(
        "\n\n".join(lines),
        reply_markup=menu()
    )


async def monitor_alerts(application):
    while True:
        try:
            alerts = all_alerts()

            for alert in alerts:
                try:
                    data = await api_request(
                        api_path(alert["gift"], alert["model"])
                    )

                    if not isinstance(data, dict) or data.get("error"):
                        continue

                    item, gift_info = get_item(data)
                    floor = item.get("floor")

                    if not isinstance(floor, dict):
                        continue

                    rub = floor.get("rub")

                    if rub is None:
                        continue

                    current = float(rub)

                    save_history(
                        gift_info.get("name", alert["gift"]),
                        item.get("name", alert["model"]),
                        floor
                    )

                    if current <= alert["target"]:
                        # Deactivate before sending to prevent duplicates.
                        with database() as conn:
                            conn.execute(
                                "UPDATE alerts SET active=0 WHERE id=?",
                                (alert["id"],)
                            )
                            conn.commit()

                        await application.bot.send_message(
                            chat_id=alert["chat_id"],
                            text=(
                                "🚨 ЦЕНА ДОСТИГЛА ТВОЕЙ ЦЕЛИ!\n\n"
                                f"🎁 {alert['gift']}\n"
                                f"Модель: {alert['model'] or 'Вся коллекция'}\n"
                                f"Текущая цена: {number(current, 2)} ₽\n"
                                f"Твоя цель: {number(alert['target'], 2)} ₽\n\n"
                                "Источник: https://starprice.net/gifts"
                            ),
                            disable_web_page_preview=True
                        )

                except Exception:
                    logger.exception("Alert error: %s", alert["id"])

        except Exception:
            logger.exception("Monitor error")

        await asyncio.sleep(CHECK_INTERVAL)


async def post_init(application):
    application.bot_data["monitor_task"] = asyncio.create_task(
        monitor_alerts(application)
    )


async def post_shutdown(application):
    task = application.bot_data.get("monitor_task")

    if task:
        task.cancel()

        try:
            await task
        except asyncio.CancelledError:
            pass


# =====================================
# COMMANDS
# =====================================

async def alert_command(update, context):
    context.user_data["mode"] = "alert"

    await update.message.reply_text(
        "🔔 СОЗДАНИЕ УВЕДОМЛЕНИЯ\n\n"
        "Отправь:\n"
        "Plush Pepe / Frozen / 50000\n\n"
        "Последнее число — цена в рублях.\n"
        "Для всей коллекции используй:\n"
        "Plush Pepe / - / 50000",
        reply_markup=menu()
    )


async def alerts_command(update, context):
    await show_alerts(update.message, update.effective_chat.id)


async def delalert_command(update, context):
    if not context.args:
        await update.message.reply_text(
            "Пример: /delalert 3"
        )
        return

    try:
        alert_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом.")
        return

    if remove_alert(alert_id, update.effective_chat.id):
        await update.message.reply_text("✅ Уведомление удалено.")
    else:
        await update.message.reply_text("Уведомление не найдено.")


async def refresh_command(update, context):
    context.user_data["mode"] = "price"

    await update.message.reply_text(
        "⚡ Напиши подарок для обновления цены.\n\n"
        "Пример: Plush Pepe / Frozen",
        reply_markup=menu()
    )


# =====================================
# BUTTONS
# =====================================

async def buttons(update, context):
    query = update.callback_query
    await query.answer()

    action = query.data

    if action == "top":
        context.user_data["mode"] = None
        await show_top(query.message)
        return

    if action == "alerts":
        context.user_data["mode"] = None
        await show_alerts(query.message, query.message.chat_id)
        await query.message.reply_text(
            "Создать новое уведомление: /alert",
            reply_markup=menu()
        )
        return

    actions = {
        "portals_number": (
            "portals_number",
            "🔢 Отправь номер подарка для поиска по всем коллекциям Portals.\n\n"
            "Или укажи коллекцию и номер:\n"
            "Plush Pepe / 1234"
        ),
        "price": (
            "price",
            "🔎 Напиши название подарка или модели.\n\n"
            "Пример: Plush Pepe / Frozen"
        ),
        "compare": (
            "compare",
            "📊 Напиши подарок для сравнения площадок.\n\n"
            "Пример: Plush Pepe / Frozen"
        ),
        "fast_flip": (
            "fast_flip",
            "⚡ FAST FLIP + LIQUIDITY SCANNER\n\n"
            "Напиши название коллекции для анализа активных объявлений Portals.\n"
            "Пример: Plush Pepe\n\n"
            "Будут рассчитаны минимум, медиана, число конкурирующих объявлений "
            "и разницу между медианной и минимальной ценой."
        ),
        "rare": (
            "rare",
            "💎 Напиши название коллекции.\n\n"
            "Например: Plush Pepe"
        ),
        "history": (
            "history",
            "📈 Напиши подарок для просмотра истории.\n\n"
            "Пример: Plush Pepe / Frozen"
        ),
        "help": (
            None,
            "ℹ️ Gifts Hunter\n\n"
            "Анализатор NFT-подарков Telegram.\n"
            "Команды: /start /alert /alerts /delalert /refresh"
        )
    }

    if action not in actions:
        return

    mode, message = actions[action]
    context.user_data["mode"] = mode

    await query.message.reply_text(
        message,
        reply_markup=menu()
    )


# =====================================
# TEXT HANDLER
# =====================================

async def text_handler(update, context):
    mode = context.user_data.get("mode")

    if not mode:
        await update.message.reply_text(
            "Выбери функцию в меню 👇",
            reply_markup=menu()
        )
        return

    text = update.message.text.strip()

    if mode == "alert":
        context.user_data["mode"] = None
        await create_alert_from_text(update, text)
        return

    if mode == "portals_number":
        context.user_data["mode"] = None
        parts = text.split("/", 1)
        if len(parts) == 2:
            collection = parts[0].strip()
            number_value = parts[1].strip()
            if not collection:
                await update.message.reply_text("Укажи название коллекции.")
                return
        else:
            collection = None
            number_value = text.strip()

        await show_portals_number_search(update, number_value, collection)
        return

    parts = text.split("/", 1)

    gift = parts[0].strip()
    model = parts[1].strip() if len(parts) > 1 else None

    if not gift:
        await update.message.reply_text("Напиши название подарка.")
        return

    context.user_data["mode"] = None

    if mode != "fast_flip":
        await update.message.reply_text("⏳ Получаю данные...")

    if mode == "fast_flip":
        await show_fast_flip(update, gift)

    elif mode == "rare":
        await show_rare(update, gift)

    elif mode == "history":
        await show_history(update, gift, model)

    elif mode in ("price", "compare"):
        await show_price(update, gift, model, mode)

    else:
        await update.message.reply_text(
            "Неизвестный режим. Используй меню.",
            reply_markup=menu()
        )


# =====================================
# MAIN
# =====================================

def main():
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")

    init_db()

    app = (
        Application.builder()
        .token(TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("alert", alert_command))
    app.add_handler(CommandHandler("alerts", alerts_command))
    app.add_handler(CommandHandler("delalert", delalert_command))
    app.add_handler(CommandHandler("refresh", refresh_command))

    app.add_handler(CallbackQueryHandler(buttons))

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    logger.info("Gifts Hunter starting...")

    app.run_polling()


if __name__ == "__main__":
    main()
