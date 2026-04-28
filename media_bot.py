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
import time
from datetime import datetime
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import requests
from telegram.error import NetworkError, TimedOut, RetryAfter
from mp3_downloader import MP3Downloader

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
ADMIN_GROUP_ID = os.getenv("ADMIN_GROUP_ID")
TEMP_DOWNLOADS_DIR = "/app/bot_temp"
COOKIES_DIR = "/app/cookies"
TELEGRAM_SIZE_LIMIT_BYTES = 49 * 1024 * 1024 # 49 МБ для надежности
TELEGRAM_CONNECT_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_CONNECT_TIMEOUT_SECONDS", "30"))
TELEGRAM_READ_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_READ_TIMEOUT_SECONDS", "180"))
TELEGRAM_WRITE_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_WRITE_TIMEOUT_SECONDS", "180"))
TELEGRAM_POOL_TIMEOUT_SECONDS = int(os.getenv("TELEGRAM_POOL_TIMEOUT_SECONDS", "30"))
TELEGRAM_SEND_VIDEO_ATTEMPTS = int(os.getenv("TELEGRAM_SEND_VIDEO_ATTEMPTS", "4"))
TELEGRAM_SEND_VIDEO_RETRY_DELAY_SECONDS = int(os.getenv("TELEGRAM_SEND_VIDEO_RETRY_DELAY_SECONDS", "10"))
YTDLP_SOCKET_TIMEOUT_SECONDS = int(os.getenv("YTDLP_SOCKET_TIMEOUT_SECONDS", "60"))
YTDLP_RETRIES = int(os.getenv("YTDLP_RETRIES", "10"))
YTDLP_FRAGMENT_RETRIES = int(os.getenv("YTDLP_FRAGMENT_RETRIES", "10"))

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
_last_video_send_debug = ""

def get_ytdlp_network_options() -> list[str]:
    return [
        '--socket-timeout', str(YTDLP_SOCKET_TIMEOUT_SECONDS),
        '--retries', str(YTDLP_RETRIES),
        '--fragment-retries', str(YTDLP_FRAGMENT_RETRIES),
    ]

def format_file_debug_info(file_path: str | None) -> str:
    if not file_path:
        return "File: not provided"

    try:
        stat = os.stat(file_path)
        return (
            f"File path: {file_path}\n"
            f"File exists: yes\n"
            f"File size bytes: {stat.st_size}\n"
            f"File size MB: {stat.st_size / 1024 / 1024:.2f}\n"
            f"File modified: {datetime.fromtimestamp(stat.st_mtime).strftime('%d.%m.%Y %H:%M:%S')}"
        )
    except FileNotFoundError:
        return f"File path: {file_path}\nFile exists: no"
    except Exception as e:
        return f"File path: {file_path}\nFile info error: {type(e).__name__}: {e}"

def format_video_send_debug(
    platform: str,
    source_url: str,
    chat_id: int,
    message_id: int,
    user,
    video_path: str,
    width: int | None,
    height: int | None,
    duration: int | None,
    attempts: list[str],
) -> str:
    username = getattr(user, "username", None) or getattr(user, "first_name", "unknown")
    user_id = getattr(user, "id", "unknown")

    lines = [
        "TELEGRAM VIDEO SEND DEBUG:",
        "-" * 50,
        f"Platform: {platform}",
        f"Source URL: {source_url}",
        f"User: {username} (ID: {user_id})",
        f"Chat ID: {chat_id}",
        f"Message ID: {message_id}",
        f"Video metadata: width={width}, height={height}, duration={duration}",
        f"Configured attempts: {TELEGRAM_SEND_VIDEO_ATTEMPTS}",
        f"Timeouts: connect={TELEGRAM_CONNECT_TIMEOUT_SECONDS}s, read={TELEGRAM_READ_TIMEOUT_SECONDS}s, write={TELEGRAM_WRITE_TIMEOUT_SECONDS}s, pool={TELEGRAM_POOL_TIMEOUT_SECONDS}s",
        format_file_debug_info(video_path),
        "",
        "Attempts:",
    ]
    lines.extend(attempts or ["No attempts recorded"])
    return "\n".join(lines)

