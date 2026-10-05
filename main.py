import os
import math
import asyncio
import logging
from pathlib import Path
from pypdf import PdfReader
import edge_tts

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

# ==================== الإعدادات ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
CHUNK_PAGES = 40
MAX_CHARS_PER_TTS = 1800
TEMP_DIR = Path("./bot_temp")
TEMP_DIR.mkdir(exist_ok=True)

ARABIC_VOICES = {
    "1": {"name": "حامد (سعودي ذكر)", "id": "ar-SA-HamedNeural"},
    "2": {"name": "زارية (سعودي أنثى)", "id": "ar-SA-ZariyahNeural"},
    "3": {"name": "سلمى (مصري أنثى)", "id": "ar-EG-SalmaNeural"},
    "4": {"name": "شاكر (مصري ذكر)", "id": "ar-EG-ShakirNeural"},
    "5": {"name": "حمدان (إماراتي ذكر)", "id": "ar-AE-HamdanNeural"},
    "6": {"name": "فاطمة (إماراتي أنثى)", "id": "ar-AE-FatimaNeural"},
    "7": {"name": "علي (بحريني ذكر)", "id": "ar-BH-AliNeural"},
    "8": {"name": "ليلى (بحريني أنثى)", "id": "ar-BH-LailaNeural"},
}

SELECT_VOICE, GENERATING = range(2)

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)


def clean_user_files(user_id: int):
    for f in TEMP_DIR.glob(f"{user_id}_*"):
        try:
            f.unlink()
        except:
            pass


def extract_text_from_pdf(pdf_path: str, start_page: int, end_page: int) -> str:
    reader = PdfReader(pdf_path)
    total = len(reader.pages)
    end_page = min(end_page, total)
    texts = []
    for i in range(start_page, end_page):
        try:
            page_text = reader.pages[i].extract_text()
            if page_text and page_text.strip():
                texts.append(page_text.strip())
        except Exception as e:
            logger.warning(f"Page {i}: {e}")
    return "\n\n".join(texts)


def split_text(text: str, max_chars: int = MAX_CHARS_PER_TTS):
    if len(text) <= max_chars:
        return [text]
    chunks = []
    current = ""
    for p in text.split("\n\n"):
        if len(current) + len(p) + 2 <= max_chars:
            current += p + "\n\n"
        else:
            if current:
                chunks.append(current.strip())
            while len(p) > max_chars:
                chunks.append(p[:max_chars])
                p = p[max_chars:]
            current = p + "\n\n"
    if current.strip():
        chunks.append(current.strip())
    return chunks


async def generate_tts(text: str, voice_id: str, output_path: str):
    communicate = edge_tts.Communicate(text, voice_id)
    await communicate.save(output_path)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clean_user_files(update.effective_user.id)
    context.user_data.clear()
    text = (
        "📚 **مرحباً بك في بوت قارئ الكتب!**\n\n"
        "أرسل ملف PDF واختر الصوت وسأقرأه لك جزءاً بجزء.\n\n"
        "/start - إعادة البدء\n/cancel - إلغاء"
    )
    await update.message.reply_text(text, parse_mode="Markdown")
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clean_user_files(update.effective_user.id)
    context.user_data.clear()
    await update.message.reply_text("✅ تم الإلغاء.")
    return ConversationHandler.END


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = update.message.document
    if not doc.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ أرسل ملف PDF فقط.")
        return ConversationHandler.END

    user_id = update.effective_user.id
    clean_user_files(user_id)

    status = await update.message.reply_text("📥 جاري تحميل الكتاب...")
    file = await context.bot.get_file(doc.file_id)
    pdf_path = TEMP_DIR / f"{user_id}_book.pdf"
    await file.download_to_drive(pdf_path)

    reader = PdfReader(str(pdf_path))
    total = len(reader.pages)

    context.user_data.update({
        "pdf_path": str(pdf_path),
        "total_pages": total,
        "current_page": 0,
    })

    buttons = [[InlineKeyboardButton(v["name"], callback_data=f"voice_{k}")] for k, v in ARABIC_VOICES.items()]
    await status.edit_text(
        f"✅ تم استلام الكتاب\n📄 الصفحات: **{total}**\n\nاختر الصوت:",
        reply_markup=InlineKeyboardMarkup(buttons),
        parse_mode="Markdown"
    )
    return SELECT_VOICE


async def set_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.replace("voice_", "")
    voice = ARABIC_VOICES[key]
    context.user_data["selected_voice"] = voice["id"]
    context.user_data["voice_name"] = voice["name"]
    await query.edit_message_text(f"🔊 تم اختيار: **{voice['name']}**\nجاري البدء...", parse_mode="Markdown")
    return await generate_next_chunk(query.message, context)


async def generate_next_chunk(message, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    if not data.get("pdf_path"):
        await message.reply_text("❌ انتهت الجلسة. أرسل /start")
        return ConversationHandler.END

    user_id = message.chat_id
    start = data["current_page"]
    total = data["total_pages"]
    end = min(start + CHUNK_PAGES, total)
    part = (start // CHUNK_PAGES) + 1

    status = await message.reply_text(f"⏳ جاري معالجة الجزء {part} (صفحات {start+1} → {end})")

    try:
        text = extract_text_from_pdf(data["pdf_path"], start, end)
        if not text.strip():
            await status.edit_text("⚠️ لا يوجد نص في هذه الصفحات.")
            data["current_page"] = end
            return GENERATING

        chunks = split_text(text)
        files = []
        for i, chunk in enumerate(chunks):
            path = TEMP_DIR / f"{user_id}_{i}.mp3"
            await generate_tts(chunk, data["selected_voice"], str(path))
            files.append(path)

        # دمج بسيط
        final = TEMP_DIR / f"{user_id}_part_{part}.mp3"
        with open(final, "wb") as out:
            for f in files:
                with open(f, "rb") as inp:
                    out.write(inp.read())
                f.unlink(missing_ok=True)

        data["current_page"] = end
        has_next = end < total

        kb = []
        if has_next:
            kb.append([InlineKeyboardButton("▶️ الجزء التالي", callback_data="next")])
        kb.append([InlineKeyboardButton("❌ إلغاء", callback_data="cancel")])

        caption = f"📖 الجزء {part} | {data.get('voice_name')}\n📄 {start+1} → {end} من {total}"

        await status.delete()
        with open(final, "rb") as audio:
            await message.reply_audio(audio=audio, caption=caption, reply_markup=InlineKeyboardMarkup(kb))

    except Exception as e:
        logger.exception(e)
        await status.edit_text(f"❌ خطأ: {str(e)[:120]}")

    return GENERATING


async def next_chunk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await generate_next_chunk(query.message, context)


async def cancel_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    clean_user_files(query.from_user.id)
    context.user_data.clear()
    await query.edit_message_text("✅ تم الإلغاء.")
    return ConversationHandler.END


def main():
    if not BOT_TOKEN:
        print("BOT_TOKEN missing")
        return
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Document.MimeType("application/pdf"), handle_pdf),
            CommandHandler("start", start_command),
        ],
        states={
            SELECT_VOICE: [CallbackQueryHandler(set_voice, pattern="^voice_")],
            GENERATING: [
                CallbackQueryHandler(next_chunk, pattern="^next$"),
                CallbackQueryHandler(cancel_all, pattern="^cancel$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel_command), CommandHandler("start", start_command)],
        allow_reentry=True,
    )

    app.add_handler(conv)
    print("🤖 البوت يعمل...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
