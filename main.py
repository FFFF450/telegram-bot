import os
import math
import asyncio
import logging
import shutil
from pathlib import Path
from pypdf import PdfReader
import edge_tts
import yt_dlp
from pydub import AudioSegment

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler,
    CallbackQueryHandler, ContextTypes, filters, ConversationHandler
)

# ==================== الإعدادات ====================
BOT_TOKEN = "ضع توكن البوت هنا"
CHUNK_PAGES = 40          # عدد الصفحات لكل جزء (قللها لو الكتب معقدة)
MAX_CHARS_PER_TTS = 1800  # حد آمن لـ edge-tts
TEMP_DIR = Path("./bot_temp")
TEMP_DIR.mkdir(exist_ok=True)

# أصوات صحيحة ومحدثة
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

# حالات المحادثة
SELECT_VOICE, SELECT_MUSIC, GENERATING = range(3)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)


# ==================== دوال مساعدة ====================

def clean_user_files(user_id: int):
    """حذف كل ملفات المستخدم المؤقتة"""
    for f in TEMP_DIR.glob(f"{user_id}_*"):
        try:
            f.unlink()
        except Exception:
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
            logger.warning(f"Page {i} extract error: {e}")
    return "\n\n".join(texts)


def split_text(text: str, max_chars: int = MAX_CHARS_PER_TTS) -> list[str]:
    """تقسيم النص إلى أجزاء آمنة لـ TTS"""
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current = ""
    for paragraph in text.split("\n\n"):
        if len(current) + len(paragraph) + 2 <= max_chars:
            current += paragraph + "\n\n"
        else:
            if current:
                chunks.append(current.strip())
            # لو الفقرة نفسها طويلة جداً
            while len(paragraph) > max_chars:
                chunks.append(paragraph[:max_chars])
                paragraph = paragraph[max_chars:]
            current = paragraph + "\n\n"
    if current.strip():
        chunks.append(current.strip())
    return chunks


async def generate_tts(text: str, voice_id: str, output_path: str, rate: str = "+0%"):
    communicate = edge_tts.Communicate(text, voice_id, rate=rate)
    await communicate.save(output_path)


def download_youtube_audio(query_or_url: str, output_path: str) -> bool:
    try:
        search = query_or_url if query_or_url.startswith("http") else f"ytsearch1:{query_or_url}"
        ydl_opts = {
            "format": "bestaudio/best",
            "outtmpl": str(output_path).replace(".mp3", ""),
            "postprocessors": [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }],
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
        }
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([search])
        return Path(output_path).exists()
    except Exception as e:
        logger.error(f"YT download error: {e}")
        return False


def mix_audio(speech_file: str, music_file: str | None, output_file: str, music_volume: float = 0.30):
    speech = AudioSegment.from_file(speech_file)
    speech_len = len(speech)

    if music_file and Path(music_file).exists():
        music = AudioSegment.from_file(music_file)
        gain_db = 20 * math.log10(music_volume)
        music = music + gain_db

        if len(music) < speech_len:
            loops = math.ceil(speech_len / len(music))
            music = music * loops
        music = music[:speech_len].fade_out(2500)
        final = music.overlay(speech)
    else:
        final = speech

    final.export(output_file, format="mp3", bitrate="128k")


# ==================== معالجات البوت ====================

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    clean_user_files(update.effective_user.id)
    context.user_data.clear()

    text = (
        "📚 **مرحباً بك في أقوى بوت قارئ كتب صوتي!**\n\n"
        "1️⃣ أرسل ملف PDF\n"
        "2️⃣ اختر الصوت\n"
        "3️⃣ (اختياري) أضف موسيقى خلفية من يوتيوب\n"
        "4️⃣ سأقرأ الكتاب جزءاً بجزء مع جودة عالية\n\n"
        "الأوامر:\n"
        "/start - إعادة البدء\n"
        "/cancel - إلغاء العملية الحالية"
    )
    keyboard = [[InlineKeyboardButton("🎧 تجربة الأصوات", callback_data="test_voices")]]
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(keyboard), parse_mode="Markdown")
    return ConversationHandler.END


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    clean_user_files(user_id)
    context.user_data.clear()
    await update.message.reply_text("✅ تم إلغاء العملية وحذف الملفات المؤقتة.")
    return ConversationHandler.END


async def test_voices_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    buttons = [
        [InlineKeyboardButton(f"▶️ {v['name']}", callback_data=f"test_{k}")]
        for k, v in ARABIC_VOICES.items()
    ]
    await query.edit_message_text("اختر صوت لتجربته:", reply_markup=InlineKeyboardMarkup(buttons))