async def send_video_with_retries(
    context: ContextTypes.DEFAULT_TYPE,
    *,
    chat_id: int,
    video_path: str,
    caption: str,
    parse_mode: str,
    width: int | None,
    height: int | None,
    duration: int | None,
    supports_streaming: bool,
    platform: str,
    source_url: str,
    user,
    message_id: int,
):
    global _last_video_send_debug

    attempts_log = []
    last_error = None

    for attempt in range(1, TELEGRAM_SEND_VIDEO_ATTEMPTS + 1):
        started_at = time.monotonic()
        logger.info(
            f"Sending {platform} video to Telegram attempt {attempt}/{TELEGRAM_SEND_VIDEO_ATTEMPTS}. "
            f"{format_file_debug_info(video_path).replace(chr(10), '; ')}"
        )

        try:
            with open(video_path, 'rb') as vf:
                result = await context.bot.send_video(
                    chat_id=chat_id,
                    video=vf,
                    caption=caption,
                    parse_mode=parse_mode,
                    width=width,
                    height=height,
                    duration=duration,
                    supports_streaming=supports_streaming,
                    connect_timeout=TELEGRAM_CONNECT_TIMEOUT_SECONDS,
                    read_timeout=TELEGRAM_READ_TIMEOUT_SECONDS,
                    write_timeout=TELEGRAM_WRITE_TIMEOUT_SECONDS,
                    pool_timeout=TELEGRAM_POOL_TIMEOUT_SECONDS,
                )

            elapsed = time.monotonic() - started_at
            attempts_log.append(f"Attempt {attempt}: success in {elapsed:.1f}s")
            _last_video_send_debug = format_video_send_debug(
                platform, source_url, chat_id, message_id, user,
                video_path, width, height, duration, attempts_log
            )
            logger.info(f"Telegram send_video succeeded on attempt {attempt} in {elapsed:.1f}s")
            return result

        except RetryAfter as e:
            elapsed = time.monotonic() - started_at
            last_error = e
            retry_delay = int(e.retry_after) + 1
            attempts_log.append(
                f"Attempt {attempt}: RetryAfter after {elapsed:.1f}s; retry_after={e.retry_after}s"
            )
        except (TimedOut, NetworkError) as e:
            elapsed = time.monotonic() - started_at
            last_error = e
            retry_delay = TELEGRAM_SEND_VIDEO_RETRY_DELAY_SECONDS * attempt
            attempts_log.append(
                f"Attempt {attempt}: {type(e).__name__} after {elapsed:.1f}s; error={e}"
            )
        except Exception as e:
            elapsed = time.monotonic() - started_at
            attempts_log.append(
                f"Attempt {attempt}: non-retryable {type(e).__name__} after {elapsed:.1f}s; error={e}"
            )
            _last_video_send_debug = format_video_send_debug(
                platform, source_url, chat_id, message_id, user,
                video_path, width, height, duration, attempts_log
            )
            raise

        _last_video_send_debug = format_video_send_debug(
            platform, source_url, chat_id, message_id, user,
            video_path, width, height, duration, attempts_log
        )

        if attempt < TELEGRAM_SEND_VIDEO_ATTEMPTS:
            logger.warning(
                f"Telegram send_video failed on attempt {attempt}/{TELEGRAM_SEND_VIDEO_ATTEMPTS}; "
                f"retrying in {retry_delay}s: {type(last_error).__name__}: {last_error}"
            )
            await asyncio.sleep(retry_delay)

    logger.error(f"Telegram send_video failed after {TELEGRAM_SEND_VIDEO_ATTEMPTS} attempts")
    raise last_error

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

            if _last_video_send_debug.strip():
                tmp_file.write(f"\n\n{'='*50}\n")
                tmp_file.write("LAST TELEGRAM VIDEO SEND DEBUG:\n")
                tmp_file.write(f"{'-'*50}\n")
                tmp_file.write(_last_video_send_debug)

            tmp_file_path = tmp_file.name

        # Отправляем сообщение с файлом
        with open(tmp_file_path, 'rb') as error_file:
            await context.bot.send_document(
                chat_id=ADMIN_GROUP_ID,
                document=error_file,
                caption=admin_message,
                parse_mode="HTML",
                filename=f"error_{platform}_{timestamp.replace(':', '-').replace(' ', '_')}.txt",
                connect_timeout=TELEGRAM_CONNECT_TIMEOUT_SECONDS,
                read_timeout=TELEGRAM_READ_TIMEOUT_SECONDS,
                write_timeout=TELEGRAM_WRITE_TIMEOUT_SECONDS,
                pool_timeout=TELEGRAM_POOL_TIMEOUT_SECONDS,
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
    pattern = r"https?://(?:www\.|vm\.|vt\.)?tiktok\.com/(@[\w\.-]+/video/\d+|[\w-]+)"
    match = re.search(pattern, text)
    return match.group(0) if match else None

def find_youtube_shorts_url(text: str):
    pattern = r"(https?://(?:www\.)?youtube\.com/shorts/[a-zA-Z0-9_-]+)"
    match = re.search(pattern, text)
    return match.group(0) if match else None

def resolve_tiktok_url(url: str):
    if "vm.tiktok.com" in url or "vt.tiktok.com" in url:
        try: return requests.head(url, allow_redirects=True, timeout=10).url
        except requests.RequestException: return url
    return url

async def run_subprocess(command: list[str], timeout: int = 300, suppress_stdout_log: bool = False) -> tuple[str, str]:
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
        *get_ytdlp_network_options(),
        '--dump-json',
        '--no-warnings',
        '--playlist-items', '1',
        '--cookies', cookie_path,
        '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0',
        url
    ]

    # Подавляем STDOUT логирование для команды проверки
    stdout, stderr = await run_subprocess(check_command, timeout=90, suppress_stdout_log=True)

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
        '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0',
        *get_ytdlp_network_options()
    ]

    try:
        await run_subprocess(yt_dlp_download_command)
    except Exception:
        # План Б: fallback на любой best
        logger.warning("Failed to download format <= 720p, trying best available...")
        yt_dlp_download_command[5] = "best"
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
        *get_ytdlp_network_options(),
        '--dump-json',
        '--cookies', cookie_path,
        '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
        url
    ]

    stdout, stderr = await run_subprocess(yt_dlp_list_command, timeout=90, suppress_stdout_log=True)

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
        '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
        *get_ytdlp_network_options()
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

        yt_dlp_list_command = ['yt-dlp', *get_ytdlp_network_options(), '--dump-json', url]
        stdout, stderr = await run_subprocess(yt_dlp_list_command, timeout=90, suppress_stdout_log=True)

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
            '-o', os.path.join(temp_folder, 'final_video.%(ext)s'), '--no-warnings',
            *get_ytdlp_network_options()
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
    """Скачивает YouTube Shorts с гарантированным звуком, приоритет 720p и выше"""
    logger.info("🎬 YouTube Shorts: Getting available formats...")

    # Получаем информацию о доступных форматах
    info_command = [
        'yt-dlp', *get_ytdlp_network_options(), '--dump-json', '--no-warnings',
        '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0',
        '--add-header', 'Referer: https://www.youtube.com/',
        url
    ]

    # До 3 попыток для получения информации
    for info_attempt in range(1, 4):
        try:
            logger.info(f"🔍 YouTube Shorts: Getting info attempt {info_attempt}/3")
            stdout, stderr = await run_subprocess(info_command, timeout=90, suppress_stdout_log=True)

            if not stdout.strip():
                logger.warning(f"⚠️ Empty response on info attempt {info_attempt}")
                continue

            video_info = json.loads(stdout)
            break

        except Exception as e:
            logger.warning(f"❌ Info attempt {info_attempt} failed: {e}")
            if info_attempt == 3:
                logger.error("❌ All info attempts failed for YouTube Shorts")
                return None
            continue
    else:
        return None

    # Проверяем доступные форматы
    formats = video_info.get('formats', [])
    if not formats:
        logger.error("❌ No formats available")
        return None

    logger.info("🎯 YouTube Shorts: Analyzing available formats for video+audio...")

    # Группируем форматы: комбинированные (видео+аудио) и отдельные
    combined_formats = []  # Форматы с видео и аудио
    video_only_formats = []  # Только видео
    audio_formats = []  # Только аудио

    for fmt in formats:
        vcodec = fmt.get('vcodec', 'none')
        acodec = fmt.get('acodec', 'none')
        height = fmt.get('height', 0)
        ext = fmt.get('ext', '')
        filesize = fmt.get('filesize') or fmt.get('filesize_approx', 0)
        tbr = fmt.get('tbr', 0)
        format_id = fmt.get('format_id', '')

        if vcodec != 'none' and acodec != 'none':
            # Комбинированный формат (видео + аудио)
            combined_formats.append({
                'format_id': format_id,
                'height': height,
                'ext': ext,
                'filesize': filesize,
                'tbr': tbr,
                'is_mp4': ext.lower() == 'mp4',
                'type': 'combined'
            })
        elif vcodec != 'none' and acodec == 'none':
            # Только видео
            video_only_formats.append({
                'format_id': format_id,
                'height': height,
                'ext': ext,
                'filesize': filesize,
                'tbr': tbr,
                'is_mp4': ext.lower() == 'mp4',
                'type': 'video_only'
            })
        elif vcodec == 'none' and acodec != 'none':
            # Только аудио
            audio_formats.append({
                'format_id': format_id,
                'ext': ext,
                'filesize': filesize,
                'tbr': tbr,
                'abr': fmt.get('abr', 0),
                'type': 'audio_only'
            })

    logger.info(f"📊 Found formats: {len(combined_formats)} combined, {len(video_only_formats)} video-only, {len(audio_formats)} audio-only")

    base_command = [
        'yt-dlp', '--rm-cache-dir', '--force-ipv4',
        *get_ytdlp_network_options(),
        '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36 Edg/138.0.0.0',
        '--add-header', 'Referer: https://www.youtube.com/',
        '--http-chunk-size', '10M',
        url,
        '--playlist-items', '1',
        '-o', os.path.join(temp_folder, 'final_video.%(ext)s'),
        '--no-warnings'
    ]

    # СТРАТЕГИЯ 1: Пробуем комбинированные форматы (видео+аудио в одном файле)
    if combined_formats:
        logger.info("🔥 YouTube Shorts: Trying combined video+audio formats...")

        # Группируем по разрешению и сортируем: >= 720p сначала, потом < 720p
        resolution_groups = {}
        for fmt in combined_formats:
            height = fmt['height']
            if height not in resolution_groups:
                resolution_groups[height] = []
            resolution_groups[height].append(fmt)

        available_resolutions = sorted(resolution_groups.keys())
        high_quality = sorted([r for r in available_resolutions if r >= 720])
        low_quality = sorted([r for r in available_resolutions if r < 720], reverse=True)
        resolution_priority = high_quality + low_quality

        logger.info(f"🎯 Combined format resolution priority: {resolution_priority}")

        for resolution in resolution_priority:
            formats_for_resolution = resolution_groups[resolution]
            # Сортируем: MP4 сначала, потом по битрейту
            formats_for_resolution.sort(key=lambda x: (x['is_mp4'], x['tbr']), reverse=True)

            for fmt in formats_for_resolution:
                quality_tier = "🔥 PREFERRED" if resolution >= 720 else "💀 FALLBACK"

                # Проверяем размер файла
                if fmt['filesize'] and fmt['filesize'] > TELEGRAM_SIZE_LIMIT_BYTES:
                    logger.info(f"⚠️ {quality_tier} Skipping combined format {fmt['format_id']} ({resolution}p) - too large: {fmt['filesize']/1024/1024:.1f}MB")
                    continue

                logger.info(f"🎵 {quality_tier} Trying COMBINED format {fmt['format_id']} ({resolution}p, {fmt['ext']}) - guaranteed audio!")

                command = base_command.copy()
                command.extend(['-f', fmt['format_id']])

                try:
                    stdout, stderr = await run_subprocess(command, timeout=240)

                    if "HTTP Error 403" in stderr:
                        logger.warning("⚠️ HTTP 403 Forbidden detected, trying next format...")
                        continue

                    video_files = glob.glob(os.path.join(temp_folder, 'final_video.*'))
                    if video_files:
                        file_path = video_files[0]
                        file_size = os.path.getsize(file_path)

                        if file_size > TELEGRAM_SIZE_LIMIT_BYTES:
                            logger.warning(f"⚠️ Downloaded file too large: {file_size/1024/1024:.1f}MB, removing and trying next format")
                            os.remove(file_path)
                            continue

                        quality_log = "🔥 EXCELLENT" if resolution >= 720 else "💀 ACCEPTABLE"
                        logger.info(f"✅ {quality_log} SUCCESS! Downloaded {resolution}p COMBINED video+audio: {file_path} ({file_size/1024/1024:.1f}MB)")
                        return file_path
                    else:
                        logger.warning(f"⚠️ No video file created for combined format {fmt['format_id']}")

                except Exception as e:
                    logger.warning(f"❌ Failed combined format {fmt['format_id']} ({resolution}p): {str(e)}")

    # СТРАТЕГИЯ 2: Используем умные селекторы yt-dlp для автоматического объединения
    logger.info("🔄 YouTube Shorts: Trying smart format selectors for auto-merging...")
    smart_selectors = [
        # Приоритет: лучшее качество с аудио >= 720p
        "bestvideo[height>=720]+bestaudio/best[height>=720]",
        # Лучшее качество с аудио любого разрешения
        "bestvideo+bestaudio/best",
        # MP4 с аудио >= 720p
        "bestvideo[height>=720][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height>=720]+bestaudio",
        # MP4 с аудио любого качества
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio",
        # Запасные варианты
        "best[height>=720][ext=mp4]/best[ext=mp4]",
        "best[height>=720]/best"
    ]

    for selector in smart_selectors:
        selector_type = "🔥 PREFERRED" if ">=720" in selector else "💀 FALLBACK"
        has_audio_guarantee = "+bestaudio" in selector or "bestvideo+bestaudio" in selector
        audio_note = "🎵 GUARANTEED AUDIO" if has_audio_guarantee else "⚠️ may lack audio"

        logger.info(f"🎯 {selector_type} Trying smart selector: {selector} ({audio_note})")

        command = base_command.copy()
        command.extend(['-f', selector])

        # Если селектор содержит объединение, добавляем флаг для merge
        if '+' in selector:
            command.extend(['--merge-output-format', 'mp4'])

        try:
            stdout, stderr = await run_subprocess(command, timeout=300)  # Больше времени для merge

            video_files = glob.glob(os.path.join(temp_folder, 'final_video.*'))
            if video_files:
                file_path = video_files[0]
                file_size = os.path.getsize(file_path)

                if file_size > TELEGRAM_SIZE_LIMIT_BYTES:
                    logger.warning(f"⚠️ Smart selector file too large: {file_size/1024/1024:.1f}MB")
                    os.remove(file_path)
                    continue

                audio_status = "🎵 WITH AUDIO" if has_audio_guarantee else "❓ audio unknown"
                logger.info(f"✅ SUCCESS with smart selector {selector}: {file_path} ({file_size/1024/1024:.1f}MB) {audio_status}")
                return file_path

        except Exception as e:
            logger.warning(f"❌ Failed smart selector {selector}: {str(e)}")

    # СТРАТЕГИЯ 3: Последний шанс - простые селекторы
    logger.info("🆘 YouTube Shorts: Last resort - simple selectors...")
    simple_selectors = ["best", "worst"]

    for selector in simple_selectors:
        logger.info(f"🆘 LAST RESORT: Trying simple selector: {selector}")

        command = base_command.copy()
        command.extend(['-f', selector])

        try:
            stdout, stderr = await run_subprocess(command, timeout=240)

            video_files = glob.glob(os.path.join(temp_folder, 'final_video.*'))
            if video_files:
                file_path = video_files[0]
                file_size = os.path.getsize(file_path)

                if file_size > TELEGRAM_SIZE_LIMIT_BYTES:
                    logger.warning(f"⚠️ Last resort file too large: {file_size/1024/1024:.1f}MB")
                    os.remove(file_path)
                    continue

                logger.info(f"✅ LAST RESORT SUCCESS with {selector}: {file_path} ({file_size/1024/1024:.1f}MB)")
                return file_path

        except Exception as e:
            logger.warning(f"❌ Failed last resort selector {selector}: {str(e)}")

    logger.error("❌ All YouTube Shorts download strategies failed")
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
    import traceback
    import shutil
    import os  # ✅ используем глобальный импорт, без переопределений

    global _current_bot_context, _last_video_send_debug
    _current_bot_context = context
    _last_video_send_debug = ""

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
            # 🔹 Проверяем размер файла ДО отправки
            file_size = os.path.getsize(video_path)
            if file_size > 50 * 1024 * 1024:  # 50 MB
                await status_msg.edit_text("⚠️ Сори, видео больше 50 МБ, а других форматов нет 😔")
                logger.warning(f"Video too large to send: {file_size / (1024*1024):.2f} MB")
                return  # Прерываем выполнение, не пытаемся отправить

            # 🔹 Получаем метаданные и отправляем видео
            caption = f"Instagram <a href=\"{url}\">видео</a> отправил {user.mention_html()}"
            width, height, duration = await get_video_metadata(video_path)

            await send_video_with_retries(
                context,
                chat_id=chat_id,
                video_path=video_path,
                caption=caption,
                parse_mode="HTML",
                width=width,
                height=height,
                duration=duration,
                supports_streaming=True,
                platform="Instagram",
                source_url=url,
                user=user,
                message_id=msg_id,
            )

            await context.bot.delete_message(chat_id, msg_id)
            success = True

        else:
            # Если видео не удалось скачать
            await status_msg.edit_text(
                "Не удалось скачать это видео. 😔\nВозможно, пост приватный, 18+ или аккаунты заблокированы."
            )
            error_details = (
                f"Instagram download failed for URL: {url}\n"
                f"User: {user.username or user.first_name} (ID: {user.id})\n"
                f"Chat ID: {chat_id}\n"
                f"Message ID: {msg_id}\n"
                "All cookie files failed to download the video."
            )

            await send_error_to_admin(
                context,
                "Instagram: Не удалось скачать видео после всех попыток cookie",
                error_details,
                "Instagram"
            )

    except Exception as e:
        logger.error(f"❌ Error processing Instagram: {e}", exc_info=True)
        await status_msg.edit_text("Произошла непредвиденная ошибка. 😔")

        # Отправляем детальную ошибку админу
        error_details = (
            f"Instagram processing error for URL: {url}\n"
            f"User: {user.username or user.first_name} (ID: {user.id})\n"
            f"Chat ID: {chat_id}\n"
            f"Message ID: {msg_id}\n\n"
            f"Exception: {str(e)}\n\n"
            f"Traceback:\n{traceback.format_exc()}"
        )

        await send_error_to_admin(
            context,
            f"Instagram: Непредвиденная ошибка - {str(e)}",
            error_details,
            "Instagram"
        )

    finally:
        _current_bot_context = None

        if success:
            try:
                await status_msg.delete()
            except Exception:
                pass

        # 🔹 Безопасно удаляем временную папку
        try:
            if os.path.exists(temp_folder):
                shutil.rmtree(temp_folder)
        except Exception as cleanup_error:
            logger.warning(f"Не удалось удалить временную папку {temp_folder}: {cleanup_error}")

