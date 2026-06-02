import asyncio
import logging
import os
import re
import shutil
import zipfile
from uuid import uuid4
from pathlib import Path
from PIL import Image, UnidentifiedImageError
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    ApplicationBuilder, CommandHandler, MessageHandler, 
    CallbackQueryHandler, ContextTypes, filters
)

# ------------------------- الإعدادات -------------------------
TOKEN = os.environ.get("TOKEN", "6202130917:AAGALbLs7q5uObku7S94zQcTeFR85e1aoqI")  # ⚠️ استبدله بالتوكن الحقيقي
MAX_PACK_SIZE_MB = 50
PROGRESS_BAR_LENGTH = 20
PROGRESS_BAR_FILL = "█"
PROGRESS_BAR_EMPTY = "-"

# ------------------------- تسجيل التقارير (Logging) -------------------------
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log", encoding="utf-8"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ------------------------- البيانات العامة -------------------------
current_tasks = {}  # user_id -> {"task": asyncio.Task, "data": ...}
stats = {"stickers": 0, "packs": 0}

# ------------------------- الأدوات المساعدة -------------------------
async def async_cleanup_files(*paths):
    """يحذف الملفات والمجلدات بشكل غير متزامن، مع تجاهل القيمة None والمسارات غير الموجودة."""
    def _cleanup():
        for path in paths:
            if path is None:
                continue
            if os.path.isfile(path):
                try:
                    os.remove(path)
                    logger.debug(f"تم حذف الملف: {path}")
                except Exception as e:
                    logger.warning(f"تعذر حذف الملف {path}: {e}")
            elif os.path.isdir(path):
                try:
                    shutil.rmtree(path, ignore_errors=True)
                    logger.debug(f"تم حذف المجلد: {path}")
                except Exception as e:
                    logger.warning(f"تعذر حذف المجلد {path}: {e}")
    await asyncio.to_thread(_cleanup)

async def get_folder_size_mb(folder):
    """يعيد حجم المجلد بالميغابايت بشكل غير متزامن."""
    if not os.path.exists(folder):
        return 0.0
    
    def _size():
        total = 0
        for root, dirs, files in os.walk(folder):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    continue
        return total / (1024 * 1024)
    return await asyncio.to_thread(_size)

def progress_bar(done, total, length=PROGRESS_BAR_LENGTH):
    """يعيد شريط التقدم مع النسب المئوية والعداد."""
    filled = int(done / total * length)
    perc = int(done / total * 100)
    bar = PROGRESS_BAR_FILL * filled + PROGRESS_BAR_EMPTY * (length - filled)
    return f"[{bar}] {perc}% ({done}/{total})"

def format_size_mb(size_mb: float) -> str:
    """تنسيق جميل للحجم بالميغابايت."""
    if size_mb < 0.1:
        return f"{size_mb * 1024:.0f} KB"
    elif size_mb < 1:
        return f"{size_mb * 1024:.1f} KB"
    elif size_mb < 1024:
        return f"{size_mb:.2f} MB"
    else:
        return f"{size_mb / 1024:.2f} GB"

def safe_filename(name: str) -> str:
    """إنشاء اسم ملف آمن لملفات ZIP."""
    return re.sub(r"[^a-zA-Z0-9_\-]", "_", name)

async def safe_delete_message(message):
    """حذف الرسالة بشكل آمن مع تجاهل الأخطاء."""
    if message:
        try:
            await message.delete()
        except Exception:
            pass

