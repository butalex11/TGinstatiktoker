import logging
import os
import glob
import asyncio
import json
import re
from telegram import Update
from telegram.ext import ContextTypes
import shutil

logger = logging.getLogger(__name__)

class MP3Downloader:
    def __init__(self, temp_downloads_dir: str, telegram_size_limit: int):
        self.temp_downloads_dir = temp_downloads_dir
        self.telegram_size_limit = telegram_size_limit

    def extract_url_from_command(self, text: str) -> str | None:
        """Извлекает URL из команды /downloadmp3"""
        # Ищем URL в кавычках или без них после команды
        patterns = [
            r'/downloadmp3\s+"([^"]+)"',  # URL в двойных кавычках
            r"/downloadmp3\s+'([^']+)'",  # URL в одинарных кавычках
            r'/downloadmp3\s+(\S+)',      # URL без кавычек
        ]

        for pattern in patterns:
            match = re.search(pattern, text)
            if match:
                return match.group(1)
        return None

    def is_supported_url(self, url: str) -> bool:
        """Проверяет, поддерживается ли URL для скачивания MP3"""
        supported_patterns = [
            r'youtube\.com/watch\?v=',
            r'youtube\.com/shorts/',
            r'youtu\.be/',
        ]

        return any(re.search(pattern, url) for pattern in supported_patterns)

    async def run_subprocess(self, command: list[str], timeout: int = 180) -> tuple[str, str]:
        """Выполняет команду в подпроцессе"""
        logger.info(f"🛠 Running MP3 command: {' '.join(command)}")

        process = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )

        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)

            if stdout:
                logger.info(f"[yt-dlp MP3 STDOUT]\n{stdout.decode(errors='ignore')}")
            if stderr:
                logger.warning(f"[yt-dlp MP3 STDERR]\n{stderr.decode(errors='ignore')}")

            return stdout.decode(), stderr.decode()

        except asyncio.TimeoutError:
            try:
                process.kill()
                raise TimeoutError(f"MP3 command timed out after {timeout} seconds")
            except ProcessLookupError:
                pass
            raise

    async def get_audio_info(self, url: str) -> dict | None:
        """Получает информацию об аудио для выбора лучшего качества"""
        try:
            command = ['yt-dlp', '--dump-json', '--no-warnings', url]
            stdout, stderr = await self.run_subprocess(command, timeout=60)

            if not stdout.strip():
                return None

            info = json.loads(stdout)
            return info

        except Exception as e:
            logger.error(f"❌ Failed to get audio info: {e}")
            return None

    async def download_mp3(self, url: str, temp_folder: str) -> str | None:
        """Скачивает MP3 в лучшем качестве"""
        try:
            logger.info(f"🎵 Starting MP3 download: {url}")

            # Команда для скачивания лучшего аудио и конвертации в MP3
            # yt-dlp сам выберет лучшее аудио и сконвертирует в MP3
            command = [
                'yt-dlp',
                url,
                '--extract-audio',              # Извлечь только аудио
                '--audio-format', 'mp3',        # Конвертировать в MP3
                '--audio-quality', '0',         # Лучшее качество (0 = лучшее)
                '--embed-metadata',             # Встроить метаданные
                '--add-metadata',               # Добавить метаданные
                '-o', os.path.join(temp_folder, '%(title)s.%(ext)s'),
                '--no-warnings',
                '--playlist-items', '1',        # Только один элемент если это плейлист
            ]

            # Для YouTube добавляем дополнительные заголовки
            if 'youtube.com' in url or 'youtu.be' in url:
                command.extend([
                    '--add-header', 'User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/138.0.0.0 Safari/537.36',
                    '--add-header', 'Referer: https://www.youtube.com/',
                ])

            await self.run_subprocess(command)

            # Ищем скачанный MP3 файл
            mp3_files = glob.glob(os.path.join(temp_folder, '*.mp3'))

            if not mp3_files:
                raise Exception("MP3 file was not created")

            mp3_path = mp3_files[0]

            # Проверяем размер файла
            file_size = os.path.getsize(mp3_path)
            if file_size > self.telegram_size_limit:
                logger.warning(f"⚠️ MP3 file too large: {file_size / (1024*1024):.1f} MB")
                return None

            logger.info(f"✅ MP3 successfully downloaded: {mp3_path} ({file_size / (1024*1024):.1f} MB)")
            return mp3_path

        except Exception as e:
            logger.error(f"❌ MP3 download failed: {e}")
            return None

    async def get_audio_metadata(self, audio_path: str) -> dict:
        """Получает метаданные аудиофайла"""
        try:
            command = [
                'ffprobe', '-v', 'error',
                '-show_entries', 'format=duration,size:stream=codec_name',
                '-of', 'json', audio_path
            ]

            stdout, stderr = await self.run_subprocess(command, timeout=30)
            info = json.loads(stdout)

            duration = float(info.get('format', {}).get('duration', 0))
            size = int(info.get('format', {}).get('size', 0))

            return {
                'duration': int(duration),
                'size_mb': round(size / (1024*1024), 1)
            }

        except Exception as e:
            logger.warning(f"⚠️ Failed to get audio metadata: {e}")
            return {'duration': None, 'size_mb': None}

    async def process_mp3_download(self, update: Update, context: ContextTypes.DEFAULT_TYPE):
        """Основная функция обработки команды /downloadmp3"""
        if not update.message or not update.message.text:
            return

        chat_id = update.effective_chat.id
        msg_id = update.message.message_id
        user = update.effective_user
        text = update.message.text

        logger.info(f"📥 MP3 download request from user {user.id} ({user.username or user.first_name}) in chat {chat_id}")

        # Извлекаем URL из команды
        url = self.extract_url_from_command(text)
        if not url:
            logger.warning(f"❌ Invalid command format from user {user.id}: {text}")
            await update.message.reply_text(
                "❌ Неверный формат команды!\n\n"
                "Используйте:\n"
                "`/downloadmp3 \"ссылка\"`\n"
                "или\n"
                "`/downloadmp3 ссылка`",
                parse_mode="Markdown"
            )
            return

        # Проверяем поддерживаемость URL
        if not self.is_supported_url(url):
            logger.warning(f"❌ Unsupported URL from user {user.id}: {url}")
            await update.message.reply_text(
                "❌ Данный тип ссылки не поддерживается!\n\n"
                "Поддерживаются:\n"
                "• YouTube (обычные видео и Shorts)\n"
            )
            return

        logger.info(f"🎯 Processing MP3 download for URL: {url}")

        # Отправляем статус сообщение
        status_msg = await context.bot.send_message(
            chat_id=chat_id,
            text=f"🎵 Начинаю скачивание MP3...\n🔗 {url}",
            reply_to_message_id=msg_id
        )

        temp_folder = os.path.join(self.temp_downloads_dir, f"mp3_{chat_id}_{msg_id}")
        os.makedirs(temp_folder, exist_ok=True)
        success = False

        try:
            # Обновляем статус
            await status_msg.edit_text(f"🎵 Скачиваю аудио в лучшем качестве...\n🔗 {url}")

            # Скачиваем MP3
            mp3_path = await self.download_mp3(url, temp_folder)

            if not mp3_path:
                logger.error(f"❌ MP3 download failed for URL: {url}")
                await status_msg.edit_text(
                    "❌ Не удалось скачать аудио.\n"
                    "Возможные причины:\n"
                    "• Файл слишком большой (>49MB)\n"
                    "• Видео недоступно или приватное\n"
                    "• Временные проблемы с сервисом"
                )
                return

            # Получаем метаданные
            metadata = await self.get_audio_metadata(mp3_path)

            # Формируем описание
            file_name = os.path.basename(mp3_path)
            caption = f"🎵 MP3 от {user.mention_html()}\n🔗 <a href=\"{url}\">Исходное видео</a>"

            if metadata['duration']:
                caption += f"\n⏱ Длительность: {metadata['duration']//60}:{metadata['duration']%60:02d}"
            if metadata['size_mb']:
                caption += f"\n📁 Размер: {metadata['size_mb']} MB"

            # Отправляем файл
            await status_msg.edit_text("📤 Отправляю MP3 файл...")

            logger.info(f"📤 Sending MP3 file: {file_name} ({metadata['size_mb']} MB)")

            with open(mp3_path, 'rb') as audio_file:
                await context.bot.send_audio(
                    chat_id=chat_id,
                    audio=audio_file,
                    caption=caption,
                    parse_mode="HTML",
                    duration=metadata['duration'],
                    title=file_name.replace('.mp3', '')
                )

            success = True
            await context.bot.delete_message(chat_id, msg_id)
            logger.info(f"✅ MP3 successfully sent to user {user.id} in chat {chat_id}")

        except Exception as e:
            logger.error(f"❌ Error processing MP3 request from user {user.id}: {e}", exc_info=True)
            await status_msg.edit_text(
                "❌ Произошла ошибка при обработке запроса.\n"
                "Попробуйте еще раз через несколько минут."
            )

        finally:
            # Удаляем статус сообщение при успехе
            if success:
                try:
                    await status_msg.delete()
                except Exception as e:
                    logger.warning(f"⚠️ Failed to delete status message: {e}")

            # Очищаем временную папку
            if os.path.exists(temp_folder):
                shutil.rmtree(temp_folder)
                logger.info(f"🧹 Cleaned up temp folder: {temp_folder}")
