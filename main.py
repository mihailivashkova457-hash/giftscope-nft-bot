
import os
import json
import logging
import asyncio
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

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

TOKEN = os.getenv("BOT_TOKEN")
API = "https://starprice.net/api/v1/gifts"


def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Оценить подарок", callback_data="price")],
        [InlineKeyboardButton("📊 Сравнить цены", callback_data="compare")],
        [InlineKeyboardButton("💎 Редкие подарки", callback_data="rare")],
        [InlineKeyboardButton("🏆 Топ подарков", callback_data="top")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")]
    ])


def get_json(path=""):
    url = API + path
    request = Request(
        url,
        headers={"User-Agent": "TonHunter/1.0"}
    )
    with urlopen(request, timeout=20) as response:
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



def money(value, currency):
    if value is None:
        return "нет данных"

    try:
        number = float(value)
        formatted = f"{number:,.2f}".rstrip("0").rstrip(".")
        formatted = formatted.replace(",", " ").replace(".", ",")
        return f"{formatted} {currency}".strip()
    except (ValueError, TypeError):
        return f"{value} {currency}".strip()



def floor_text(floor):
    if not floor:
        return "Нет активных предложений"

    result = money(floor.get("price"), floor.get("currency", ""))
    rub = floor.get("rub")

    if rub is not None:
        result += f" (≈ {money(rub, '₽')})"

    return result


def offers_text(offers):
    if not offers:
        return "Нет предложений"

    names = {
        "telegram": "Telegram",
        "fragment": "Fragment",
        "portals": "Portals"
    }

    lines = []

    for offer in offers:
        market = names.get(offer.get("market"), offer.get("market", "Рынок"))
        price = money(
            offer.get("price"),
            offer.get("currency", "")
        )

        rub = offer.get("rub")
        if rub is not None:
            result += f" (≈ {money(rub, '₽')})"

        lines.append(f"• {market}: {price}")

        if offer.get("lots") is not None:
            lines[-1] += f" | Лотов: {offer['lots']}"

        if offer.get("url"):
            lines[-1] += f"\n  {offer['url']}"

    return "\n".join(lines)


def split_gift(text):
    parts = text.split("/", 1)
    gift = parts[0].strip()
    model = parts[1].strip() if len(parts) > 1 else None
    return gift, model


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text(
        "💎 Добро пожаловать в Ton Hunter!\n\n"
        "Анализатор NFT-подарков Telegram.\n"
        "Выбери нужный раздел:",
        reply_markup=menu()
    )