# ------------------------- الأوامر -------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "مرحباً! 👋\n\n"
        "سأساعدك في تحميل الملصقات من تيليجرام.\n\n"
        "📌 أرسل لي:\n"
        "• أي ملصق — لتحميل ملصق واحد\n"
        "• رابط بصيغة t.me/addstickers/اسم_الحزمة — لتحميل حزمة كاملة\n\n"
        "بعد ذلك، اختر الصيغة: PNG، JPG أو ZIP.\n\n"
        "🔧 الأوامر:\n"
        "/stats — الإحصائيات\n"
        "/cancel — إلغاء المهمة الحالية\n"
        "/help — دليل المساعدة هذا"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "/start — تشغيل البوت\n"
        "/help — المساعدة\n"
        "/stats — الإحصائيات\n"
        "/cancel — إلغاء المهمة الحالية\n\n"
        "📥 كيفية تحميل ملصق واحد:\n"
        "1. أرسل الملصق\n"
        "2. اختر الصيغة (PNG/JPG/ZIP)\n"
        "3. استلم الملف\n\n"
        "📦 كيفية تحميل حزمة ملصقات:\n"
        "1. أرسل الرابط t.me/addstickers/اسم_الحزمة\n"
        "2. اختر الصيغة\n"
        "3. استلم أرشيف ZIP يحتوي على جميع الملصقات"
    )

async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        f"📊 <b>إحصائيات البوت</b>\n"
        f"━━━━━━━━━━━━━━━━━━━━━\n"
        f"🎨 الملصقات المحملة: <b>{stats['stickers']}</b>\n"
        f"📦 الحزم المحملة: <b>{stats['packs']}</b>",
        parse_mode="HTML"
    )

async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    task_info = current_tasks.pop(user_id, None)
    
    if task_info and task_info.get("task") and not task_info["task"].done():
        task_info["task"].cancel()
        await update.message.reply_text("❌ تم إلغاء المهمة")
        logger.info(f"قام المستخدم {user_id} بإلغاء المهمة")
    else:
        await update.message.reply_text("ℹ️ لا توجد مهام نشطة")

# ------------------------- معالجة الرسائل -------------------------
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.message.from_user.id
    
    if update.message.sticker:
        sticker = update.message.sticker
        current_tasks[user_id] = {"data": {"sticker": sticker}, "task": None}
        
        keyboard = [
            [InlineKeyboardButton("🖼 PNG", callback_data="format_png"),
             InlineKeyboardButton("📸 JPG", callback_data="format_jpg"),
             InlineKeyboardButton("📦 ZIP", callback_data="format_zip")]
        ]
        await update.message.reply_text(
            "🎨 اختر صيغة الملصق:",
            reply_markup=InlineKeyboardMarkup(keyboard)
        )
        
    elif update.message.text:
        match = re.search(r"t\.me/addstickers/(\w+)", update.message.text)
        if match:
            set_name = match.group(1)
            current_tasks[user_id] = {"data": {"set_name": set_name}, "task": None}
            
            keyboard = [
                [InlineKeyboardButton("🖼 PNG (في ملف ZIP)", callback_data="pack_png"),
                 InlineKeyboardButton("📸 JPG (في ملف ZIP)", callback_data="pack_jpg"),
                 InlineKeyboardButton("📦 ZIP (الأصلي)", callback_data="pack_zip")]
            ]
            await update.message.reply_text(
                f"📦 تم العثور على حزمة: <code>{set_name}</code>\n\nاختر الصيغة:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML"
            )

# ------------------------- الأزرار -------------------------
async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    data = query.data
    user_id = query.from_user.id
    task_info = current_tasks.get(user_id)
    
    if not task_info:
        await query.message.reply_text("❌ لم يتم العثور على المهمة. أرسل الملصق أو الرابط مجدداً.")
        return

    # تحديد ما يتم تحميله
    if data.startswith("format_") and "sticker" in task_info["data"]:
        format_type = data.split("_")[1]
        sticker = task_info["data"]["sticker"]
        task = asyncio.create_task(download_sticker_single(query, context, sticker, format_type))
        current_tasks[user_id]["task"] = task
        
    elif data.startswith("pack_") and "set_name" in task_info["data"]:
        format_type = data.split("_")[1]
        set_name = task_info["data"]["set_name"]
        task = asyncio.create_task(download_full_pack(query, context, set_name, format_type))
        current_tasks[user_id]["task"] = task
    
    # حذف الرسالة التي تحتوي على الأزرار
    await safe_delete_message(query.message)