async def test_voice_play(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    key = query.data.replace("test_", "")
    voice = ARABIC_VOICES.get(key)
    if not voice:
        return

    await query.message.reply_text(f"⏳ جاري توليد عينة ({voice['name']})...")
    sample = "مرحباً بك، هذا نموذج تجريبي لصوتي. يمكنك اختياره لقراءة كتابك بكل سهولة ووضوح."
    out = TEMP_DIR / f"sample_{key}.mp3"
    await generate_tts(sample, voice["id"], str(out))
    with open(out, "rb") as f:
        await query.message.reply_audio(audio=f, caption=f"🔊 {voice['name']}")
    out.unlink(missing_ok=True)


async def handle_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    document = update.message.document
    if not document.file_name.lower().endswith(".pdf"):
        await update.message.reply_text("❌ أرسل ملف PDF فقط.")
        return ConversationHandler.END

    if document.file_size and document.file_size > 45 * 1024 * 1024:
        await update.message.reply_text("⚠️ الملف كبير جداً (الحد الموصى به 45 ميجا).")
        return ConversationHandler.END

    user_id = update.effective_user.id
    clean_user_files(user_id)

    status = await update.message.reply_text("📥 جاري تحميل الكتاب...")
    file = await context.bot.get_file(document.file_id)
    pdf_path = TEMP_DIR / f"{user_id}_book.pdf"
    await file.download_to_drive(pdf_path)

    reader = PdfReader(str(pdf_path))
    total_pages = len(reader.pages)

    context.user_data.update({
        "pdf_path": str(pdf_path),
        "total_pages": total_pages,
        "current_page": 0,
        "selected_voice": None,
        "music_path": None,
        "music_volume": 0.30,
    })

    buttons = [
        [InlineKeyboardButton(v["name"], callback_data=f"voice_{k}")]
        for k, v in ARABIC_VOICES.items()
    ]
    await status.edit_text(
        f"✅ تم استلام الكتاب\n"
        f"📄 الصفحات: **{total_pages}**\n"
        f"📦 الأجزاء المتوقعة: **{math.ceil(total_pages / CHUNK_PAGES)}**\n\n"
        f"اختر صوت القارئ:",
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

    keyboard = [
        [InlineKeyboardButton("🎵 25% هادئة", callback_data="vol_25")],
        [InlineKeyboardButton("🎵 35% متوسطة", callback_data="vol_35")],
        [InlineKeyboardButton("🎵 45% أعلى", callback_data="vol_45")],
        [InlineKeyboardButton("⏭️ بدون موسيقى", callback_data="skip_music")],
    ]
    await query.edit_message_text(
        f"🔊 تم اختيار: **{voice['name']}**\n\n"
        f"الآن:\n"
        f"• أرسل **رابط يوتيوب** أو **اسم الأغنية**\n"
        f"• أو اختر مستوى الصوت ثم اضغط بدون موسيقى",
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown"
    )
    return SELECT_MUSIC


async def set_volume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    vol = int(query.data.replace("vol_", "")) / 100
    context.user_data["music_volume"] = vol
    await query.answer(f"تم ضبط الصوت على {int(vol*100)}%")
    return SELECT_MUSIC


async def handle_music_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if "pdf_path" not in context.user_data:
        return ConversationHandler.END

    query_text = update.message.text.strip()
    user_id = update.effective_user.id
    music_path = TEMP_DIR / f"{user_id}_music.mp3"

    msg = await update.message.reply_text("🔎 جاري البحث والتحميل من يوتيوب...")
    loop = asyncio.get_running_loop()
    success = await loop.run_in_executor(None, download_youtube_audio, query_text, str(music_path))

    if success:
        context.user_data["music_path"] = str(music_path)
        await msg.edit_text("✅ تم تحميل الموسيقى بنجاح!")
    else:
        context.user_data["music_path"] = None
        await msg.edit_text("⚠️ فشل التحميل، سأكمل بدون موسيقى.")

    return await start_generation(update, context)


async def skip_music(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    context.user_data["music_path"] = None
    await query.edit_message_text("⏭️ تم التخطي... جاري البدء")
    return await start_generation(query, context)


async def start_generation(update_or_query, context: ContextTypes.DEFAULT_TYPE):
    # يدعم سواء message أو callback
    if hasattr(update_or_query, "message") and update_or_query.message:
        message = update_or_query.message
    else:
        message = update_or_query

    return await generate_next_chunk(message, context)


async def generate_next_chunk(message, context: ContextTypes.DEFAULT_TYPE):
    data = context.user_data
    if not data.get("pdf_path"):
        await message.reply_text("❌ انتهت الجلسة. أرسل /start من جديد.")
        return ConversationHandler.END

    user_id = message.chat_id
    start_p = data["current_page"]
    total_p = data["total_pages"]
    end_p = min(start_p + CHUNK_PAGES, total_p)
    part_num = (start_p // CHUNK_PAGES) + 1

    status = await message.reply_text(
        f"⏳ جاري معالجة **الجزء {part_num}**\n"
        f"الصفحات {start_p+1} → {end_p} من {total_p}",
        parse_mode="Markdown"
    )

    try:
        text = extract_text_from_pdf(data["pdf_path"], start_p, end_p)
        if not text.strip():
            await status.edit_text("⚠️ لا يوجد نص قابل للقراءة في هذه الصفحات (قد تكون صوراً).")
            data["current_page"] = end_p
            if end_p < total_p:
                keyboard = [[InlineKeyboardButton("▶️ الجزء التالي", callback_data="next_chunk")]]
                await message.reply_text("اضغط للمتابعة:", reply_markup=InlineKeyboardMarkup(keyboard))
            return GENERATING

        # تقسيم النص إلى أجزاء صغيرة
        text_chunks = split_text(text)
        speech_files = []

        for idx, chunk in enumerate(text_chunks):
            temp_speech = TEMP_DIR / f"{user_id}_speech_{idx}.mp3"
            await generate_tts(chunk, data["selected_voice"], str(temp_speech))
            speech_files.append(temp_speech)

        # دمج كل أجزاء الكلام
        combined = AudioSegment.empty()
        for sf in speech_files:
            combined += AudioSegment.from_file(sf)
            sf.unlink(missing_ok=True)

        speech_path = TEMP_DIR / f"{user_id}_speech_final.mp3"
        combined.export(speech_path, format="mp3")

        # دمج مع الموسيقى
        final_path = TEMP_DIR / f"{user_id}_part_{part_num}.mp3"
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(
            None, mix_audio,
            str(speech_path), data.get("music_path"),
            str(final_path), data.get("music_volume", 0.30)
        )
        speech_path.unlink(missing_ok=True)

        # تحديث التقدم
        data["current_page"] = end_p
        has_next = end_p < total_p

        keyboard = []
        if has_next:
            next_start = end_p + 1
            next_end = min(end_p + CHUNK_PAGES, total_p)
            keyboard.append([
                InlineKeyboardButton(
                    f"▶️ الجزء {part_num+1} (صـ {next_start}-{next_end})",
                    callback_data="next_chunk"
                )
            ])
        keyboard.append([InlineKeyboardButton("🔄 إعادة هذا الجزء", callback_data="regen_chunk")])
        keyboard.append([InlineKeyboardButton("❌ إلغاء وإنهاء", callback_data="cancel_all")])

        caption = (
            f"📖 **الجزء {part_num}** | {data.get('voice_name', '')}\n"
            f"📄 الصفحات: {start_p+1} → {end_p}\n"
            f"📊 التقدم: {end_p}/{total_p} ({int(end_p/total_p*100)}%)"
        )

        await status.delete()
        with open(final_path, "rb") as audio:
            await message.reply_audio(
                audio=audio,
                caption=caption,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown",
                title=f"الجزء {part_num}",
                performer="قارئ الكتب"
            )

    except Exception as e:
        logger.exception("Generation error")
        await status.edit_text(f"❌ حدث خطأ أثناء المعالجة:\n`{str(e)[:200]}`", parse_mode="Markdown")

    return GENERATING


async def next_chunk_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    return await generate_next_chunk(query.message, context)


async def regen_chunk(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("جاري إعادة التوليد...")
    # نرجع صفحة للخلف
    data = context.user_data
    data["current_page"] = max(0, data["current_page"] - CHUNK_PAGES)
    return await generate_next_chunk(query.message, context)


async def cancel_all(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    clean_user_files(query.from_user.id)
    context.user_data.clear()
    await query.edit_message_text("✅ تم إنهاء العملية وحذف الملفات.")
    return ConversationHandler.END


# ==================== التشغيل ====================

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    conv_handler = ConversationHandler(
        entry_points=[
            MessageHandler(filters.Document.MimeType("application/pdf"), handle_pdf),
            CommandHandler("start", start_command),
        ],
        states={
            SELECT_VOICE: [
                CallbackQueryHandler(set_voice, pattern=r"^voice_"),
            ],
            SELECT_MUSIC: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_music_text),
                CallbackQueryHandler(set_volume, pattern=r"^vol_"),
                CallbackQueryHandler(skip_music, pattern=r"^skip_music$"),
            ],
            GENERATING: [
                CallbackQueryHandler(next_chunk_callback, pattern=r"^next_chunk$"),
                CallbackQueryHandler(regen_chunk, pattern=r"^regen_chunk$"),
                CallbackQueryHandler(cancel_all, pattern=r"^cancel_all$"),
            ],
        },
        fallbacks=[
            CommandHandler("cancel", cancel_command),
            CommandHandler("start", start_command),
        ],
        allow_reentry=True,
    )

    app.add_handler(conv_handler)
    app.add_handler(CallbackQueryHandler(test_voices_menu, pattern=r"^test_voices$"))
    app.add_handler(CallbackQueryHandler(test_voice_play, pattern=r"^test_"))

    print("🤖 البوت الأقوى يعمل الآن...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
