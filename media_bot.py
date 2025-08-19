import logging
import os
import re
import shutil
import glob
import asyncio
import json
import itertools
import traceback
import tempfile
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
from telegram.error import NetworkError, TimedOut, RetryAfter
from mp3_downloader import MP3Downloader
from bot_notifications import create_notification_manager

# --- НАСТРОЙКА ЛОГИРОВАНИЯ (УБИРАЕМ СПАМ) ---
logging.getLogger('httpx').setLevel(logging.ERROR)
logging.getLogger('httpcore').setLevel(logging.ERROR)
logging.getLogger('telegram').setLevel(logging.WARNING)

# Основная настройка логирования
logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%d.%m.%Y %H:%M:%S",
    level=logging.INFO
)

# --- КОНФИГУРАЦИЯ ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
GROUP_IDS_STR = os.getenv("ALLOWED_GROUP_IDS")
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID")  # Новая переменная для группы админа
TEMP_DOWNLOADS_DIR = "/app/bot_temp"
COOKIES_DIR = "/app/cookies"
TELEGRAM_SIZE_LIMIT_BYTES = 49 * 1024 * 1024 # 49 МБ для надежности
NOTIFICATIONS_ENABLED = os.getenv("BOT_NOTIFICATIONS", "no")

notification_manager = create_notification_manager(GROUP_IDS_STR, NOTIFICATIONS_ENABLED)

if not BOT_TOKEN or not GROUP_IDS_STR:
    logging.critical("ERROR: BOT_TOKEN, ALLOWED_GROUP_IDS environment variables not set!")
    exit()

# ADMIN_GROUP_ID не обязательный, но если задан, то должен быть числом
if ADMIN_GROUP_ID:
    try:
        ADMIN_GROUP_ID = int(ADMIN_GROUP_ID)
        logging.info(f"Admin group ID set: {ADMIN_GROUP_ID}")
    except ValueError:
        logging.critical("ERROR: ADMIN_GROUP_ID must be a valid integer!")
        exit()
else:
    logging.info("ADMIN_GROUP_ID not set - error notifications disabled")

try:
    ALLOWED_GROUP_IDS = {int(group_id.strip()) for group_id in GROUP_IDS_STR.split(',')}
except (ValueError, TypeError):
    logging.critical(f"ERROR: Invalid format in ALLOWED_GROUP_IDS.")
    exit()
if not os.path.exists(TEMP_DOWNLOADS_DIR):
    try: os.makedirs(TEMP_DOWNLOADS_DIR)
    except OSError as e: logging.critical(f"Failed to create directory {TEMP_DOWNLOADS_DIR}: {e}"); exit()

logger = logging.getLogger(__name__)
script_dir = os.path.dirname(os.path.abspath(__file__))

# Инициализируем MP3 downloader
mp3_downloader = MP3Downloader(TEMP_DOWNLOADS_DIR, TELEGRAM_SIZE_LIMIT_BYTES)

# Глобальная переменная для передачи контекста в CookieRotator
_current_bot_context = None
# Глобальная переменная для хранения последнего STDERR от yt-dlp
_last_ytdlp_stderr = ""

# --- ФУНКЦИЯ ОТПРАВКИ ОШИБОК АДМИНУ ---
async def send_error_to_admin(context: ContextTypes.DEFAULT_TYPE, error_message: str, error_details: str, platform: str = "Unknown"):
    """Отправляет сообщение об ошибке и файл с деталями в группу администратора"""
    if not ADMIN_GROUP_ID:
        return  # Если ID группы админа не задан, просто пропускаем

    try:
        # Формируем сообщение для админа
        timestamp = datetime.now().strftime("%d.%m.%Y %H:%M:%S")
        admin_message = f"🚨 <b>Ошибка в боте</b>\n\n" \
                       f"📅 Время: {timestamp}\n" \
                       f"🎬 Платформа: {platform}\n" \
                       f"❌ Ошибка: {error_message}\n\n" \
                       f"Подробности ошибки в прикрепленном файле."

        # Создаем временный файл с деталями ошибки
        with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as tmp_file:
            tmp_file.write(f"Отчет об ошибке бота\n")
            tmp_file.write(f"{'='*50}\n")
            tmp_file.write(f"Время: {timestamp}\n")
            tmp_file.write(f"Платформа: {platform}\n")
            tmp_file.write(f"Краткое описание: {error_message}\n")
            tmp_file.write(f"{'='*50}\n\n")
            tmp_file.write(f"ПОДРОБНАЯ ИНФОРМАЦИЯ ОБ ОШИБКЕ:\n")
            tmp_file.write(f"{'-'*50}\n")
            tmp_file.write(error_details)

            # Добавляем информацию из последнего STDERR yt-dlp, если есть
            global _last_ytdlp_stderr
            if _last_ytdlp_stderr.strip():
                tmp_file.write(f"\n\n{'='*50}\n")
                tmp_file.write(f"ПОСЛЕДНИЙ YT-DLP STDERR:\n")
                tmp_file.write(f"{'-'*50}\n")
                tmp_file.write(_last_ytdlp_stderr)

            tmp_file_path = tmp_file.name

        # Отправляем сообщение с файлом
        with open(tmp_file_path, 'rb') as error_file:
            await context.bot.send_document(
                chat_id=ADMIN_GROUP_ID,
                document=error_file,
                caption=admin_message,
                parse_mode="HTML",
                filename=f"error_{platform}_{timestamp.replace(':', '-').replace(' ', '_')}.txt"
            )

        # Удаляем временный файл
        os.unlink(tmp_file_path)

        logger.info(f"Error report sent to admin group: {ADMIN_GROUP_ID}")

    except Exception as e:
        logger.error(f"Failed to send error report to admin: {e}")