# ------------------------- تحميل ملصق واحد -------------------------
async def download_sticker_single(query, context, sticker, format_type):
    user_id = query.from_user.id
    temp_folder = f"temp_{uuid4().hex}"
    os.makedirs(temp_folder, exist_ok=True)
    
    # تحديد الامتداد
    if sticker.is_animated:
        ext = ".tgs"
    elif sticker.is_video:
        ext = ".webm"
    else:
        ext = ".webp"
    
    temp_path = os.path.join(temp_folder, f"sticker{ext}")
    original_name = Path(temp_path).stem
    zip_name = None
    
    try:
        file = await context.bot.get_file(sticker.file_id)
        await file.download_to_drive(temp_path)
        
        # التحويل إلى JPG
        if format_type == "jpg":
            try:
                im = Image.open(temp_path)
                jpg_path = temp_path.rsplit(".", 1)[0] + ".jpg"
                im.convert("RGB").save(jpg_path, "JPEG")
                await async_cleanup_files(temp_path)
                temp_path = jpg_path
            except UnidentifiedImageError:
                pass
        
        # إنشاء ملف ZIP بالامتداد الصحيح في الداخل
        if format_type == "zip":
            zip_name = os.path.join(temp_folder, f"{original_name}.zip")
            target_ext = ".png" if ext == ".webp" else ".jpg" if format_type == "jpg" else ext
            with zipfile.ZipFile(zip_name, "w") as zipf:
                arcname = f"{original_name}{target_ext}"
                zipf.write(temp_path, arcname=arcname)
            await query.message.reply_document(
                open(zip_name, "rb"),
                filename=f"sticker_{original_name}.zip"
            )
        else:
            await query.message.reply_document(
                open(temp_path, "rb"),
                filename=f"sticker_{original_name}{'.jpg' if format_type == 'jpg' else '.png' if format_type == 'png' else ext}"
            )
        
        stats["stickers"] += 1
        logger.info(f"تم تحميل الملصق بواسطة المستخدم {user_id}، الصيغة: {format_type}")
        
    except asyncio.CancelledError:
        await query.message.reply_text("❌ تم إلغاء التحميل")
        raise
    except Exception as e:
        await query.message.reply_text(f"❌ خطأ: {str(e)[:200]}")
        logger.error(f"خطأ أثناء تحميل الملصق: {e}")
    finally:
        await async_cleanup_files(temp_folder, zip_name)
        current_tasks.pop(user_id, None)

