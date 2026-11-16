from contextlib import contextmanager
import os
import json
import logging
import asyncio
import sqlite3

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from urllib.request import Request, urlopen
from urllib.parse import quote
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

TOKEN = os.getenv("BOT_TOKEN")
API = "https://starprice.net/api/v1/gifts"
DB_PATH = os.getenv("DB_PATH", "tonhunter.db")
CHECK_INTERVAL = 300

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("TonHunter")


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
            "User-Agent": "TonHunter/2.0",
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


def money(value, currency=""):
    if value is None:
        return "нет данных"

    return f"{number(value)} {currency}".strip()


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

    return result + rub_text(floor.get("rub"))


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

        price += rub_text(offer.get("rub"))

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
        [InlineKeyboardButton("📊 Сравнить цены", callback_data="compare")],
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
        "💎 Добро пожаловать в Ton Hunter 2.0!\n\n"
        "Анализатор NFT-подарков Telegram.\n\n"
        "🔎 Оценка стоимости\n"
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
        "ℹ️ TON HUNTER 2.0\n\n"
        "/start — главное меню\n"
        "/alert — создать уведомление\n"
        "/alerts — мои уведомления\n"
        "/delalert ID — удалить уведомление\n"
        "/refresh — обновить цену\n"
        "/help — помощь\n\n"
        "Для уведомления отправь:\n"
        "Plush Pepe / Frozen / 50000\n\n"
        "Последнее число — целевая цена в рублях.",
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
        lines.append("\n📊 Наблюдения Ton Hunter:")

        for row in reversed(local):
            date = row["recorded_at"][:16].replace("T", " ")

            price = money(row["price"], row["currency"])

            price += rub_text(row["rub"])

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
            "Последнее число — цена в рублях."
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
            "ℹ️ Ton Hunter 2.0\n\n"
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

    parts = text.split("/", 1)

    gift = parts[0].strip()
    model = parts[1].strip() if len(parts) > 1 else None

    if not gift:
        await update.message.reply_text("Напиши название подарка.")
        return

    context.user_data["mode"] = None

    await update.message.reply_text("⏳ Получаю данные...")

    if mode == "rare":
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

    logger.info("Ton Hunter 2.0 starting...")

    app.run_polling()


if __name__ == "__main__":
    main()