# --- МЕНЕДЖЕР РОТАЦИИ COOKIE ДЛЯ INSTAGRAM ---
class CookieRotator:
    def __init__(self, cookies_dir: str):
        self.cookies_dir = cookies_dir
        self.cookie_files = self._load_cookie_files()
        self.cookie_cycle = itertools.cycle(self.cookie_files) if self.cookie_files else None
        self.current_cookie_file = None

    def _load_cookie_files(self) -> list:
        """Загружает все cookie файлы из директории для Instagram"""
        if not os.path.exists(self.cookies_dir):
            logger.warning(f"🍪 Cookies directory not found: {self.cookies_dir}")
            return []

        # Ищем все файлы cookies*.txt (для Instagram)
        files = sorted(glob.glob(os.path.join(self.cookies_dir, 'cookies*.txt')))

        if files:
            logger.info(f"🍪 Found {len(files)} instagram cookie files: {[os.path.basename(f) for f in files]}")
        else:
            logger.warning(f"🍪 No instagram cookie files found in {self.cookies_dir}")

        return files

    def get_next_cookie(self) -> str:
        """Возвращает путь к следующему cookie файлу из ротации"""
        if not self.cookie_cycle:
            raise Exception("No available cookie files")

        self.current_cookie_file = next(self.cookie_cycle)
        cookie_name = os.path.basename(self.current_cookie_file)
        logger.info(f"🔄 Switching to instagram cookie: {cookie_name}")

        return self.current_cookie_file

    async def try_with_all_cookies_async(self, process_func, url, temp_folder, *args, **kwargs):
        """Асинхронно пробует обработать с каждым cookie по очереди (проверка + скачивание)"""
        if not self.cookie_files:
            raise Exception("No available cookie files for attempts")

        attempts_total = len(self.cookie_files)
        last_error = None
        photo_only_detected = False

        for attempt in range(attempts_total):
            try:
                cookie_path = self.get_next_cookie()
                cookie_name = os.path.basename(cookie_path)

                logger.info(f"🍪 Attempt {attempt + 1}/{attempts_total} with instagram cookie: {cookie_name}")
                result = await process_func(cookie_path, url, temp_folder, *args, **kwargs)

                logger.info(f"✅ Successfully processed with instagram cookie: {cookie_name}")
                return result

            except Exception as e:
                last_error = e
                cookie_name = os.path.basename(self.current_cookie_file) if self.current_cookie_file else "unknown"
                error_msg = str(e)

                # Проверяем, является ли это ошибкой "только фото"
                if error_msg.startswith("PHOTO_ONLY:"):
                    logger.info(f"ℹ️ Instagram cookie {cookie_name}: Detected photo-only post")
                    photo_only_detected = True
                    # Для фото-постов не пробуем другие cookie, сразу возвращаем ошибку
                    raise e
                else:
                    logger.warning(f"❌ Error with instagram cookie {cookie_name}: {str(e)}")

                    # Отправляем уведомление админу только о реальных ошибках
                    if _current_bot_context and ADMIN_GROUP_ID:
                        error_details = f"Instagram cookie error for URL: {url}\n"
                        error_details += f"Cookie file: {cookie_name}\n"
                        error_details += f"Attempt: {attempt + 1}/{attempts_total}\n"
                        error_details += f"Cookie path: {cookie_path}\n\n"
                        error_details += f"Exception: {str(e)}\n\n"
                        error_details += f"Traceback:\n{traceback.format_exc()}"

                        try:
                            await send_error_to_admin(
                                _current_bot_context,
                                f"Instagram cookie {cookie_name}: Ошибка при попытке {attempt + 1}/{attempts_total}",
                                error_details,
                                "Instagram Cookie"
                            )
                        except Exception as admin_error:
                            logger.error(f"Failed to send cookie error to admin: {admin_error}")

                if attempt < attempts_total - 1:
                    logger.info("🔄 Trying next instagram cookie...")

        raise Exception(f"All instagram cookie files failed. Last error: {last_error}")

