
import os
import logging
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)
TOKEN = os.getenv("BOT_TOKEN")

def menu():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🔎 Оценить подарок", callback_data="price")],
        [InlineKeyboardButton("📊 Сравнить цены", callback_data="compare")],
        [InlineKeyboardButton("💎 Редкие подарки", callback_data="rare")],
        [InlineKeyboardButton("🏆 Топ подарков", callback_data="top")],
        [InlineKeyboardButton("ℹ️ Помощь", callback_data="help")]
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💎 Добро пожаловать в GiftScope!\n\n"
        "Анализатор NFT-подарков Telegram.\n"
        "Выбери нужный раздел:",
        reply_markup=menu()
    )

async def buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    messages = {
        "price": "🔎 Пришли название или ссылку на NFT-подарок. Подготовим оценку.",
        "compare": "📊 Сравнение цен скоро будет доступно.",
        "rare": "💎 Раздел редких подарков скоро будет доступен.",
        "top": "🏆 Рейтинг дорогих подарков скоро будет доступен.",
        "help": "ℹ️ GiftScope — помощник для анализа Telegram NFT-подарков."
    }

    await query.message.reply_text(
        messages.get(query.data, "Выбери раздел:"),
        reply_markup=menu()
    )

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎁 Получил твой запрос!\n"
        "Автоматическая оценка стоимости пока настраивается.\n"
        "Скоро подключим источники цен.",
        reply_markup=menu()
    )

def main():
    if not TOKEN:
        raise RuntimeError("Не задан BOT_TOKEN")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(buttons))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