async def process_tiktok_link(update: Update, context: ContextTypes.DEFAULT_TYPE, url: str):
    global _current_bot_context, _last_video_send_debug
    _current_bot_context = context
    _last_video_send_debug = ""

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

            await send_video_with_retries(
                context,
                chat_id=chat_id,
                video_path=video_path,
                caption=caption,
                parse_mode="HTML",
                width=width,
                height=height,
                duration=duration,
                supports_streaming=True,
                platform="TikTok",
                source_url=resolved_url,
                user=user,
                message_id=msg_id,
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
    global _last_video_send_debug
    _last_video_send_debug = ""
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

            await send_video_with_retries(
                context,
                chat_id=chat_id,
                video_path=video_path,
                caption=caption,
                parse_mode="HTML",
                width=width,
                height=height,
                duration=duration,
                supports_streaming=True,
                platform="YouTube Shorts",
                source_url=url,
                user=user,
                message_id=msg_id,
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
            error_details += "All quality-priority download attempts failed."

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
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .connect_timeout(TELEGRAM_CONNECT_TIMEOUT_SECONDS)
        .read_timeout(TELEGRAM_READ_TIMEOUT_SECONDS)
        .write_timeout(TELEGRAM_WRITE_TIMEOUT_SECONDS)
        .pool_timeout(TELEGRAM_POOL_TIMEOUT_SECONDS)
        .build()
    )
    application.add_error_handler(error_handler)
    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("downloadmp3", downloadmp3_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    logger.info("🚀 Bot successfully started!")

    async def post_init(application):
        await setup_commands(application)

    application.post_init = post_init
    application.run_polling()

if __name__ == "__main__":
    main()