# --- МЕНЕДЖЕР РОТАЦИИ COOKIE ДЛЯ TIKTOK ---
class TikTokCookieRotator:
    def __init__(self, cookies_dir: str):
        self.cookies_dir = cookies_dir
        self.cookie_files = self._load_cookie_files()
        self.cookie_cycle = itertools.cycle(self.cookie_files) if self.cookie_files else None
        self.current_cookie_file = None

    def _load_cookie_files(self) -> list:
        """Загружает все cookie файлы из директории для TikTok"""
        if not os.path.exists(self.cookies_dir):
            logger.warning(f"🍪 Cookies directory not found: {self.cookies_dir}")
            return []

        # Ищем все файлы cookie_tiktok*.txt
        files = sorted(glob.glob(os.path.join(self.cookies_dir, 'cookie_tiktok*.txt')))

        if files:
            logger.info(f"🍪 Found {len(files)} tiktok cookie files: {[os.path.basename(f) for f in files]}")
        else:
            logger.warning(f"🍪 No tiktok cookie files found in {self.cookies_dir}")

        return files

    def get_next_cookie(self) -> str:
        """Возвращает путь к следующему TikTok cookie файлу из ротации"""
        if not self.cookie_cycle:
            raise Exception("No available TikTok cookie files")

        self.current_cookie_file = next(self.cookie_cycle)
        cookie_name = os.path.basename(self.current_cookie_file)
        logger.info(f"🔄 Switching to tiktok cookie: {cookie_name}")

        return self.current_cookie_file

    async def try_with_all_cookies_async(self, process_func, url, temp_folder, *args, **kwargs):
        """Асинхронно пробует обработать с каждым TikTok cookie по очереди"""
        if not self.cookie_files:
            raise Exception("No available TikTok cookie files for attempts")

        attempts_total = len(self.cookie_files)
        last_error = None

        for attempt in range(attempts_total):
            try:
                cookie_path = self.get_next_cookie()
                cookie_name = os.path.basename(cookie_path)

                logger.info(f"🍪 Attempt {attempt + 1}/{attempts_total} with tiktok cookie: {cookie_name}")
                result = await process_func(cookie_path, url, temp_folder, *args, **kwargs)

                logger.info(f"✅ Successfully processed with tiktok cookie: {cookie_name}")
                return result

            except Exception as e:
                last_error = e
                cookie_name = os.path.basename(self.current_cookie_file) if self.current_cookie_file else "unknown"
                logger.warning(f"❌ Error with tiktok cookie {cookie_name}: {str(e)}")

                # Отправляем уведомление админу только о реальных ошибках
                if _current_bot_context and ADMIN_GROUP_ID:
                    error_details = f"TikTok cookie error for URL: {url}\n"
                    error_details += f"Cookie file: {cookie_name}\n"
                    error_details += f"Attempt: {attempt + 1}/{attempts_total}\n"
                    error_details += f"Cookie path: {cookie_path}\n\n"
                    error_details += f"Exception: {str(e)}\n\n"
                    error_details += f"Traceback:\n{traceback.format_exc()}"

                    try:
                        await send_error_to_admin(
                            _current_bot_context,
                            f"TikTok cookie {cookie_name}: Ошибка при попытке {attempt + 1}/{attempts_total}",
                            error_details,
                            "TikTok Cookie"
                        )
                    except Exception as admin_error:
                        logger.error(f"Failed to send tiktok cookie error to admin: {admin_error}")

                if attempt < attempts_total - 1:
                    logger.info("🔄 Trying next tiktok cookie...")

        raise Exception(f"All TikTok cookie files failed. Last error: {last_error}")

# Глобальные экземпляры ротаторов
cookie_rotator = CookieRotator(COOKIES_DIR)
tiktok_cookie_rotator = TikTokCookieRotator(COOKIES_DIR)

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def find_instagram_url(text: str):
    pattern = r"(https://www\.instagram\.com/(p|reel)/[a-zA-Z0-9_-]+/?)"
    match = re.search(pattern, text)
    return match.group(0) if match else None

def find_tiktok_url(text: str):
    pattern = r"https?://(?:www\.|vm\.)?tiktok\.com/(@[\w\.-]+/video/\d+|[\w-]+)"
    match = re.search(pattern, text)
    return match.group(0) if match else None

def find_youtube_shorts_url(text: str):
    pattern = r"(https?://(?:www\.)?youtube\.com/shorts/[a-zA-Z0-9_-]+)"
    match = re.search(pattern, text)
    return match.group(0) if match else None

def resolve_tiktok_url(url: str):
    if "vm.tiktok.com" in url:
        try: return requests.head(url, allow_redirects=True, timeout=10).url
        except requests.RequestException: return url
    return url