async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    actions = {
        "price": (
            "price",
            "🔎 Напиши название коллекции или модели.\n\n"
            "Для модели используй формат:\n"
            "Plush Pepe / Frozen"
        ),
        "compare": (
            "compare",
            "📊 Напиши коллекцию или модель для сравнения цен.\n\n"
            "Пример:\n"
            "Plush Pepe / Frozen"
        ),
        "rare": (
            "rare",
            "💎 Напиши название коллекции, чтобы посмотреть редкие модели.\n\n"
            "Например: Plush Pepe"
        ),
        "top": (
            "top",
            None
        ),
        "help": (
            None,
            "ℹ️ Ton Hunter — анализатор NFT-подарков Telegram.\n\n"
            "Данные о ценах получаем через публичный API StarPrice.\n"
            "Цены являются справочными и могут изменяться."
        )
    }

    action = actions.get(query.data)

    if not action:
        return

    mode, message = action

    if query.data == "top":
        context.user_data["mode"] = None
        await query.message.reply_text(
            "⏳ Загружаю список коллекций..."
        )
        data = await api_request()

        if data.get("error"):
            await query.message.reply_text(
                "Не удалось получить данные. Попробуй позже.",
                reply_markup=menu()
            )
            return

        if isinstance(data, dict):
            items = data.get("gifts", data.get("data", []))
        else:
            items = data

        if not isinstance(items, list):
            items = []

        items = [
            item for item in items
            if isinstance(item, dict) and item.get("floor")
        ]

        items.sort(
            key=lambda x: (
                x["floor"].get("rub")
                if x["floor"].get("rub") is not None
                else -1
            ),
            reverse=True
        )

        lines = ["🏆 ТОП КОЛЛЕКЦИЙ ПО ЦЕНЕ\n"]

        for i, item in enumerate(items[:10], 1):
            lines.append(
                f"{i}. {item.get('name', 'Без названия')}\n"
                f"   Флор: {floor_text(item.get('floor'))}"
            )

        if not items:
            lines.append("Пока нет доступных цен.")

        await query.message.reply_text(
            "\n".join(lines),
            reply_markup=menu()
        )
        return

    context.user_data["mode"] = mode

    await query.message.reply_text(
        message,
        reply_markup=menu()
    )


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    mode = context.user_data.get("mode")

    if not mode:
        await update.message.reply_text(
            "Выбери функцию в меню 👇",
            reply_markup=menu()
        )
        return

    gift, model = split_gift(update.message.text)

    if not gift:
        await update.message.reply_text("Напиши название подарка.")
        return

    context.user_data["mode"] = None

    await update.message.reply_text("⏳ Ищу информацию о подарке...")

    path = "/" + quote(gift, safe="")

    if model:
        path += "/" + quote(model, safe="")

    data = await api_request(path)

    if data.get("error"):
        errors = {
            "not_found": (
                "❌ Подарок или модель не найдены.\n"
                "Проверь название и попробуй ещё раз."
            ),
            "rate_limit": "⏳ Слишком много запросов. Попробуй позже.",
            "connection": "⚠️ Не удалось подключиться к источнику цен.",
            "api_error": "⚠️ Ошибка источника данных."
        }

        await update.message.reply_text(
            errors.get(data["error"], "Не удалось получить данные."),
            reply_markup=menu()
        )
        return

    if mode == "rare":
        models = data.get("models", [])

        if not models and isinstance(data.get("model"), dict):
            models = [data["model"]]

        models = [
            item for item in models
            if item.get("rarity_pct") is not None
        ]

        models.sort(key=lambda x: x["rarity_pct"])

        lines = [
            f"💎 РЕДКИЕ МОДЕЛИ: {data.get('name', gift)}\n"
        ]

        for item in models[:10]:
            lines.append(
                f"• {item.get('name', 'Без названия')}\n"
                f"  Редкость: {item['rarity_pct']}%\n"
                f"  Флор: {floor_text(item.get('floor'))}"
            )

        if not models:
            lines.append("Информация о редкости пока отсутствует.")

        await update.message.reply_text(
            "\n".join(lines),
            reply_markup=menu()
        )
        return

    if isinstance(data.get("model"), dict):
        gift_info = data.get("gift", {})
        item = data["model"]
        gift_name = gift_info.get("name", gift)
        model_name = item.get("name", model or gift)
        title = f"{gift_name} / {model_name}"
    else:
        item = data
        title = data.get("name", gift)

    floor = item.get("floor")
    offers = item.get("offers", [])

    lines = [
        f"🎁 {title}",
        "",
        f"💰 Минимальная цена: {floor_text(floor)}",
        "",
        "📊 ЦЕНЫ НА ПЛОЩАДКАХ:",
        offers_text(offers)
    ]

    if item.get("rarity_pct") is not None:
        lines.extend([
            "",
            f"💎 Редкость: {item['rarity_pct']}%"
        ])

    if item.get("issued") is not None:
        lines.append(f"📦 Выпущено: {item['issued']}")

    if item.get("models_count") is not None:
        lines.append(f"🎨 Моделей: {item['models_count']}")

    if mode == "compare":
        lines.insert(0, "📊 СРАВНЕНИЕ ЦЕН\n")

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


def main():
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    logger.info("Ton Hunter is starting...")
    app.run_polling()


if __name__ == "__main__":
    main()