# ------------------------- تحميل حزمة ملصقات كاملة -------------------------
async def download_full_pack(query, context, set_name, format_type):
    user_id = query.from_user.id
    temp_folder = f"temp_pack_{uuid4().hex}"
    os.makedirs(temp_folder, exist_ok=True)
    zip_name = None
    progress_msg = None
    
    try:
        progress_msg = await query.message.reply_text(
            f"📦 جاري بدء تحميل الحزمة <code>{set_name}</code>...",
            parse_mode="HTML"
        )
        
        sticker_set = await context.bot.get_sticker_set(set_name)
        stickers = sticker_set.stickers
        total = len(stickers)
        
        if total == 0:
            await progress_msg.edit_text("❌ الحزمة فارغة")
            return
        
        last_percent = -1
        downloaded_files = []
        
        for idx, sticker in enumerate(stickers, 1):
            if sticker.is_animated:
                ext = ".tgs"
            elif sticker.is_video:
                ext = ".webm"
            else:
                ext = ".webp"
            
            temp_path = os.path.join(temp_folder, f"{sticker.file_unique_id}{ext}")
            file = await context.bot.get_file(sticker.file_id)
            await file.download_to_drive(temp_path)
            
            # التحويل لصيغة PNG/JPG
            if format_type in ["png", "jpg"] and ext == ".webp":
                try:
                    im = Image.open(temp_path)
                    new_ext = ".png" if format_type == "png" else ".jpg"
                    new_path = temp_path.rsplit(".", 1)[0] + new_ext
                    if format_type == "png":
                        im.save(new_path, "PNG")
                    else:
                        im.convert("RGB").save(new_path, "JPEG")
                    await async_cleanup_files(temp_path)
                    temp_path = new_path
                except UnidentifiedImageError:
                    pass
            
            downloaded_files.append(temp_path)
            
            # تحديث شريط التقدم
            current_percent = int(idx / total * 100)
            if current_percent != last_percent or idx % 5 == 0 or idx == total:
                await progress_msg.edit_text(
                    f"📦 تحميل الحزمة <code>{set_name}</code>\n"
                    f"{progress_bar(idx, total)}\n"
                    f"💾 {format_size_mb(await get_folder_size_mb(temp_folder))}",
                    parse_mode="HTML"
                )
                last_percent = current_percent
            
            # حد الحجم الأقصى
            size_mb = await get_folder_size_mb(temp_folder)
            if size_mb > MAX_PACK_SIZE_MB:
                await progress_msg.edit_text(f"❌ الحزمة تتجاوز الحد المسموح ({MAX_PACK_SIZE_MB} ميغابايت)")
                return
        
        # إنشاء ملف ZIP
        zip_name = os.path.join(temp_folder, f"{safe_filename(set_name)}.zip")
        with zipfile.ZipFile(zip_name, "w", zipfile.ZIP_DEFLATED) as zipf:
            for idx, file_path in enumerate(downloaded_files, 1):
                if format_type == "png":
                    arcname = f"{idx:04d}.png"
                elif format_type == "jpg":
                    arcname = f"{idx:04d}.jpg"
                else:
                    ext = os.path.splitext(file_path)[1]
                    arcname = f"{idx:04d}{ext}"
                zipf.write(file_path, arcname)
        
        # حذف رسالة التقدم
        await safe_delete_message(progress_msg)
        
        # إرسال الأرشيف مضغوطاً
        size_mb = await get_folder_size_mb(temp_folder)
        caption = (
            f"✅ <b>حزمة الملصقات جاهزة!</b>\n\n"
            f"📦 <code>{set_name}</code>\n"
            f"📊 عدد الملصقات: <b>{total}</b>\n"
            f"💾 الحجم: <b>{format_size_mb(size_mb)}</b>\n"
            f"🎨 الصيغة: <b>{format_type.upper()}</b>"
        )
        await query.message.reply_document(
            open(zip_name, "rb"),
            filename=f"{safe_filename(set_name)}.zip",
            caption=caption,
            parse_mode="HTML"
        )
        
        stats["packs"] += 1
        logger.info(f"تم تحميل الحزمة {set_name} بواسطة المستخدم {user_id}، الصيغة: {format_type}، الحجم: {format_size_mb(size_mb)}")
        
    except asyncio.CancelledError:
        await safe_delete_message(progress_msg)
        await query.message.reply_text("❌ تم إلغاء تحميل الحزمة")
        raise
    except Exception as e:
        await safe_delete_message(progress_msg)
        await query.message.reply_text(f"❌ خطأ: {str(e)[:200]}")
        logger.error(f"خطأ أثناء تحميل الحزمة {set_name}: {e}")
    finally:
        await async_cleanup_files(temp_folder, zip_name)
        current_tasks.pop(user_id, None)

# ------------------------- تشغيل البوت -------------------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()
    
    # الأوامر
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("cancel", cancel_command))
    
    # الرسائل
    app.add_handler(MessageHandler(
        filters.Sticker.ALL | (filters.TEXT & ~filters.COMMAND),
        handle_message
    ))
    
    # الأزرار
    app.add_handler(CallbackQueryHandler(button_callback))
    
    logger.info("🚀 تم تشغيل البوت بنجاح!")
    app.run_polling()

if __name__ == "__main__":
    main()