async def run_subprocess(command: list[str], timeout: int = 180, suppress_stdout_log: bool = False) -> tuple[str, str]:
    global _last_ytdlp_stderr

    logger.info(f"🛠 Запуск команды: {' '.join(command)}")

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

        # Сохраняем STDERR для отчетов об ошибках
        stderr_decoded = stderr.decode(errors='ignore')
        _last_ytdlp_stderr = stderr_decoded

        # Логируем STDOUT только если не подавлено
        if stdout and not suppress_stdout_log:
            logger.info(f"[yt-dlp STDOUT]\n{stdout.decode(errors='ignore')}")

        # STDERR всегда логируем
        if stderr_decoded:
            logger.warning(f"[yt-dlp STDERR]\n{stderr_decoded}")

        return stdout.decode(), stderr_decoded

    except asyncio.TimeoutError:
        try:
            process.kill()
            raise TimeoutError(f"Command timed out after {timeout} seconds")
        except ProcessLookupError:
            pass
        raise

# --- ЛОГИКА СКАЧИВАНИЯ С РОТАЦИЕЙ COOKIE ДЛЯ INSTAGRAM ---
async def process_instagram_with_cookie(cookie_path: str, url: str, temp_folder: str) -> str:
    """Проверяет содержимое поста и скачивает Instagram видео с конкретным cookie файлом"""

    # Сначала проверяем содержимое поста
    logger.info(f"🔍 Checking Instagram post content with cookie: {os.path.basename(cookie_path)}")

    check_command = [
        'yt-dlp',
        '--dump-json',
        '--no-warnings',
        '--playlist-items', '1',
        '--cookies', cookie_path,
        '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0',
        url
    ]

    # Подавляем STDOUT логирование для команды проверки
    stdout, stderr = await run_subprocess(check_command, timeout=30, suppress_stdout_log=True)

    # Проверяем, содержит ли STDERR сообщение о том, что видео форматы не найдены
    if "No video formats found!" in stderr:
        logger.info(f"ℹ️ Instagram post contains only images/photos (no video formats found)")
        raise Exception("PHOTO_ONLY:В этом посте только фотографии, видео отсутствует")

    if not stdout.strip():
        raise Exception("Не удалось получить информацию о посте")

    try:
        post_info = json.loads(stdout)
    except json.JSONDecodeError:
        raise Exception("Ошибка обработки информации о посте")

    # Проверяем наличие видео в посте (дополнительная проверка)
    formats = post_info.get('formats', [])
    has_video_format = False

    for format_info in formats:
        # Ищем форматы с видео (не только аудио)
        if format_info.get('vcodec') and format_info.get('vcodec') != 'none':
            has_video_format = True
            break

    # Дополнительная проверка через duration
    duration = post_info.get('duration')

    # Если нет видео форматов и нет длительности
    if not has_video_format and (not duration or duration <= 0):
        logger.info(f"ℹ️ Instagram post contains only images/photos (no video formats in JSON)")
        raise Exception("PHOTO_ONLY:В этом посте только фотографии, видео отсутствует")

    logger.info(f"✅ Instagram post contains video content, proceeding with download")

    # Если видео есть, скачиваем его
    format_selector = "best[height<=720][ext=mp4]/best[ext=mp4]/best[height<=720]/best"

    yt_dlp_download_command = [
        'yt-dlp', url,
        '--playlist-items', '1',
        '-f', format_selector,
        '-o', os.path.join(temp_folder, 'final_video.%(ext)s'),
        '--no-warnings',
        '--cookies', cookie_path,
        '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0'
    ]

    try:
        await run_subprocess(yt_dlp_download_command)
    except Exception:
        # План Б: fallback на любой best
        logger.warning("Failed to download format <= 720p, trying best available...")
        yt_dlp_download_command[4] = "best"
        await run_subprocess(yt_dlp_download_command)

    video_files = glob.glob(os.path.join(temp_folder, '*.mp4'))
    if not video_files:
        raise Exception("Video file not created")

    return video_files[0]

async def download_video_with_yt_dlp_instagram(url: str, temp_folder: str) -> tuple[str | None, str | None]:
    """Скачивает Instagram видео с ротацией cookie. Возвращает (video_path, error_message)"""
    try:
        logger.info(f"🎬 Starting Instagram processing: {url}")

        video_path = await cookie_rotator.try_with_all_cookies_async(
            process_instagram_with_cookie,
            url,
            temp_folder
        )

        logger.info(f"✅ Instagram video successfully downloaded: {video_path}")
        return video_path, None

    except Exception as e:
        error_msg = str(e)
        if error_msg.startswith("PHOTO_ONLY:"):
            # Это сообщение о том, что в посте только фото
            photo_msg = error_msg.replace("PHOTO_ONLY:", "")
            logger.info(f"ℹ️ Instagram post is photo-only: {url}")
            return None, photo_msg
        else:
            logger.error(f"❌ Failed to download Instagram video: {e}")
            return None, None

