import logging
import signal
import atexit
import asyncio
import os
from typing import Set

logger = logging.getLogger(__name__)

class BotNotificationManager:
    """Менеджер уведомлений о запуске и остановке бота"""
    
    def __init__(self, allowed_group_ids: Set[int], enabled: bool = True):
        self.allowed_group_ids = allowed_group_ids
        self.enabled = enabled
        self.application = None
        
        if self.enabled:
            logger.info("🔔 Система уведомлений активирована")
        else:
            logger.info("🔕 Система уведомлений отключена")
    
    def set_application(self, application):
        """Устанавливает экземпляр Telegram Application"""
        self.application = application
        if self.enabled:
            self.setup_signal_handlers()
    
    async def send_startup_notification(self):
        """Отправляет уведомление о запуске бота во все разрешенные группы"""
        if not self.enabled or not self.application:
            return
            
        logger.info("📢 Отправляю уведомления о запуске...")
        
        for group_id in self.allowed_group_ids:
            try:
                await self.application.bot.send_message(
                    chat_id=group_id,
                    text="🚀 Я снова в строю! 🚀",
                    disable_notification=True  # Беззвучное уведомление
                )
                logger.info(f"✅ Уведомление о запуске отправлено в группу {group_id}")
            except Exception as e:
                logger.error(f"❌ Не удалось отправить уведомление о запуске в группу {group_id}: {e}")
    
    async def send_shutdown_notification(self):
        """Отправляет уведомление об остановке бота во все разрешенные группы"""
        if not self.enabled or not self.application:
            return
            
        logger.info("📢 Отправляю уведомления об остановке...")
        
        for group_id in self.allowed_group_ids:
            try:
                await self.application.bot.send_message(
                    chat_id=group_id,
                    text="🛠 Ушел на профилактику 🛠",
                    disable_notification=True  # Беззвучное уведомление
                )
                logger.info(f"✅ Уведомление об остановке отправлено в группу {group_id}")
            except Exception as e:
                logger.error(f"❌ Не удалось отправить уведомление об остановке в группу {group_id}: {e}")
    
    def setup_signal_handlers(self):
        """Настраивает обработчики сигналов для корректного завершения работы"""
        
        def signal_handler(signum, frame):
            logger.info(f"🛑 Получен сигнал {signum}, начинаю завершение работы...")
            
            # Создаем event loop если его нет
            try:
                loop = asyncio.get_event_loop()
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
            
            # Отправляем уведомление об остановке
            try:
                loop.run_until_complete(self.send_shutdown_notification())
            except Exception as e:
                logger.error(f"❌ Ошибка при отправке уведомления об остановке: {e}")
            
            logger.info("👋 Бот завершает работу...")
            exit(0)
        
        # Регистрируем обработчики для различных сигналов
        signal.signal(signal.SIGTERM, signal_handler)  # Docker stop
        signal.signal(signal.SIGINT, signal_handler)   # Ctrl+C
        
        # Дополнительная защита через atexit
        atexit.register(lambda: logger.info("🔚 Процесс завершен"))
        
        logger.info("🛡️ Обработчики сигналов настроены")

def create_notification_manager(group_ids_str: str, notifications_enabled_str: str) -> BotNotificationManager:
    """Создает экземпляр менеджера уведомлений на основе переменных окружения"""
    
    # Парсим группы
    try:
        allowed_group_ids = {int(group_id.strip()) for group_id in group_ids_str.split(',')}
    except (ValueError, TypeError, AttributeError):
        logger.error("❌ Неверный формат ALLOWED_GROUP_IDS для уведомлений")
        allowed_group_ids = set()
    
    # Определяем включены ли уведомления
    enabled = notifications_enabled_str and notifications_enabled_str.lower() in ('yes', 'true', '1', 'on', 'enabled')
    
    return BotNotificationManager(allowed_group_ids, enabled)