# --- ЛОГИКА СКАЧИВАНИЯ С РОТАЦИЕЙ COOKIE ДЛЯ TIKTOK ---
async def process_tiktok_with_cookie(cookie_path: str, url: str, temp_folder: str) -> str:
    """Скачивает TikTok видео с конкретным cookie файлом"""
    logger.info(f"🎬 TikTok: Getting available formats for URL: {url} with cookie: {os.path.basename(cookie_path)}")

    # Команда для получения информации с cookies
    yt_dlp_list_command = [
        'yt-dlp',
        '--dump-json',
        '--cookies', cookie_path,
        '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
        url
    ]

    stdout, stderr = await run_subprocess(yt_dlp_list_command, timeout=60, suppress_stdout_log=True)

    # Проверяем ошибки аутентификации
    if "This post may not be comfortable for some audiences" in stderr or "Log in for access" in stderr:
        raise Exception("TikTok требует аутентификации - пост может быть ограничен")

    if not stdout.strip():
        raise Exception("Не удалось получить информацию о TikTok видео")

    video_info = json.loads(stdout)

    logger.info("🎬 TikTok: Selecting best format under 50 MB...")
    candidate_formats = []
    for f in video_info.get('formats', []):
        if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
            filesize = f.get('filesize') or f.get('filesize_approx')
            if filesize and filesize < TELEGRAM_SIZE_LIMIT_BYTES:
                candidate_formats.append(f)

    if not candidate_formats:
        raise Exception("No suitable video formats found under 50 MB")

    best_format = sorted(candidate_formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), reverse=True)[0]
    chosen_format_str = best_format['format_id']
    logger.info(f"✅ TikTok: Selected best format ({best_format.get('height')}p) with ID: {chosen_format_str}")

    logger.info("⬬ TikTok: Downloading selected format...")
    yt_dlp_download_command = [
        'yt-dlp', url,
        '-f', chosen_format_str,
        '-o', os.path.join(temp_folder, 'final_video.%(ext)s'),
        '--no-warnings',
        '--cookies', cookie_path,
        '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36'
    ]

    await run_subprocess(yt_dlp_download_command)

    video_files = glob.glob(os.path.join(temp_folder, 'final_video.*'))
    if video_files:
        logger.info(f"✅ TikTok video successfully downloaded: {video_files[0]}")
        return video_files[0]
    else:
        raise Exception("yt-dlp did not create final file")

async def download_video_with_yt_dlp_tiktok(url: str, temp_folder: str) -> tuple[str | None, str | None]:
    """Скачивает TikTok видео с поддержкой cookies. Возвращает (video_path, error_message)"""

    # Сначала пробуем без cookies
    try:
        logger.info(f"🎬 TikTok: Trying without cookies first for URL: {url}")

        yt_dlp_list_command = ['yt-dlp', '--dump-json', url]
        stdout, stderr = await run_subprocess(yt_dlp_list_command, timeout=60, suppress_stdout_log=True)

        # Если в stderr есть сообщение об ограничении, переходим к cookies
        if "This post may not be comfortable for some audiences" in stderr or "Log in for access" in stderr:
            logger.info("🍪 TikTok: Authentication required, trying with cookies...")

            if not tiktok_cookie_rotator.cookie_files:
                logger.warning("❌ TikTok: No cookie files available for restricted content")
                return None, "Этот TikTok пост требует авторизации, но TikTok cookies не настроены"

            # Используем механизм ротации TikTok cookies
            try:
                video_path = await tiktok_cookie_rotator.try_with_all_cookies_async(
                    process_tiktok_with_cookie,
                    url,
                    temp_folder
                )
                return video_path, None
            except Exception as cookie_error:
                # Только здесь возвращаем None, None чтобы вызвать отправку админу
                logger.error(f"❌ All TikTok cookies failed: {cookie_error}")
                return None, None

        # Если нет ограничений, продолжаем обычную загрузку без cookies
        video_info = json.loads(stdout)

        logger.info("🎬 TikTok: Selecting best format under 50 MB...")
        candidate_formats = []
        for f in video_info.get('formats', []):
            if f.get('vcodec') != 'none' and f.get('acodec') != 'none':
                filesize = f.get('filesize') or f.get('filesize_approx')
                if filesize and filesize < TELEGRAM_SIZE_LIMIT_BYTES:
                    candidate_formats.append(f)

        if not candidate_formats:
            # Это ошибка без попытки cookies - не отправляем админу
            return None, "Нет подходящих форматов видео под 50 МБ"

        best_format = sorted(candidate_formats, key=lambda x: (x.get('height') or 0, x.get('tbr') or 0), reverse=True)[0]
        chosen_format_str = best_format['format_id']
        logger.info(f"✅ TikTok: Selected best format ({best_format.get('height')}p) with ID: {chosen_format_str}")

        logger.info("⬬ TikTok: Downloading selected format...")
        yt_dlp_download_command = [
            'yt-dlp', url, '-f', chosen_format_str,
            '-o', os.path.join(temp_folder, 'final_video.%(ext)s'), '--no-warnings'
        ]
        await run_subprocess(yt_dlp_download_command)

        video_files = glob.glob(os.path.join(temp_folder, 'final_video.*'))
        if video_files:
            logger.info(f"✅ TikTok video successfully downloaded: {video_files[0]}")
            return video_files[0], None
        else:
            # Это ошибка без попытки cookies - не отправляем админу
            return None, "Не удалось создать видеофайл"

    except Exception as e:
        # Это ошибка при попытке без cookies - не отправляем админу
        error_msg = str(e)
        logger.error(f"❌ Failed to download TikTok video without cookies: {e}")
        return None, f"Ошибка скачивания: {error_msg}"

async def download_video_with_yt_dlp_youtube_shorts(url: str, temp_folder: str) -> str | None:
    logger.info("🎬 YouTube Shorts: Looking for the best available file (not higher than 720p)...")
    format_selector = "best[height<=720][ext=mp4]/best[ext=mp4]/best[height<=720]/best"

    # Base yt-dlp command with extra reliability flags
    base_command = [
        'yt-dlp', '--rm-cache-dir', '--force-ipv4',
        '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0',
        '--add-header', 'Referer: https://www.youtube.com/',
        '--http-chunk-size', '10M',
        url,
        '--playlist-items', '1',
        '-f', format_selector,
        '-o', os.path.join(temp_folder, 'final_video.%(ext)s'),
        '--no-warnings'
    ]

    # Up to 3 retry attempts
    for attempt in range(1, 4):
        logger.info(f"▶️ YouTube Shorts: Attempt {attempt}/3")

        try:
            stdout, stderr = await run_subprocess(base_command)

            # Check for 403 error in stderr
            if "HTTP Error 403" in stderr:
                logger.warning("⚠️ HTTP 403 Forbidden detected. Retrying...")
                continue

            # Check if MP4 file exists
            video_files = glob.glob(os.path.join(temp_folder, '*.mp4'))
            if video_files:
                logger.info(f"✅ YouTube Shorts video successfully downloaded: {video_files[0]}")
                return video_files[0]
            else:
                logger.warning(f"⚠️ Downloaded file is not MP4 or was not found. Files: {video_files}")

        except Exception as e:
            logger.error(f"❌ yt-dlp failed on attempt {attempt}. Error: {str(e)}")

    logger.error("❌ All YouTube Shorts download attempts have failed.")
    return None

async def get_video_metadata(video_path: str) -> tuple[int | None, int | None, int | None]:
    try:
        logger.info("📋 Getting metadata from video file for 'smart' sending...")
        ffprobe_command = [
            'ffprobe', '-v', 'error', '-select_streams', 'v:0',
            '-show_entries', 'stream=width,height,duration', '-of', 'json', video_path
        ]
        stdout, stderr = await run_subprocess(ffprobe_command, timeout=60)
        video_info = json.loads(stdout)['streams'][0]
        width = int(video_info.get('width', 0))
        height = int(video_info.get('height', 0))
        duration = int(float(video_info.get('duration', 0)))
        logger.info(f"✅ Metadata obtained: {width}x{height}, {duration} sec.")
        return width, height, duration
    except Exception as e:
        logger.warning(f"⚠️ Failed to get metadata from video file: {e}. Sending as usual.")
        return None, None, None

# --- ОСНОВНЫЕ ОБРАБОТЧИКИ ---
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.id in ALLOWED_GROUP_IDS:
        await update.message.reply_html(f"Привет, {update.effective_user.mention_html()}!")

async def downloadmp3_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик команды /downloadmp3"""
    if update.effective_chat.id in ALLOWED_GROUP_IDS:
        try:
            await mp3_downloader.process_mp3_download(update, context)
        except Exception as e:
            logger.error(f"❌ Error processing MP3 download: {e}", exc_info=True)

            # Отправляем детальную ошибку админу
            user = update.effective_user
            error_details = f"MP3 download error\n"
            error_details += f"User: {user.username or user.first_name} (ID: {user.id})\n"
            error_details += f"Chat ID: {update.effective_chat.id}\n"
            error_details += f"Message ID: {update.message.message_id}\n"
            error_details += f"Command args: {context.args if context.args else 'No args'}\n\n"
            error_details += f"Exception: {str(e)}\n\n"
            error_details += f"Traceback:\n{traceback.format_exc()}"

            await send_error_to_admin(
                context,
                f"MP3 Download: Непредвиденная ошибка - {str(e)}",
                error_details,
                "MP3 Download"
            )

async def process_instagram_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    global _current_bot_context
    _current_bot_context = context

    chat_id, msg_id, user = update.effective_chat.id, update.message.message_id, update.effective_user

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="Проверяю содержимое Instagram поста... 🔍",
        reply_to_message_id=msg_id
    )

    temp_folder = os.path.join(TEMP_DOWNLOADS_DIR, f"insta_{chat_id}_{msg_id}")
    os.makedirs(temp_folder, exist_ok=True)
    success = False

    try:
        await status_msg.edit_text("Обрабатываю Instagram пост... ⏳")

        video_path, photo_message = await download_video_with_yt_dlp_instagram(url, temp_folder)

        if photo_message:
            # Это пост только с фотографиями
            await status_msg.edit_text(f"ℹ️ {photo_message}")
            logger.info(f"ℹ️ Instagram post contains no video: {url}")
            return

        if video_path:
            caption = f"Instagram <a href=\"{url}\">видео</a> отправил {user.mention_html()}"
            width, height, duration = await get_video_metadata(video_path)

            with open(video_path, 'rb') as vf:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=vf,
                    caption=caption,
                    parse_mode="HTML",
                    width=width,
                    height=height,
                    duration=duration,
                    supports_streaming=True
                )
            await context.bot.delete_message(chat_id, msg_id)
            success = True
        else:
            await status_msg.edit_text(
                "Не удалось скачать это видео. 😔\nВозможно, пост приватный, 18+ или аккаунты заблокированы."
            )
            # Отправляем ошибку админу только если это реальная ошибка скачивания
            error_details = f"Instagram download failed for URL: {url}\n"
            error_details += f"User: {user.username or user.first_name} (ID: {user.id})\n"
            error_details += f"Chat ID: {chat_id}\n"
            error_details += f"Message ID: {msg_id}\n"
            error_details += "All cookie files failed to download the video."

            await send_error_to_admin(
                context,
                "Instagram: Не удалось скачать видео после всех попыток cookie",
                error_details,
                "Instagram"
            )
    except Exception as e:
        logger.error(f"❌ Error processing Instagram: {e}", exc_info=True)
        await status_msg.edit_text("Произошла непредвиденная ошибка.")

        # Отправляем детальную ошибку админу
        error_details = f"Instagram processing error for URL: {url}\n"
        error_details += f"User: {user.username or user.first_name} (ID: {user.id})\n"
        error_details += f"Chat ID: {chat_id}\n"
        error_details += f"Message ID: {msg_id}\n\n"
        error_details += f"Exception: {str(e)}\n\n"
        error_details += f"Traceback:\n{traceback.format_exc()}"

        await send_error_to_admin(
            context,
            f"Instagram: Непредвиденная ошибка - {str(e)}",
            error_details,
            "Instagram"
        )
    finally:
        _current_bot_context = None
        if success:
            try: await status_msg.delete()
            except Exception: pass
        if os.path.exists(temp_folder):
            shutil.rmtree(temp_folder)

async def process_tiktok_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    global _current_bot_context
    _current_bot_context = context

    chat_id, msg_id, user = update.effective_chat.id, update.message.message_id, update.effective_user

    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="Обрабатываю TikTok видео... ⏳",
        reply_to_message_id=msg_id
    )

    temp_folder = os.path.join(TEMP_DOWNLOADS_DIR, f"tiktok_{chat_id}_{msg_id}")
    os.makedirs(temp_folder, exist_ok=True)
    success = False

    try:
        resolved_url = resolve_tiktok_url(url)
        video_path, error_message = await download_video_with_yt_dlp_tiktok(resolved_url, temp_folder)

        if error_message:
            # Показываем пользователю конкретную ошибку
            await status_msg.edit_text(f"ℹ️ {error_message}")
            logger.info(f"ℹ️ TikTok specific error: {error_message}")
            return

        if video_path:
            caption = f"TikTok <a href=\"{url}\">видео</a> отправил {user.mention_html()}"
            width, height, duration = await get_video_metadata(video_path)

            with open(video_path, 'rb') as vf:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=vf,
                    caption=caption,
                    parse_mode="HTML",
                    width=width,
                    height=height,
                    duration=duration,
                    supports_streaming=True
                )
            await context.bot.delete_message(chat_id, msg_id)
            success = True
        else:
            await status_msg.edit_text(
                "Не удалось скачать это видео. 😔\nВозможно, оно слишком большое или недоступно."
            )
            # Отправляем ошибку админу
            error_details = f"TikTok download failed for URL: {url}\n"
            error_details += f"Resolved URL: {resolved_url}\n"
            error_details += f"User: {user.username or user.first_name} (ID: {user.id})\n"
            error_details += f"Chat ID: {chat_id}\n"
            error_details += f"Message ID: {msg_id}\n"
            error_details += "Video download returned None - possibly too large or unavailable."

            await send_error_to_admin(
                context,
                "TikTok: Не удалось скачать видео",
                error_details,
                "TikTok"
            )
    except Exception as e:
        logger.error(f"❌ Error processing TikTok: {e}", exc_info=True)
        await status_msg.edit_text("Произошла непредвиденная ошибка. Попробуйте еще раз через минуту!")

        # Отправляем детальную ошибку админу
        error_details = f"TikTok processing error for URL: {url}\n"
        error_details += f"User: {user.username or user.first_name} (ID: {user.id})\n"
        error_details += f"Chat ID: {chat_id}\n"
        error_details += f"Message ID: {msg_id}\n\n"
        error_details += f"Exception: {str(e)}\n\n"
        error_details += f"Traceback:\n{traceback.format_exc()}"

        await send_error_to_admin(
            context,
            f"TikTok: Непредвиденная ошибка - {str(e)}",
            error_details,
            "TikTok"
        )
    finally:
        _current_bot_context = None
        if success:
            try: await status_msg.delete()
            except Exception: pass
        if os.path.exists(temp_folder):
            shutil.rmtree(temp_folder)

async def process_youtube_shorts_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    chat_id, msg_id, user = update.effective_chat.id, update.message.message_id, update.effective_user
    status_msg = await context.bot.send_message(
        chat_id=chat_id,
        text="Обрабатываю YouTube Shorts видео... ⏳",
        reply_to_message_id=msg_id
    )
    temp_folder = os.path.join(TEMP_DOWNLOADS_DIR, f"youtube_{chat_id}_{msg_id}")
    os.makedirs(temp_folder, exist_ok=True)
    success = False
    try:
        video_path = await download_video_with_yt_dlp_youtube_shorts(url, temp_folder)
        if video_path:
            caption = f"YouTube Shorts <a href=\"{url}\">видео</a> отправил {user.mention_html()}"
            width, height, duration = await get_video_metadata(video_path)

            with open(video_path, 'rb') as vf:
                await context.bot.send_video(
                    chat_id=chat_id,
                    video=vf,
                    caption=caption,
                    parse_mode="HTML",
                    width=width,
                    height=height,
                    duration=duration,
                    supports_streaming=True
                )
            await context.bot.delete_message(chat_id, msg_id)
            success = True
        else:
            await status_msg.edit_text(
                "Не удалось скачать это видео. 😔\nВозможно, видео недоступно."
            )
            # Отправляем ошибку админу
            error_details = f"YouTube Shorts download failed for URL: {url}\n"
            error_details += f"User: {user.username or user.first_name} (ID: {user.id})\n"
            error_details += f"Chat ID: {chat_id}\n"
            error_details += f"Message ID: {msg_id}\n"
            error_details += "All 3 download attempts failed."

            await send_error_to_admin(
                context,
                "YouTube Shorts: Не удалось скачать видео после всех попыток",
                error_details,
                "YouTube Shorts"
            )
    except Exception as e:
        logger.error(f"❌ Error processing YouTube Shorts: {e}", exc_info=True)
        await status_msg.edit_text("Произошла непредвиденная ошибка.")

        # Отправляем детальную ошибку админу
        error_details = f"YouTube Shorts processing error for URL: {url}\n"
        error_details += f"User: {user.username or user.first_name} (ID: {user.id})\n"
        error_details += f"Chat ID: {chat_id}\n"
        error_details += f"Message ID: {msg_id}\n\n"
        error_details += f"Exception: {str(e)}\n\n"
        error_details += f"Traceback:\n{traceback.format_exc()}"

        await send_error_to_admin(
            context,
            f"YouTube Shorts: Непредвиденная ошибка - {str(e)}",
            error_details,
            "YouTube Shorts"
        )
    finally:
        if success:
            try: await status_msg.delete()
            except Exception: pass
        if os.path.exists(temp_folder):
            shutil.rmtree(temp_folder)

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not (update.message and update.message.text and update.effective_chat.id in ALLOWED_GROUP_IDS): return
    text = update.message.text
    if insta_url := find_instagram_url(text):
        await process_instagram_link(update, context, insta_url)
    elif tiktok_url := find_tiktok_url(text):
        await process_tiktok_link(update, context, tiktok_url)
    elif youtube_shorts_url := find_youtube_shorts_url(text):
        await process_youtube_shorts_link(update, context, youtube_shorts_url)

async def setup_commands(application):
    """Настройка команд бота с описаниями"""
    commands = [
        ("downloadmp3", "Скачать MP3 из видео YouTube (использование: /downloadmp3 ссылка)")
    ]
    await application.bot.set_my_commands(commands)
    logger.info("✅ Bot commands configured successfully")

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Обработчик сетевых ошибок Telegram Bot API"""
    error = context.error

    if isinstance(error, NetworkError):
        logger.warning("🌐 Проблема с интернет-соединением при работе с Telegram API")
    elif isinstance(error, TimedOut):
        logger.warning("⏱️ Тайм-аут при обращении к Telegram API")
    elif isinstance(error, RetryAfter):
        logger.warning(f"🚫 Превышен лимит запросов Telegram API. Повтор через {error.retry_after} сек")
    else:
        # Для всех остальных ошибок логируем кратко
        logger.error(f"❌ Ошибка Telegram Bot API: {type(error).__name__}: {str(error)}")


def main():
    application = Application.builder().token(BOT_TOKEN).build()
    notification_manager.set_application(application)
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("downloadmp3", downloadmp3_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🚀 Bot successfully started!")

    async def post_init(application):
        await setup_commands(application)
        await notification_manager.send_startup_notification()
    application.post_init = post_init
    application.run_polling()

if __name__ == "__main__":
    main()
