from __future__ import annotations
import json
import asyncio
import logging
import time
import threading
import os
import re
import random
from typing import TYPE_CHECKING, Dict, Optional, Set, List, Tuple
from threading import Lock
from datetime import datetime
from collections import defaultdict
from enum import Enum
from dataclasses import dataclass

# Заменяем Telethon на Kurigram (форк Pyrogram)
try:
    from pyrogram import Client, errors, raw, types as pytypes
    from pyrogram.enums import ParseMode
except ImportError:
    import subprocess
    import sys
    subprocess.check_call([sys.executable, "-m", "pip", "install", "kurigram"])
    from pyrogram import Client, errors, raw, types as pytypes
    from pyrogram.enums import ParseMode

if TYPE_CHECKING:
    from cardinal import Cardinal
from tg_bot import CBT
import tg_bot.static_keyboards
from telebot.types import InlineKeyboardMarkup as K, InlineKeyboardButton as B
import requests

logger = logging.getLogger("FPC.StarsGifts")
logger.setLevel(logging.INFO)

log_file = "storage/logs/stars_gifts.log"
os.makedirs("storage/logs", exist_ok=True)
file_handler = logging.FileHandler(log_file, encoding='utf-8')
file_handler.setLevel(logging.INFO)
formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
logger.addHandler(file_handler)

NAME = "Telegram Stars Gifts (Kurigram)"
VERSION = "1.2.0"
DESCRIPTION = "Автопродажа подарков тг (поддержка Kurigram)"
CREDITS = ''
UUID = "6c7adbc9-c1ca-419c-9fe1-0056c9d33258"
SETTINGS_PAGE = True

REQUIREMENTS = ["kurigram","requests","curl_cffi"]

SETTINGS = {
    "api_sessions": [],
    "gift_lots": [],
    "gift_settings": {
        "message": "",
        "hide_name": True,
        "auto_send": True,
        "skip_gift_selection": False
    },
    "statistics": {
        "total_sent": 0,
        "total_revenue": 0,
        "daily_stats": {},
        "weekly_stats": {},
        "monthly_stats": {}
    }
}

api_sessions = []
gift_lots = []
gift_settings = SETTINGS["gift_settings"].copy()
statistics = SETTINGS["statistics"].copy()

class OrderState(Enum):
    WAITING_USERNAME = "waiting_username"
    WAITING_GIFT_CHOICE = "waiting_gift_choice"
    WAITING_CONFIRMATION = "waiting_confirmation"
    WAITING_NEW_USERNAME = "waiting_new_username"
    WAITING_SETTINGS = "waiting_settings"
    PROCESSING_SENDING = "processing_sending"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUNDED = "refunded"

@dataclass
class OrderContext:
    order_id: str
    buyer_username: str
    target_username: Optional[str]
    stars: int
    amount: int
    state: OrderState = OrderState.WAITING_USERNAME
    gift_choice: Optional[str] = None
    gift: Optional[Dict] = None
    created_at: float = time.time()
    last_message_at: float = time.time()
    hide_name: Optional[bool] = None
    message: Optional[str] = None
    
    def update_state(self, state: OrderState):
        self.state = state
        self.last_message_at = time.time()

    def get_hide_name(self) -> bool:
        return self.hide_name if self.hide_name is not None else gift_settings.get("hide_name", True)
    
    def get_message(self) -> str:
        return self.message if self.message is not None else gift_settings.get("message", "")

class StarGiftSender:
    """Отправитель подарков на основе Kurigram (Pyrogram)"""
    
    def __init__(self, session_name: str, api_id: str, api_hash: str, session_string: str = None):
        self.session_name = session_name
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.session_string = session_string
        self.client: Optional[Client] = None
        self.initialized = False
        self.isFlood = False
        self.flood_until = 0
        self.lowbalance = False
        self.balance: Optional[int] = None
        self._lock = Lock()
        self._me_peer = None
        self._peer_cache: Dict[str, any] = {}
        
    def _create_client(self) -> Client:
        """Создает клиент Kurigram/Pyrogram"""
        session_path = f"storage/sessions/{self.session_name}"
        
        if self.session_string:
            # Используем строку сессии в памяти
            return Client(
                name=session_path,
                api_id=self.api_id,
                api_hash=self.api_hash,
                session_string=self.session_string,
                in_memory=True
            )
        else:
            # Используем файл сессии
            os.makedirs("storage/sessions", exist_ok=True)
            return Client(
                name=session_path,
                api_id=self.api_id,
                api_hash=self.api_hash
            )
    
    async def _check_session_valid(self) -> Tuple[bool, str]:
        """Проверяет, валидна ли сессия"""
        client = None
        try:
            client = self._create_client()
            await client.connect()
            
            if await client.get_me():
                me = await client.get_me()
                username = f"@{me.username}" if me.username else f"ID:{me.id}"
                return True, f"Активна ({username})"
            return False, "Не авторизована"
            
        except Exception as e:
            return False, f"Ошибка: {str(e)[:50]}"
        finally:
            if client and client.is_connected:
                await client.disconnect()
    
    async def _authorize(self, phone: str = None, code: str = None, password: str = None) -> Tuple[bool, str]:
        """Авторизация сессии"""
        client = self._create_client()
        
        try:
            await client.connect()
            
            # Проверяем, уже авторизованы
            if await client.get_me():
                await client.disconnect()
                self.initialized = True
                logger.info(f"Сессия {self.session_name} уже авторизована")
                return True, "Уже авторизован"
            
            if not phone:
                return False, "Не указан номер телефона"
            
            if not code:
                # Отправляем код
                sent_code = await client.send_code(phone)
                self.auth_data = {'phone': phone, 'phone_code_hash': sent_code.phone_code_hash}
                logger.info(f"Код отправлен для {self.session_name}")
                return True, "Код отправлен"
            
            # Вводим код
            try:
                await client.sign_in(
                    phone_number=self.auth_data.get('phone', phone),
                    code=code,
                    phone_code_hash=self.auth_data.get('phone_code_hash')
                )
            except errors.SessionPasswordNeeded:
                if not password:
                    return True, "Требуется пароль 2FA"
                await client.check_password(password)
            
            if await client.get_me():
                # Сохраняем строку сессии если ее еще нет
                if not self.session_string:
                    self.session_string = await client.export_session_string()
                self.initialized = True
                logger.info(f"Сессия {self.session_name} успешно авторизована")
                return True, "Успешная авторизация"
            
            return False, "Не удалось авторизоваться"
            
        except errors.FloodWait as e:
            logger.warning(f"FloodWait при авторизации {self.session_name}: {e.value}s")
            return False, f"Флуд: ждите {e.value}с"
        except Exception as e:
            logger.error(f"Ошибка авторизации {self.session_name}: {e}")
            return False, f"Ошибка: {str(e)[:100]}"
        finally:
            if client and client.is_connected:
                await client.disconnect()
    
    async def _get_balance(self) -> int:
        """Получает баланс звезд"""
        client = None
        try:
            client = self._create_client()
            await client.connect()
            
            me = await client.get_me()
            if self._me_peer is None:
                self._me_peer = await client.resolve_peer(me.id)
            result = await client.invoke(
                raw.functions.payments.GetStarsStatus(
                    peer=self._me_peer
                )
            )
            
            # Правильное извлечение баланса из объекта StarsAmount
            if hasattr(result, 'balance'):
                # Если balance - это объект StarsAmount
                if hasattr(result.balance, 'amount'):
                    balance = result.balance.amount
                else:
                    balance = int(result.balance) if result.balance else 0
            else:
                balance = 0
                
            self.lowbalance = balance < 100 if isinstance(balance, (int, float)) else False
            self.balance = balance if isinstance(balance, int) else 0
            
            logger.info(f"Баланс сессии {self.session_name}: {self.balance} звезд")
            return self.balance
            
        except errors.FloodWait as e:
            logger.warning(f"FloodWait при получении баланса {self.session_name}: {e.value}s")
            self.isFlood = True
            self.flood_until = time.time() + e.value
            return 0
        except Exception as e:
            logger.error(f"Ошибка получения баланса {self.session_name}: {e}")
            self.initialized = False
            return 0
        finally:
            if client and client.is_connected:
                await client.disconnect()

    async def _send_star(
        self, 
        gift_id: int, 
        username: str, 
        message: str = None, 
        hide_name: bool = True,
        stars_cost: int = 1
    ) -> Tuple[bool, str]:
        """Отправляет подарок"""
        
        if self.balance is not None and self.balance < stars_cost:
            self.lowbalance = True
            return False, "BALANCE_TOO_LOW"
        
        if self.isFlood and self.flood_until > time.time():
            wait_time = int(self.flood_until - time.time())
            return False, f"FLOOD_WAIT_{wait_time}"
        
        if self.isFlood and self.flood_until <= time.time():
            self.isFlood = False
            self.flood_until = 0
        
        client = None
        try:
            client = self._create_client()
            await client.connect()
            
            # Получаем получателя (кэшируем peer)
            peer_key = f"user_{username}"
            if peer_key not in self._peer_cache:
                try:
                    self._peer_cache[peer_key] = await client.resolve_peer(username)
                except Exception as e:
                    logger.error(f"Не удалось найти пользователя @{username}: {e}")
                    return False, "NO_USER"
            recipient = self._peer_cache[peer_key]
            
            # Подготовка сообщения
            msg_obj = None
            if message:
                msg_obj = raw.types.TextWithEntities(text=message, entities=[])
            
            # Создаем инвойс для подарка
            invoice = raw.types.InputInvoiceStarGift(
                peer=recipient,
                gift_id=int(gift_id),
                hide_name=hide_name,
                message=msg_obj
            )
            
            # Получаем форму оплаты
            try:
                payment_form = await client.invoke(
                    raw.functions.payments.GetPaymentForm(invoice=invoice)
                )
                
                # Отправляем звезды
                await client.invoke(
                    raw.functions.payments.SendStarsForm(
                        form_id=payment_form.form_id,
                        invoice=invoice
                    )
                )
                
                self.balance -= stars_cost
                logger.info(f"Подарок {gift_id} отправлен @{username} через {self.session_name} (остаток: {self.balance})")
                return True, "Подарок отправлен"
                
            except errors.FloodWait as e:
                self.isFlood = True
                self.flood_until = time.time() + e.value
                logger.warning(f"Флуд {self.session_name}: ждите {e.value}с")
                return False, f"FLOOD_WAIT_{e.value}"
                
            except errors.BadRequest as e:
                error_str = str(e)
                if "STARGIFT_MESSAGE_INVALID" in error_str:
                    return False, "PAID_MESSAGES_ENABLED"
                if "USER_DISALLOWED_STARGIFTS" in error_str:
                    return False, "USER_DISALLOWED_STARGIFTS"
                if "BALANCE_TOO_LOW" in error_str:
                    self.lowbalance = True
                    return False, "BALANCE_TOO_LOW"
                return False, f"BadRequest: {error_str}"
                
            except Exception as e:
                logger.error(f"Ошибка отправки {self.session_name}: {e}")
                error_str = str(e)
                if "BALANCE_TOO_LOW" in error_str:
                    self.lowbalance = True
                    return False, "BALANCE_TOO_LOW"
                if "USERNAME_NOT_OCCUPIED" in error_str or "USER_ID_INVALID" in error_str:
                    return False, "NO_USER"
                if "STARGIFT_MESSAGE_INVALID" in error_str:
                    return False, "PAID_MESSAGES_ENABLED"
                if "USER_DISALLOWED_STARGIFTS" in error_str:
                    return False, "USER_DISALLOWED_STARGIFTS"
                return False, str(e)
                
        except Exception as e:
            logger.error(f"Критическая ошибка отправки {self.session_name}: {e}")
            return False, str(e)
        finally:
            if client and client.is_connected:
                await client.disconnect()
    
    def initialize(self) -> Tuple[bool, str]:
        """Синхронная обертка для проверки сессии"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            success, msg = loop.run_until_complete(self._check_session_valid())
            self.initialized = success
            return success, msg
        finally:
            loop.close()
    
    def get_balance(self) -> int:
        """Синхронная обертка для получения баланса"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(self._get_balance())
            # Убеждаемся, что возвращаем int
            return int(result) if result is not None else 0
        except Exception as e:
            logger.error(f"Ошибка в get_balance: {e}")
            return 0
        finally:
            loop.close()
    
    def authorize(self, phone: str = None, code: str = None, password: str = None) -> Tuple[bool, str]:
        """Синхронная обертка для авторизации"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self._authorize(phone, code, password))
        finally:
            loop.close()
    
    def send_star(
        self, 
        gift_id: int, 
        username: str, 
        message: str = None, 
        hide_name: bool = None,
        stars_cost: int = 1
    ) -> Tuple[bool, str]:
        """Синхронная обертка для отправки подарка"""
        if hide_name is None:
            hide_name = gift_settings.get("hide_name", True)
            
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self._send_star(gift_id, username, message, hide_name, stars_cost))
        finally:
            loop.close()
    
    async def a_send_star(
        self, 
        gift_id: int, 
        username: str, 
        message: str = None, 
        hide_name: bool = None,
        stars_cost: int = 1
    ) -> Tuple[bool, str]:
        """Асинхронная отправка подарка"""
        if hide_name is None:
            hide_name = gift_settings.get("hide_name", True)
        return await self._send_star(gift_id, username, message, hide_name, stars_cost)
    
    async def a_get_balance(self) -> int:
        """Асинхронное получение баланса"""
        result = await self._get_balance()
        return int(result) if result is not None else 0
    
    async def _delete_chat(self, username: str) -> bool:
        """Удаляет диалог с пользователем из списка чатов отправителя"""
        client = None
        try:
            client = self._create_client()
            await client.connect()
            peer = await client.resolve_peer(username)
            
            await client.invoke(
                raw.functions.messages.DeleteHistory(
                    peer=peer,
                    max_id=0,
                    just_clear=False,
                    revoke=False
                )
            )
            logger.info(f"Чат с @{username} удален из диалогов сессии {self.session_name}")
            return True
        except Exception as e:
            logger.warning(f"Не удалось удалить чат с @{username} из сессии {self.session_name}: {e}")
            return False
        finally:
            if client and client.is_connected:
                await client.disconnect()
    
    def delete_chat(self, username: str) -> bool:
        """Синхронная обертка для удаления чата"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self._delete_chat(username))
        finally:
            loop.close()
    
    def export_session_string(self) -> Tuple[Optional[str], str]:
        """Экспортирует строку сессии"""
        if not self.initialized:
            return None, "Сессия не активна"
        
        if self.session_string:
            return self.session_string, "Успешно"
        
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        
        async def _export():
            client = self._create_client()
            try:
                await client.connect()
                if await client.get_me():
                    return await client.export_session_string()
                return None
            finally:
                if client.is_connected:
                    await client.disconnect()
        
        try:
            result = loop.run_until_complete(_export())
            if result:
                return result, "Успешно"
            return None, "Не удалось экспортировать"
        finally:
            loop.close()

class GiftsManager:
    """Менеджер подарков и сессий"""
    
    GIFTS = {
        13: {
            "1": {"name": "Мишка", "id": 5170233102089322756},
            "2": {"name": "Сердечко", "id": 5170145012310081615}
        },
        21: {
            "1": {"name": "Подарок", "id": 5170250947678437525},
            "2": {"name": "Роза", "id": 5168103777563050263}
        },
        43: {
            "1": {"name": "Торт", "id": 5170144170496491616},
            "2": {"name": "Цветы", "id": 5170314324215857265},
            "3": {"name": "Ракета", "id": 5170564780938756245},
            "4": {"name": "Шампанское", "id": 6028601630662853006}
        },
        85: {
            "1": {"name": "Кубок", "id": 5168043875654172773},
            "2": {"name": "Кольцо", "id": 5170690322832818290},
            "3": {"name": "Бриллиант", "id": 5170521118301225164}
        }
    }
    
    def __init__(self, sessions_data: List[Dict]):
        self.senders: List[StarGiftSender] = []
        for session in sessions_data:
            sender = StarGiftSender(
                session.get("session_name", "unknown"),
                session.get("api_id"),
                session.get("api_hash"),
                session.get("session_string")
            )
            self.senders.append(sender)
        self._sender_index = 0
        self._sender_lock = Lock()
        logger.info(f"Создан GiftsManager с {len(self.senders)} сессиями")
    
    def initialize_all_sessions(self) -> List[Tuple[str, bool, str]]:
        """Инициализирует все сессии"""
        results = []
        for sender in self.senders:
            success, msg = sender.initialize()
            results.append((sender.session_name, success, msg))
            logger.info(f"Инициализация {sender.session_name}: {success} - {msg}")
        return results
    
    def get_choices(self, price: int) -> Dict:
        """Получает доступные подарки для цены"""
        return self.GIFTS.get(price, {})
    
    def get_gift(self, choice: str, price: int) -> Optional[Dict]:
        """Получает подарок по выбору и цене"""
        choices = self.get_choices(price)
        gift = choices.get(choice)
        if gift:
            return {"name": gift["name"], "id": gift["id"], "price": price}
        return None
    
    async def a_send_gifts(
        self, 
        gift_id: int, 
        username: str, 
        amount: int, 
        stars_cost: int = 1,
        message: str = None, 
        hide_name: bool = None
    ) -> Tuple[int, int, List[str]]:
        """Асинхронно отправляет несколько подарков через одну сессию (round-robin)"""
        successes = 0
        failures = 0
        messages = []
        
        # Фильтруем активные сессии (без lowbalance)
        active_senders = [s for s in self.senders if s.initialized and not s.isFlood and not s.lowbalance]
        
        if not active_senders:
            logger.error("Нет активных сессий для отправки")
            return 0, 0, ["Нет активных сессий"]
        
        # Выбираем сессию round-robin
        with self._sender_lock:
            sender = active_senders[self._sender_index % len(active_senders)]
            self._sender_index += 1
        
        # Если баланс неизвестен — подгружаем
        if sender.balance is None:
            await sender.a_get_balance()
        
        # Проверяем хватит ли баланса на ВСЕ подарки
        if sender.balance is not None and sender.balance < stars_cost * amount:
            logger.warning(f"Недостаточно баланса у {sender.session_name}: {sender.balance} < {stars_cost * amount}")
            return 0, 0, ["BALANCE_TOO_LOW"]
        
        use_message = message if message is not None else gift_settings.get("message", "")
        use_hide_name = hide_name if hide_name is not None else gift_settings.get("hide_name", True)
        
        logger.info(f"Отправка {amount} подарков @{username} через {sender.session_name}")
        
        for i in range(amount):
            success, msg = await sender.a_send_star(gift_id, username, use_message, use_hide_name, stars_cost)
            
            # Обработка специальных ошибок
            critical_codes = ["PAID_MESSAGES_ENABLED", "NO_USER", "USER_DISALLOWED_STARGIFTS"]
            for code in critical_codes:
                if code in msg:
                    return successes, failures, [code]
            
            if success:
                successes += 1
                messages.append(f"✅ #{i+1} отправлен через {sender.session_name}")
                if i < amount - 1:
                    await asyncio.sleep(random.uniform(5, 10)) # Задержка между отправками
            else:
                failures += 1
                messages.append(f"❌ Ошибка {sender.session_name}: {msg}")
                break
            
            await asyncio.sleep(0.5)
        
        # Удаляем чат с получателем из диалогов отправителя
        if successes > 0:
            try:
                await sender._delete_chat(username)
            except Exception as e:
                logger.warning(f"Ошибка удаления чата после отправки: {e}")
        
        logger.info(f"Отправка завершена: {successes} успешно, {failures} ошибок")
        return successes, failures, messages
    
    def send_gifts(self, *args, **kwargs):
        """Синхронная обертка (устаревшая, не используй в async контексте)"""
        raise RuntimeError("send_gifts is synchronous and blocks. Use a_send_gifts instead.")
    
    def get_all_balances(self) -> List[Dict]:
        """Получает балансы всех сессий"""
        balances = []
        for sender in self.senders:
            try:
                balance = sender.get_balance()
                # Убеждаемся, что balance - это число
                balance_int = int(balance) if balance is not None else 0
            except Exception as e:
                logger.error(f"Ошибка получения баланса для {sender.session_name}: {e}")
                balance_int = 0
            
            status = "⚠️ Флуд" if sender.isFlood else "✅ Активна" if sender.initialized else "❌ Не активна"
            balances.append({
                "session": sender.session_name,
                "balance": balance_int,
                "status": status
            })
            time.sleep(0.5)
        return balances
    
    def authorize_session(self, session_name: str, phone: str = None, code: str = None, password: str = None) -> Tuple[bool, str]:
        """Авторизует сессию"""
        for sender in self.senders:
            if sender.session_name == session_name:
                return sender.authorize(phone, code, password)
        return False, "Сессия не найдена"
    
    def export_session_string(self, session_name: str) -> Tuple[Optional[str], str]:
        """Экспортирует строку сессии"""
        for sender in self.senders:
            if sender.session_name == session_name:
                return sender.export_session_string()
        return None, "Сессия не найдена"
    
    def import_session_string(self, name: str, api_id: str, api_hash: str, session_string: str) -> Tuple[bool, str]:
        """Импортирует строку сессии"""
        # Проверяем, нет ли уже такой сессии
        for sess in api_sessions:
            if sess["session_name"] == name:
                return False, "Сессия с таким именем уже существует"
        
        # Создаем и проверяем
        test_sender = StarGiftSender(name, api_id, api_hash, session_string)
        success, msg = test_sender.initialize()
        
        if not success:
            return False, f"Недействительная строка сессии: {msg}"
        
        # Сохраняем
        new_session = {
            "session_name": name,
            "api_id": api_id,
            "api_hash": api_hash,
            "session_string": session_string
        }
        api_sessions.append(new_session)
        save_config()
        
        # Добавляем в менеджер
        self.senders.append(test_sender)
        
        return True, f"Сессия {name} успешно импортирована!"
    
    def delete_session(self, session_name: str) -> bool:
        """Удаляет сессию"""
        for i, sender in enumerate(self.senders):
            if sender.session_name == session_name:
                # Удаляем файл сессии если есть
                session_file = f"storage/sessions/{session_name}.session"
                if os.path.exists(session_file):
                    try:
                        os.remove(session_file)
                    except Exception as e:
                        logger.error(f"Ошибка удаления файла {session_file}: {e}")
                
                self.senders.pop(i)
                return True
        return False

gbot = None
gchatid = None
order_manager = None
bot_instance = None
async_thread = None
manager = None
stop_event = threading.Event()

# Константы для кнопок
CBT_STARS_MAIN = "StarsMainMenu"
CBT_STARS_VIEW_SESSION = "StarsViewSession"
CBT_STARS_AUTH = "StarsAuth"
CBT_STARS_AUTH_COMPLETE = "StarsAuthComplete"
CBT_STARS_ADD_SESSION = "StarsAddSession"
CBT_STARS_DEL_SESSION = "StarsDelSession"
CBT_STARS_ADD_LOT = "StarsAddLot"
CBT_STARS_DEL_LOT = "StarsDelLot"
CBT_STARS_SETTINGS = "StarsSettings"
CBT_STARS_CHANGE_MSG = "StarsChangeMsg"
CBT_STARS_TOGGLE_VISIBILITY = "StarsToggleVisibility"
CBT_STARS_TOGGLE_AUTO = "StarsToggleAuto"
CBT_STARS_TOGGLE_SKIP = "StarsToggleSkip"
CBT_STARS_RUN = "StarsRun"
CBT_STARS_STOP = "StarsStop"
CBT_STARS_BALANCE = "StarsBalance"
CBT_STARS_INIT_SESSIONS = "StarsInitSessions"
CBT_STARS_LOTS_MENU = "StarsLotsMenu"
CBT_STARS_SHOW_LOGS = "StarsShowLogs"
CBT_STARS_STATS = "StarsStats"
CBT_STARS_RESTORE_LOTS = "StarsRestoreLots"
CBT_STARS_CLEAR_STATS = "StarsClearStats"
CBT_STARS_EXPORT_SESSION = "StarsExportSession"
CBT_STARS_IMPORT_SESSION = "StarsImportSession"

AUTO_INIT_SESSIONS = True
DISABLE_LOTS_ON_ERROR = True

def save_config():
    """Сохраняет конфигурацию"""
    SETTINGS["api_sessions"] = api_sessions
    SETTINGS["gift_lots"] = gift_lots
    SETTINGS["gift_settings"] = gift_settings
    SETTINGS["statistics"] = statistics
    
    os.makedirs("storage/plugins", exist_ok=True)
    with open(f"storage/plugins/{UUID}.json", "w", encoding="utf-8") as f:
        json.dump(SETTINGS, f, indent=4, ensure_ascii=False)
    
    logger.info("Конфигурация сохранена")

def load_config():
    """Загружает конфигурацию"""
    global api_sessions, gift_lots, gift_settings, statistics
    
    config_file = f"storage/plugins/{UUID}.json"
    if os.path.exists(config_file):
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                loaded = json.load(f)
                api_sessions = loaded.get("api_sessions", [])
                gift_lots = loaded.get("gift_lots", [])
                gift_settings.update(loaded.get("gift_settings", {}))
                if "skip_gift_selection" not in gift_settings:
                    gift_settings["skip_gift_selection"] = False
                statistics.update(loaded.get("statistics", {
                    "total_sent": 0,
                    "total_revenue": 0,
                    "daily_stats": {},
                    "weekly_stats": {},
                    "monthly_stats": {}
                }))
            
            logger.info(f"Конфигурация загружена: {len(api_sessions)} сессий, {len(gift_lots)} лотов")
        except Exception as e:
            logger.error(f"Ошибка загрузки конфигурации: {e}")
    else:
        logger.info("Конфигурационный файл не найден, используются настройки по умолчанию")

def update_statistics(stars_amount: int, gift_count: int):
    """Обновляет статистику"""
    global statistics
    
    today = datetime.now().strftime("%Y-%m-%d")
    week = datetime.now().strftime("%Y-%W")
    month = datetime.now().strftime("%Y-%m")
    
    revenue = stars_amount * gift_count
    
    statistics["total_sent"] += gift_count
    statistics["total_revenue"] += revenue
    
    if today not in statistics["daily_stats"]:
        statistics["daily_stats"][today] = {"gifts": 0, "revenue": 0}
    statistics["daily_stats"][today]["gifts"] += gift_count
    statistics["daily_stats"][today]["revenue"] += revenue
    
    if week not in statistics["weekly_stats"]:
        statistics["weekly_stats"][week] = {"gifts": 0, "revenue": 0}
    statistics["weekly_stats"][week]["gifts"] += gift_count
    statistics["weekly_stats"][week]["revenue"] += revenue
    
    if month not in statistics["monthly_stats"]:
        statistics["monthly_stats"][month] = {"gifts": 0, "revenue": 0}
    statistics["monthly_stats"][month]["gifts"] += gift_count
    statistics["monthly_stats"][month]["revenue"] += revenue
    
    clean_old_statistics()
    save_config()

def clean_old_statistics():
    """Очищает старую статистику"""
    today = datetime.now()
    
    # Чистим дневную статистику старше 90 дней
    for date_str in list(statistics["daily_stats"].keys()):
        date = datetime.strptime(date_str, "%Y-%m-%d")
        if (today - date).days > 90:
            del statistics["daily_stats"][date_str]
    
    # Чистим недельную статистику старше 365 дней
    for week_str in list(statistics["weekly_stats"].keys()):
        year, week_num = week_str.split("-")
        date = datetime.strptime(f"{year}-{week_num}-1", "%Y-%W-%w")
        if (today - date).days > 365:
            del statistics["weekly_stats"][week_str]
    
    # Чистим месячную статистику старше 730 дней
    for month_str in list(statistics["monthly_stats"].keys()):
        year, month_num = month_str.split("-")
        date = datetime.strptime(f"{year}-{month_num}-01", "%Y-%m-%d")
        if (today - date).days > 730:
            del statistics["monthly_stats"][month_str]

def get_statistics_summary() -> Dict:
    """Возвращает сводку статистики"""
    today = datetime.now().strftime("%Y-%m-%d")
    week = datetime.now().strftime("%Y-%W")
    month = datetime.now().strftime("%Y-%m")
    
    return {
        "today": {
            "gifts": statistics["daily_stats"].get(today, {"gifts": 0})["gifts"],
            "revenue": statistics["daily_stats"].get(today, {"revenue": 0})["revenue"]
        },
        "week": {
            "gifts": statistics["weekly_stats"].get(week, {"gifts": 0})["gifts"],
            "revenue": statistics["weekly_stats"].get(week, {"revenue": 0})["revenue"]
        },
        "month": {
            "gifts": statistics["monthly_stats"].get(month, {"gifts": 0})["gifts"],
            "revenue": statistics["monthly_stats"].get(month, {"revenue": 0})["revenue"]
        },
        "total": {
            "gifts": statistics["total_sent"],
            "revenue": statistics["total_revenue"]
        }
    }

def get_logs(limit: int = 50) -> List[str]:
    """Читает последние логи"""
    try:
        if not os.path.exists(log_file):
            return ["Лог-файл не найден"]
        
        with open(log_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        return lines[-limit:] if len(lines) > limit else lines
    except Exception as e:
        logger.error(f"Ошибка чтения логов: {e}")
        return [f"Ошибка чтения логов: {e}"]

def validate_username(username: str) -> bool:
    """Проверяет валидность username"""
    return bool(re.match(r'^[a-zA-Z0-9_]{5,32}$', username))

def extract_info(text: str) -> Tuple[Optional[str], Optional[int], int]:
    """Извлекает username, звезды и количество из текста"""
    username = None
    patterns = [
        r'@([a-zA-Z0-9_]{5,32})\b',
        r'https://t.me/([a-zA-Z0-9_]{5,32})\b',
        r't.me/([a-zA-Z0-9_]{5,32})\b',
        r'\b([a-zA-Z0-9_]{5,32})\b'
    ]
    
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            potential_username = match.group(1).lstrip('@')
            if validate_username(potential_username):
                username = potential_username
                break
    
    stars_match = re.search(r'(\d+)\s+зв[ёе]зд', text, re.IGNORECASE)
    stars = int(stars_match.group(1)) if stars_match else None
    
    amount_match = re.search(r'(\d+)\s+шт[.,]', text)
    amount = int(amount_match.group(1)) if amount_match else 1
    
    return username, stars, amount

class AsyncOrderManager:
    """Асинхронный менеджер заказов с SQLite persistence"""
    
    def __init__(self, cardinal):
        self.cardinal = cardinal
        self.orders: Dict[str, OrderContext] = {}
        self.user_locks: Dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self.message_queues: Dict[str, asyncio.Queue] = {}
        self.manager_instance = None
        self.processing_users: Set[str] = set()
        self.rate_limits: Dict[str, float] = {}
        self.loop = None
        self._stop_event = None
        self._db_path = "storage/plugins/stars_orders.db"
        os.makedirs(os.path.dirname(self._db_path), exist_ok=True)
        self._db_init()
        
    def _db_init(self):
        """Создаёт таблицу заказов в SQLite и мигрирует схему при необходимости"""
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.execute('''
            CREATE TABLE IF NOT EXISTS orders (
                order_id TEXT PRIMARY KEY,
                buyer_username TEXT NOT NULL,
                target_username TEXT,
                stars INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                state TEXT NOT NULL,
                gift_choice TEXT,
                gift_json TEXT,
                created_at REAL NOT NULL,
                last_message_at REAL NOT NULL,
                hide_name INTEGER,
                message TEXT
            )
        ''')
        # Миграция: добавляем колонки hide_name и message если их нет (для старых БД)
        cursor = conn.execute("PRAGMA table_info(orders)")
        columns = {row[1] for row in cursor.fetchall()}
        if "hide_name" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN hide_name INTEGER")
        if "message" not in columns:
            conn.execute("ALTER TABLE orders ADD COLUMN message TEXT")
        conn.commit()
        conn.close()
    
    def _db_save_order(self, ctx: OrderContext):
        """Сохраняет или обновляет заказ в БД"""
        import sqlite3, json
        conn = sqlite3.connect(self._db_path)
        gift_json = json.dumps(ctx.gift, ensure_ascii=False) if ctx.gift else None
        conn.execute('''
            INSERT OR REPLACE INTO orders 
            (order_id, buyer_username, target_username, stars, amount, state, gift_choice, gift_json, created_at, last_message_at, hide_name, message)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            ctx.order_id, ctx.buyer_username, ctx.target_username, ctx.stars, ctx.amount,
            ctx.state.value, ctx.gift_choice, gift_json, ctx.created_at, ctx.last_message_at,
            int(ctx.hide_name) if ctx.hide_name is not None else None,
            ctx.message
        ))
        conn.commit()
        conn.close()
    
    def _db_delete_order(self, order_id: str):
        """Удаляет завершённый заказ из БД"""
        import sqlite3
        conn = sqlite3.connect(self._db_path)
        conn.execute('DELETE FROM orders WHERE order_id = ?', (order_id,))
        conn.commit()
        conn.close()
    
    async def _db_load_orders(self):
        """Загружает незавершённые заказы из БД при старте"""
        import sqlite3, json
        from collections import defaultdict
        conn = sqlite3.connect(self._db_path)
        cursor = conn.execute('''
            SELECT order_id, buyer_username, target_username, stars, amount, state, gift_choice, gift_json, created_at, last_message_at, hide_name, message
            FROM orders
            WHERE state NOT IN (?, ?, ?)
        ''', (OrderState.COMPLETED.value, OrderState.FAILED.value, OrderState.REFUNDED.value))
        rows = cursor.fetchall()
        conn.close()
        for row in rows:
            order_id, buyer, target, stars, amount, state_str, gift_choice, gift_json, created_at, last_at, hide_name_db, message_db = row
            ctx = OrderContext(
                order_id=order_id,
                buyer_username=buyer,
                target_username=target,
                stars=stars,
                amount=amount
            )
            ctx.state = OrderState(state_str)
            ctx.gift_choice = gift_choice
            if gift_json:
                try:
                    ctx.gift = json.loads(gift_json)
                except:
                    ctx.gift = None
            ctx.created_at = created_at
            ctx.last_message_at = last_at
            if hide_name_db is not None:
                ctx.hide_name = bool(hide_name_db)
            if message_db is not None:
                ctx.message = message_db
            self.orders[order_id] = ctx
            self.message_queues[order_id] = asyncio.Queue()
            asyncio.create_task(self.order_state_machine(ctx))
            logger.info(f"Восстановлен заказ #{order_id} из БД (state={state_str})")
        if rows:
            logger.info(f"Восстановлено {len(rows)} заказов из БД")
        
    async def start(self):
        self.loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        await self._db_load_orders()
        
    async def stop(self):
        if self._stop_event:
            self._stop_event.set()
            
    async def process_order(self, order_data: dict):
        buyer = order_data["buyer_username"]
        order_id = order_data["order_id"]
        desc = order_data.get("description", "")
        
        logger.info(f"Новый заказ #{order_id} от {buyer}: {desc[:100]}")
        
        if buyer in self.processing_users:
            logger.warning(f"Пользователь {buyer} уже в обработке, откладываем заказ #{order_id}")
            await asyncio.sleep(10)
            await self.process_order(order_data)
            return
        
        self.processing_users.add(buyer)
        
        try:
            # Проверяем, что заказ из наших лотов
            skip = True
            
            # Способ 1: проверяем по description (заказ содержит название лота)
            stars_keywords = ["звезд", "stars", "Звёзды", "звёзд"]
            if any(kw in desc.lower() for kw in stars_keywords):
                skip = False
                logger.info(f"Заказ #{order_id} распознан как Stars по keywords")
            
            # Способ 2: проверяем по full_description заказа
            if skip:
                try:
                    order_obj = await asyncio.to_thread(
                        self.cardinal.account.get_order,
                        order_id
                    )
                    full_desc = getattr(order_obj, "full_description", "")
                    if "starstggreat: yes" in full_desc or any(kw in full_desc.lower() for kw in stars_keywords):
                        skip = False
                        logger.info(f"Заказ #{order_id} распознан по full_description")
                except Exception as e:
                    logger.warning(f"Не удалось получить full_description для #{order_id}: {e}")
            
            if skip:
                logger.info(f"Заказ #{order_id} пропущен - не подходит под Stars")
                return
            
            username, stars, amount = extract_info(order_data["description"])
            
            if not stars:
                await self.send_message_safe(buyer, 
                    "❌ Ошибка: не удалось определить количество звезд.\n💰 Возвращаем оплату.")
                await self.refund_order(order_data["order_id"])
                return
            
            if not self.manager_instance:
                await self.send_message_safe(buyer, 
                    "❌ Ошибка: система не инициализирована.\n💰 Возвращаем оплату.")
                await self.refund_order(order_data["order_id"])
                return
            
            choices = self.manager_instance.get_choices(stars)
            if not choices:
                await self.send_message_safe(buyer, 
                    f"❌ Для {stars} звезд нет доступных подарков.\n💰 Возвращаем оплату.")
                await self.refund_order(order_data["order_id"])
                return
            
            ctx = OrderContext(
                order_id=order_data["order_id"],
                buyer_username=buyer,
                target_username=username,
                stars=stars,
                amount=amount
            )
            
            # Патчим update_state чтобы автоматически сохранять в БД
            original_update_state = ctx.update_state
            def _patched_update_state(state):
                original_update_state(state)
                self._db_save_order(ctx)
            ctx.update_state = _patched_update_state
            
            self.orders[order_data["order_id"]] = ctx
            self.message_queues[order_data["order_id"]] = asyncio.Queue()
            self._db_save_order(ctx)
            
            if not username or not validate_username(username):
                ctx.update_state(OrderState.WAITING_USERNAME)
                await self.send_message_safe(buyer,
                    f"✨ Привет! Получен заказ на {stars} Telegram Stars, количество {amount} шт.\n"
                    "К сожалению, не вижу юзернейм получателя в описании заказа.\n\n"
                    "Пожалуйста, напиши @username кому отправить подарок (можно без @):")
            else:
                if gift_settings.get("skip_gift_selection", False):
                    first_choice = list(choices.keys())[0]
                    ctx.gift_choice = first_choice
                    ctx.gift = self.manager_instance.get_gift(first_choice, stars)
                    ctx.update_state(OrderState.WAITING_CONFIRMATION)
                    
                    await self.send_confirmation_prompt(ctx)
                else:
                    ctx.update_state(OrderState.WAITING_GIFT_CHOICE)
                    await self.send_gift_choice(ctx)
            
            asyncio.create_task(self.order_state_machine(ctx))
            
        except Exception as e:
            logger.error(f"Ошибка обработки заказа {order_data['order_id']}: {e}")
            await self.refund_order(order_data["order_id"])
        finally:
            self.processing_users.discard(buyer)
    
    async def process_message(self, username: str, message: str):
        if not await self.check_rate_limit(username):
            return
        
        user_orders = []
        for order_id, ctx in self.orders.items():
            if ctx.buyer_username == username and ctx.state not in [
                OrderState.COMPLETED, OrderState.FAILED, OrderState.REFUNDED
            ]:
                user_orders.append((ctx.last_message_at, order_id))
        
        if not user_orders:
            return
        
        user_orders.sort(reverse=True)
        order_id = user_orders[0][1]
        
        if order_id in self.message_queues:
            await self.message_queues[order_id].put(message)
    
    async def order_state_machine(self, ctx: OrderContext):
        try:
            while ctx.state not in [OrderState.COMPLETED, OrderState.FAILED, OrderState.REFUNDED]:
                try:
                    message = await asyncio.wait_for(
                        self.message_queues[ctx.order_id].get(), 
                        timeout=600.0
                    )
                except asyncio.TimeoutError:
                    await self.auto_send_on_timeout(ctx)
                    break
                
                ctx.last_message_at = time.time()
                
                async with self.user_locks[ctx.buyer_username]:
                    await self.handle_state(ctx, message)
                    
        except Exception as e:
            logger.error(f"Ошибка в стейт-машине заказа {ctx.order_id}: {e}")
            await self.refund_order(ctx.order_id)
        finally:
            self.cleanup_order(ctx.order_id)
    
    async def handle_state(self, ctx: OrderContext, message: str):
        message = message.strip()
        
        if ctx.state == OrderState.WAITING_USERNAME:
            await self.handle_username(ctx, message)
        elif ctx.state == OrderState.WAITING_GIFT_CHOICE:
            await self.handle_gift_choice(ctx, message)
        elif ctx.state == OrderState.WAITING_CONFIRMATION:
            await self.handle_confirmation(ctx, message)
        elif ctx.state == OrderState.WAITING_NEW_USERNAME:
            await self.handle_new_username(ctx, message)
        elif ctx.state == OrderState.WAITING_SETTINGS:
            await self.handle_settings(ctx, message)
    
    async def handle_username(self, ctx: OrderContext, message: str):
        username = message.lstrip('@').strip()
        
        if validate_username(username):
            ctx.target_username = username
            ctx.update_state(OrderState.WAITING_GIFT_CHOICE)
            await self.send_gift_choice(ctx)
        else:
            await self.send_message_safe(ctx.buyer_username,
                "😕 Это не похоже на правильный юзернейм.\n"
                "Юзернейм должен быть от 5 до 32 символов и содержать только буквы, цифры и нижнее подчеркивание.\n\n"
                "Попробуй еще раз, напиши @username получателя:")
    
    async def handle_gift_choice(self, ctx: OrderContext, message: str):
        choices = self.manager_instance.get_choices(ctx.stars)
        gift_choice = None
        
        # Способ 1: ищем число в любом месте
        digit_match = re.search(r'(\d+)', message)
        if digit_match:
            choice = digit_match.group(1)
            if choice in choices:
                gift_choice = choice
        
        # Способ 2: ищем название подарка по словам
        if not gift_choice:
            msg_lower = message.lower()
            for num, gift in choices.items():
                # Ищем название подарка как отдельное слово
                gift_name = gift["name"].lower()
                # Проверяем полное название
                if gift_name in msg_lower:
                    gift_choice = num
                    break
                # Проверяем частичные совпадения (минимум 4 символа)
                if len(gift_name) >= 4:
                    for i in range(len(gift_name) - 3):
                        if gift_name[i:i+4] in msg_lower and len(gift_name[i:i+4]) >= 4:
                            gift_choice = num
                            break
                    if gift_choice:
                        break
        
        if gift_choice and gift_choice in choices:
            ctx.gift_choice = gift_choice
            ctx.gift = self.manager_instance.get_gift(gift_choice, ctx.stars)
            self._db_save_order(ctx)
            ctx.update_state(OrderState.WAITING_CONFIRMATION)
            
            await self.send_confirmation_prompt(ctx)
        else:
            valid = ", ".join(choices.keys())
            await self.send_message_safe(ctx.buyer_username,
                f"Не совсем понял. Просто напиши цифру от {valid}, и я пойму какой подарок выбрать.")
    
    async def send_confirmation_prompt(self, ctx: OrderContext):
        """Отправляет меню подтверждения с текущими настройками"""
        hide = ctx.get_hide_name()
        msg_text = ctx.get_message()
        gift_name_upper = ctx.gift['name'].upper()
        msg = (
            f"📋 Все верно? Давай проверим:\n\n"
            f"🎁 Выбран подарок {gift_name_upper}\n"
            f"В количестве {ctx.amount} ШТ.\n"
            f"👤 Получатель: @{ctx.target_username}\n"
        )
        if msg_text:
            msg += f"💬 Комментарий к подарку: {msg_text}\n"
        msg += f"{'👻' if hide else '👀'} Анонимная отправка: {'да' if hide else 'нет'}\n"
        if hide and msg_text:
            msg += "\n⚠️ Важно: при анонимной отправке комментарий получателю НЕ будет виден.\n"
        msg += (
            '\n🚀 Если все правильно, напиши "+" и я сразу отправлю.\n'
            '✏️ Если нужно сменить получателя, напиши "-"\n'
            '⚙️ Хочешь настроить комментарий или анонимность, напиши "="\n'
            '❌ Если передумал, напиши "возврат"'
        )
        msg += (
            "\n\n🚨 Внимание! Сразу после отправки подарка чат в Telegram с получателем будет автоматически удалён.\n"
            "🎁 Ищи полученный подарок в профиле получателя Telegram.\n"
        )
        await self.send_message_safe(ctx.buyer_username, msg)

    async def handle_confirmation(self, ctx: OrderContext, message: str):
        msg_lower = message.strip().lower()
        # Токенизация по целым словам + символы +/-
        tokens = set(re.findall(r'\b\w+\b|[+-]', msg_lower))
        
        # Если пользователь повторяет цифру выбранного подарка — считаем подтверждением
        if re.fullmatch(r'\d+', message.strip()) and message.strip() == str(ctx.gift_choice):
            await self.send_gifts(ctx)
            return
        
        # Подтверждение: расширенный набор слов
        confirm_words = {
            '+', 'ок', 'окей', 'k', 'да', 'yes', 'yep',
            'подтверждаю', 'подтвердить', 'подтверждено',
            'отправь', 'отправляй', 'высылай', 'перешли', 'пересылай',
            'скинь', 'кинь', 'закинь', 'шли', 'выкладывай',
            'го', 'go', 'погнали', 'давай', 'давайте', 'lets',
            'жду', 'уже', 'хочу', 'получить', 'быстрее', 'скорее', 'момент',
            'готово', 'done', 'ready', 'send', 'sent',
            '1', '2', '3', '4', '5', '6'
        }
        if tokens & confirm_words:
            await self.send_gifts(ctx)
            return
        
        # Настройки подарка
        if message.strip() == '=':
            ctx.update_state(OrderState.WAITING_SETTINGS)
            hide = ctx.get_hide_name()
            msg_text = ctx.get_message()
            msg = (
                "⚙️ Настройки подарка:\n\n"
                f"{'👻' if hide else '👀'} Анонимная отправка: {'включена' if hide else 'выключена'}\n"
            )
            if msg_text:
                msg += f"💬 Комментарий: {msg_text}\n"
            else:
                msg += "💬 Комментарий: пока не задан\n"
            if hide and msg_text:
                msg += "\n⚠️ Важно: при анонимной отправке комментарий получателю НЕ будет виден.\n"
            msg += (
                '\nНапиши 1 чтобы переключить анонимность\n'
                'Напиши любой текст и он станет комментарием к подарку\n'
                'Напиши "+" чтобы вернуться к подтверждению заказа'
            )
            await self.send_message_safe(ctx.buyer_username, msg)
            return
        
        # Изменить получателя: -, сменить, изменить, другой, поменять, другое, не тот
        change_words = {'-', 'сменить', 'изменить', 'другой', 'поменять', 'другое'}
        if tokens & change_words or 'не тот' in msg_lower:
            ctx.update_state(OrderState.WAITING_NEW_USERNAME)
            await self.send_message_safe(ctx.buyer_username,
                "Введите новый @username получателя (можно без @):")
            return
        
        # Отмена: возврат, отмена, нет, cancel, стоп, назад, не надо
        cancel_words = {'возврат', 'отмена', 'нет', 'cancel', 'стоп', 'назад'}
        if tokens & cancel_words or 'не надо' in msg_lower:
            await self.refund_order(ctx.order_id, "Заказ отменен пользователем")
            ctx.update_state(OrderState.REFUNDED)
            return
        
        await self.send_message_safe(ctx.buyer_username,
            "🤔 Не совсем понял. Вот что можно сделать:\n"
            '🚀 "+" отправить подарок\n'
            '✏️ "-" сменить получателя\n'
            '⚙️ "=" настроить комментарий и анонимность\n'
            '❌ "возврат" отменить заказ и вернуть деньги')
    
    async def handle_settings(self, ctx: OrderContext, message: str):
        text = message.strip()
        
        if text == '1':
            current = ctx.get_hide_name()
            ctx.hide_name = not current
            self._db_save_order(ctx)
            status = "включена" if ctx.hide_name else "выключена"
            emoji = "👻" if ctx.hide_name else "👀"
            msg = f"{emoji} Готово! Анонимная отправка теперь {status}.\n"
            if ctx.hide_name and ctx.get_message():
                msg += "\n⚠️ Важно: при анонимной отправке комментарий получателю НЕ будет виден.\n"
            msg += '\nНапиши 1 чтобы переключить анонимность\n'
            msg += 'Напиши любой текст чтобы установить комментарий\n'
            msg += 'Напиши "+" чтобы вернуться к подтверждению'
            await self.send_message_safe(ctx.buyer_username, msg)
            return
        
        if text == '+':
            ctx.update_state(OrderState.WAITING_CONFIRMATION)
            await self.send_confirmation_prompt(ctx)
            return
        
        # Любой другой текст = установить комментарий
        ctx.message = text
        self._db_save_order(ctx)
        msg = f"💬 Комментарий к подарку установлен: {text}\n"
        if ctx.get_hide_name():
            msg += "\n⚠️ Важно: при анонимной отправке комментарий получателю НЕ будет виден.\n"
        msg += '\nНапиши 1 чтобы переключить анонимность\n'
        msg += 'Напиши текст чтобы изменить комментарий\n'
        msg += 'Напиши "+" чтобы вернуться к подтверждению'
        await self.send_message_safe(ctx.buyer_username, msg)

    async def handle_new_username(self, ctx: OrderContext, message: str):
        username = message.lstrip('@').strip()
        
        if validate_username(username):
            ctx.target_username = username
            self._db_save_order(ctx)
            ctx.update_state(OrderState.WAITING_CONFIRMATION)
            await self.send_confirmation_prompt(ctx)
        else:
            await self.send_message_safe(ctx.buyer_username,
                "😕 Это не похоже на правильный юзернейм.\n"
                "Юзернейм должен быть от 5 до 32 символов и содержать только буквы, цифры и нижнее подчеркивание.\n\n"
                "Попробуй еще раз, напиши @username получателя:")
    
    async def send_gifts(self, ctx: OrderContext):
        ctx.update_state(OrderState.PROCESSING_SENDING)
        await self.send_message_safe(ctx.buyer_username, f"⏳ Секундочку, отправляю {ctx.amount} подарков...")
        
        try:
            gprices = {13:15, 21:25, 43:50, 85:100}
            stars_cost = gprices.get(ctx.stars, ctx.stars)
            
            result = await self.manager_instance.a_send_gifts(
                ctx.gift["id"],
                ctx.target_username,
                ctx.amount,
                stars_cost,
                message=ctx.get_message(),
                hide_name=ctx.get_hide_name()
            )
            
            if isinstance(result, tuple) and len(result) == 3:
                successes, failures, messages = result
                
                # Специальные ошибки: если ничего не отправлено — рефанд/возврат в меню
                critical_msg = None
                critical_codes = ["PAID_MESSAGES_ENABLED", "NO_USER", "USER_DISALLOWED_STARGIFTS", "BALANCE_TOO_LOW", "FLOOD_WAIT", "Нет активных сессий"]
                if messages:
                    last_msg = messages[-1]
                    for code in critical_codes:
                        if code in last_msg:
                            critical_msg = code
                            break
                
                if critical_msg and successes == 0:
                    if critical_msg == "PAID_MESSAGES_ENABLED":
                        await self.send_message_safe(ctx.buyer_username,
                            "😕 Упс, не получилось отправить. Похоже, у тебя включены платные сообщения в Telegram.\n\n"
                            'Отключи их в настройках приватности Telegram, потом напиши "+" и я повторю отправку.\n'
                            'Если не хочешь ждать, напиши "возврат", верну деньги.')
                        ctx.update_state(OrderState.WAITING_CONFIRMATION)
                        return
                    
                    if critical_msg == "USER_DISALLOWED_STARGIFTS":
                        await self.send_message_safe(ctx.buyer_username,
                            "😕 Упс, не получилось отправить. Похоже, получатель запретил приём подарков в Telegram.\n\n"
                            'Проверь настройки приватности получателя или смени получателя "-", потом напиши "+" и я повторю отправку.\n'
                            'Если не хочешь ждать, напиши "возврат", верну деньги.')
                        ctx.update_state(OrderState.WAITING_CONFIRMATION)
                        return
                    
                    if critical_msg == "BALANCE_TOO_LOW":
                        await self.send_message_safe(ctx.buyer_username,
                            "😕 Сейчас не получилось отправить — временно закончились звёзды на нашем аккаунте.\n\n"
                            "Возвращаю оплату, попробуй заказать позже.")
                        if DISABLE_LOTS_ON_ERROR:
                            await self.disable_all_lots(ctx.order_id)
                        await self.send_notification_to_owner(f"⚠️ Закончились звёзды! Заказ #{ctx.order_id} возвращён.")
                        await self.refund_order(ctx.order_id, "Недостаточно звёзд на балансе")
                        ctx.update_state(OrderState.REFUNDED)
                        return
                    
                    if "FLOOD_WAIT" in critical_msg:
                        wait_time = int(critical_msg.replace("FLOOD_WAIT_", ""))
                        if wait_time >= 3600:
                            time_str = f"{wait_time // 3600} час {(wait_time % 3600) // 60} мин"
                        elif wait_time >= 60:
                            time_str = f"{wait_time // 60} мин {wait_time % 60} сек"
                        else:
                            time_str = f"{wait_time} сек"
                        await self.send_message_safe(ctx.buyer_username,
                            f"😕 Telegram временно ограничил отправку подарков (подождать нужно {time_str}).\n\n"
                            "Возвращаю оплату, попробуй заказать позже.")
                        if DISABLE_LOTS_ON_ERROR:
                            await self.disable_all_lots(ctx.order_id)
                        await self.send_notification_to_owner(
                            f"🚨 FloodWait! {time_str}\n"
                            f"Заказ #{ctx.order_id} (@{ctx.target_username})\n"
                            f"Сессия: UB"
                        )
                        await self.refund_order(ctx.order_id, f"FloodWait {time_str}")
                        ctx.update_state(OrderState.REFUNDED)
                        return
                    
                    if critical_msg == "Нет активных сессий":
                        await self.send_message_safe(ctx.buyer_username,
                            "😕 Система отправки временно недоступна.\n\n"
                            "Возвращаю оплату, попробуй заказать позже.")
                        if DISABLE_LOTS_ON_ERROR:
                            await self.disable_all_lots(ctx.order_id)
                        await self.send_notification_to_owner(f"⚠️ Нет активных сессий! Заказ #{ctx.order_id} возвращён.")
                        await self.refund_order(ctx.order_id, "Нет активных сессий")
                        ctx.update_state(OrderState.REFUNDED)
                        return
                    
                    if critical_msg == "NO_USER":
                        await self.send_message_safe(ctx.buyer_username,
                            "😕 Не удалось найти пользователя @{ctx.target_username} в Telegram.\n\n"
                            "Проверь юзернейм и напиши новый — можно без @:")
                        ctx.update_state(OrderState.WAITING_NEW_USERNAME)
                        return
                
                if successes == ctx.amount:
                    update_statistics(stars_cost, successes)
                    gift_name_upper = ctx.gift['name'].upper()
                    await self.send_message_safe(ctx.buyer_username,
                        f"✨ Готово! Подарок {gift_name_upper} успешно отправлен получателю @{ctx.target_username}, "
                        f"в количестве {ctx.amount} ШТ.\n\n"
                        f"Спасибо за заказ!")
                    await self.send_message_safe(ctx.buyer_username,
                        f"⭐ Не забудь подтвердить выполнение заказа и оставить отзыв")
                    ctx.update_state(OrderState.COMPLETED)
                
                elif successes == 0:
                    # Ничего не отправлено — рефанд
                    await self.send_message_safe(ctx.buyer_username,
                        f"😕 К сожалению, отправка не сработала.\n"
                        f"Не удалось отправить {ctx.amount} подарков.\n\n"
                        f"Возвращаю оплату...")
                    if DISABLE_LOTS_ON_ERROR:
                        await self.disable_all_lots(ctx.order_id)
                    await self.refund_order(ctx.order_id, "Ошибка отправки")
                    ctx.update_state(OrderState.REFUNDED)
                
                elif successes < ctx.amount and ctx.amount <= 2:
                    # Мелкий заказ (1-2 шт) — частичная отправка = рефанд
                    await self.send_message_safe(ctx.buyer_username,
                        f"⚠️ Отправлено частично.\n"
                        f"Успешно ушло {successes} шт., а {ctx.amount - successes} не получилось.\n\n"
                        f"Возвращаю оплату за недоставленное...")
                    if DISABLE_LOTS_ON_ERROR and successes == 0:
                        await self.disable_all_lots(ctx.order_id)
                    await self.refund_order(ctx.order_id, "Частичная отправка")
                    ctx.update_state(OrderState.REFUNDED)
                
                else:
                    # Крупный заказ (>2), частичная отправка — не рефандим, уведомляем
                    await self.send_message_safe(ctx.buyer_username,
                        f"⚠️ Отправлено частично.\n"
                        f"Успешно ушло {successes} шт., а {ctx.amount - successes} не получилось.\n\n"
                        f"Напиши мне в чат, разберемся и доставим остальное.")
                    await self.send_notification_to_owner(
                        f"Частичная отправка заказа #{ctx.order_id}\n"
                        f"{successes}/{ctx.amount} через {ctx.gift['name']}\n"
                        f"Покупатель: @{ctx.target_username}"
                    )
                    update_statistics(stars_cost, successes)
                    ctx.update_state(OrderState.COMPLETED)
                    
        except Exception as e:
            logger.error(f"Ошибка отправки подарков для заказа {ctx.order_id}: {e}")
            await self.send_message_safe(ctx.buyer_username,
                "😕 Произошла ошибка при отправке.\nВозвращаю оплату...")
            if DISABLE_LOTS_ON_ERROR:
                await self.disable_all_lots(ctx.order_id)
            await self.refund_order(ctx.order_id, f"Ошибка отправки: {e}")
            ctx.update_state(OrderState.FAILED)

    async def disable_all_lots(self, order_id: str):
        try:
            logger.warning(f"Отключаем все лоты из-за ошибки в заказе {order_id}")
            
            for lot_id in gift_lots:
                try:
                    lot_fields = await asyncio.to_thread(
                        self.cardinal.account.get_lot_fields,
                        int(lot_id)
                    )
                    lot_fields.active = False
                    lot_fields.renew_fields()
                    await asyncio.to_thread(
                        self.cardinal.account.save_lot,
                        lot_fields
                    )
                    logger.info(f"Лот {lot_id} отключен")
                except Exception as e:
                    logger.error(f"Ошибка отключения лота {lot_id}: {e}")

            await self.send_notification_to_owner(
                f"Все лоты отключены!\n"
                f"Причина: Критическая ошибка в заказе {order_id}\n"
                f"Нажмите 'Восстановить лоты' после решения проблем."
            )
            
        except Exception as e:
            logger.error(f"Ошибка при отключении лотов: {e}")
    
    async def send_notification_to_owner(self, message: str):
        try:
            global gbot, gchatid
            gbot.send_message(gchatid, message)
        except:
            pass
    
    async def send_gift_choice(self, ctx: OrderContext):
        choices = self.manager_instance.get_choices(ctx.stars)
        msg = (
            f"🎯 Отлично, получатель @{ctx.target_username}!\n"
            f"Заказ на {ctx.stars} Telegram Stars, {ctx.amount} шт.\n\n"
            "Выбери подарок, просто напиши цифру:\n"
        )
        for num, gift in sorted(choices.items()):
            gift_name_upper = gift['name'].upper()
            msg += f"{num}. {gift_name_upper}\n"
        msg += "\nНапиши номер понравившегося подарка:"
        await self.send_message_safe(ctx.buyer_username, msg)
    
    async def send_message_safe(self, buyer: str, text: str):
        try:
            await asyncio.sleep(random.uniform(1.8, 2.5))
            await asyncio.to_thread(
                self.send_message_sync,
                buyer,
                text
            )
        except Exception as e:
            logger.error(f"Ошибка отправки сообщения {buyer}: {e}")
            
    
    def send_message_sync(self, buyer: str, text: str):
        for attempt in range(5):
            try:
                chat = self.cardinal.account.get_chat_by_name(buyer, True)
                if chat:
                    self.cardinal.account.send_message(chat.id, text)
                    logger.info(f"Сообщение отправлено {buyer}")
                break
            except Exception as e:
                if attempt >= 4:
                    logger.error(f"Отправка сообщения {buyer} провалена после 5 попыток: {e}")
                    break
                time.sleep(2)
                parts = str(e).split('error')
                error_msg = parts[1] if len(parts) > 1 else str(e)
                logger.error(f"Ошибка отправки сообщения {buyer}: {error_msg} (попытка {attempt + 1}/5)")
    
    async def refund_order(self, order_id: str, reason: str = ""):
        try:
            await asyncio.to_thread(self.cardinal.account.refund, order_id)
            logger.info(f"Заказ {order_id} возвращен: {reason}")
        except Exception as e:
            logger.error(f"Ошибка возврата заказа {order_id}: {e}")
    
    async def auto_send_on_timeout(self, ctx: OrderContext):
        # Нет username — не можем отправить, рефанд
        if not ctx.target_username or not validate_username(ctx.target_username):
            await self.send_message_safe(ctx.buyer_username,
                "⏰ Долго не было ответа и не указан получатель.\nВозвращаю оплату.")
            await self.refund_order(ctx.order_id, "Таймаут: нет получателя")
            ctx.update_state(OrderState.REFUNDED)
            return
        
        # Авто-выбор первого подарка, если ещё не выбран
        if not ctx.gift or not ctx.gift_choice:
            choices = self.manager_instance.get_choices(ctx.stars)
            if not choices:
                await self.send_message_safe(ctx.buyer_username,
                    "⏰ Долго не было ответа, а для этой суммы нет подарков.\nВозвращаю оплату.")
                await self.refund_order(ctx.order_id, "Таймаут: нет подарков")
                ctx.update_state(OrderState.REFUNDED)
                return
            first_choice = list(choices.keys())[0]
            ctx.gift_choice = first_choice
            ctx.gift = self.manager_instance.get_gift(first_choice, ctx.stars)
            self._db_save_order(ctx)
        
        # Уведомляем и отправляем
        gift_name_upper = ctx.gift['name'].upper()
        await self.send_message_safe(ctx.buyer_username,
            f"⏰ Долго не было ответа. Отправляю автоматически: {gift_name_upper} на @{ctx.target_username}!")
        await self.send_gifts(ctx)
    
    async def check_rate_limit(self, username: str) -> bool:
        current_time = time.time()
        last_time = self.rate_limits.get(username, 0)
        
        if current_time - last_time < 1.0:
            return False
        
        self.rate_limits[username] = current_time
        return True
    
    def cleanup_order(self, order_id: str):
        if order_id in self.orders:
            del self.orders[order_id]
        if order_id in self.message_queues:
            del self.message_queues[order_id]
        self._db_delete_order(order_id)

class AsyncThread(threading.Thread):
    def __init__(self, cardinal):
        super().__init__(daemon=True)
        self.cardinal = cardinal
        self.loop = None
        self.order_manager = None
        
    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        
        self.order_manager = AsyncOrderManager(self.cardinal)
        
        self.loop.run_until_complete(self.order_manager.start())
        
        try:
            self.loop.run_forever()
        except Exception as e:
            logger.error(f"Ошибка в event loop: {e}")
        finally:
            self.loop.close()
    
    def stop(self):
        if self.loop and self.order_manager:
            asyncio.run_coroutine_threadsafe(
                self.order_manager.stop(),
                self.loop
            )
            self.loop.call_soon_threadsafe(self.loop.stop)

def _future_done_callback(future):
    try:
        future.result(timeout=0)
    except Exception as e:
        logger.error(f"Ошибка в async задаче: {e}")

def run_async_in_thread(coro_func, *args, **kwargs):
    if not async_thread or not async_thread.loop:
        logger.error("Асинхронный поток не запущен")
        return None
    
    future = asyncio.run_coroutine_threadsafe(
        coro_func(*args, **kwargs),
        async_thread.loop
    )
    future.add_done_callback(_future_done_callback)
    return future

def init(cardinal: Cardinal):
    """Инициализация плагина"""
    from FunPayAPI.updater.events import NewMessageEvent, NewOrderEvent
    
    global api_sessions, gift_lots, gift_settings, statistics, bot_instance, async_thread, manager
    
    logger.info(f"Инициализация плагина {NAME} v{VERSION}")
    
    load_config()
    os.makedirs("storage/sessions", exist_ok=True)
    
    manager = GiftsManager(api_sessions)
    
    async_thread = AsyncThread(cardinal)
    async_thread.start()
    
    time.sleep(1)
    
    if manager and async_thread.order_manager:
        async_thread.order_manager.manager_instance = manager

        if AUTO_INIT_SESSIONS and manager:
            def auto_init_sessions():
                time.sleep(3)
                logger.info("Автоматическая инициализация сессий...")
                results = manager.initialize_all_sessions()
                successful = sum(1 for _, success, _ in results if success)
                logger.info(f"Автоинициализация завершена: {successful}/{len(results)} сессий")
                
                # Загружаем балансы для кэширования
                logger.info("Загрузка балансов...")
                try:
                    balances = manager.get_all_balances()
                    total_balance = sum(b["balance"] for b in balances)
                    logger.info(f"Общий баланс: {total_balance} звезд")
                except Exception as e:
                    logger.error(f"Ошибка загрузки балансов: {e}")
            
            threading.Thread(target=auto_init_sessions, daemon=True).start()
    
    tg = cardinal.telegram
    bot = tg.bot
    bot_instance = bot
    
    # ==================== Обработчики меню ====================
    
    def open_plugin_settings(call):
        try:
            keyboard = K()
            keyboard.row(
                B("📱 Сессии", callback_data=CBT_STARS_VIEW_SESSION),
                B("💰 Баланс", callback_data=CBT_STARS_BALANCE)
            )
            keyboard.row(
                B("⚙️ Настройки", callback_data=CBT_STARS_SETTINGS),
                B("📊 Статистика", callback_data=CBT_STARS_STATS)
            )
            keyboard.row(
                B("📋 Логи", callback_data=CBT_STARS_SHOW_LOGS),
                B("🔄 Инициализировать", callback_data=CBT_STARS_INIT_SESSIONS)
            )
            keyboard.row(
                B("▶️ Запустить авто", callback_data=CBT_STARS_RUN),
                B("⏹️ Остановить авто", callback_data=CBT_STARS_STOP)
            )
            keyboard.row(
                B("📤 Экспорт сессии", callback_data=CBT_STARS_EXPORT_SESSION),
                B("📥 Импорт сессии", callback_data=CBT_STARS_IMPORT_SESSION)
            )
            keyboard.add(B("🔄 Восстановить лоты", callback_data=CBT_STARS_RESTORE_LOTS))
            keyboard.add(B("◀️ Назад", callback_data=f"{CBT.MAIN}"))
            
            stats = get_statistics_summary()
            active_count = sum(1 for s in manager.senders if s.initialized and not s.isFlood) if manager else 0
            total_count = len(manager.senders) if manager else 0
            
            text = f"""🎁 *Telegram Stars Gifts PRO v{VERSION}*

📊 *Статистика:*
• Сессий: {total_count} (✅ Активных: {active_count})
• Лотов: {len(gift_lots)}
• Автоотправка: {'✅ Включена' if gift_settings.get('auto_send', True) else '❌ Выключена'}

📈 *Продажи:*
• За сегодня: {stats['today']['gifts']} подарков ({stats['today']['revenue']}⭐)
• За неделю: {stats['week']['gifts']} подарков ({stats['week']['revenue']}⭐)
• За месяц: {stats['month']['gifts']} подарков ({stats['month']['revenue']}⭐)
• Всего: {stats['total']['gifts']} подарков ({stats['total']['revenue']}⭐)"""
            
            global gbot, gchatid
            gbot = bot
            gchatid = call.message.chat.id

            try:
                bot.edit_message_text(
                    text,
                    call.message.chat.id,
                    call.message.message_id,
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )
            except:
                bot.send_message(
                    call.message.chat.id,
                    text,
                    reply_markup=keyboard,
                    parse_mode="Markdown"
                )
        except Exception as e:
            logger.error(f"Ошибка в open_plugin_settings: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)

    def handle_stars_menu(m):
        open_plugin_settings(type('Call', (), {
            'message': type('Msg', (), {'chat': type('Chat', (), {'id': m.chat.id}), 'message_id': m.message_id}),
            'from_user': m.from_user,
            'id': 'cmd'
        })())

    tg.msg_handler(handle_stars_menu, commands=["stars_menu"])
    cardinal.add_telegram_commands(UUID, [("stars_menu", "Открыть меню Stars Gifts", True)])
    
    # ==================== Обработчики сессий ====================
    
    def open_sessions_menu(call):
        try:
            keyboard = K()
            
            if manager and manager.senders:
                for sender in manager.senders:
                    status = "⚠️" if sender.isFlood else "✅" if sender.initialized else "❌"
                    keyboard.add(B(f"{status} {sender.session_name}", 
                                   callback_data=f"{CBT_STARS_AUTH}:{sender.session_name}"))
            else:
                keyboard.add(B("📭 Нет сессий", callback_data="no_action"))
            
            keyboard.row(
                B("➕ Добавить", callback_data=CBT_STARS_ADD_SESSION),
                B("🗑️ Удалить", callback_data=CBT_STARS_DEL_SESSION)
            )
            keyboard.add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
            
            active_count = sum(1 for s in manager.senders if s.initialized and not s.isFlood) if manager else 0
            text = f"""📱 *Управление сессиями*

Всего сессий: {len(manager.senders) if manager else 0}
Активных: {active_count}

Выберите сессию для авторизации или управления:"""
            
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=keyboard, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Ошибка в open_sessions_menu: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
    
    def start_auth_session(call):
        try:
            session_name = call.data.replace(f"{CBT_STARS_AUTH}:", "")
            
            tg.set_state(call.message.chat.id, call.message.message_id, call.from_user.id,
                         CBT_STARS_AUTH_COMPLETE, {"session_name": session_name, "step": "phone"})
            
            keyboard = K()
            keyboard.add(B("❌ Отмена", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
            
            bot.edit_message_text(f"📱 *Авторизация сессии: {session_name}*\n\nВведите номер телефона (формат: +0):",
                                  call.message.chat.id, call.message.message_id, reply_markup=keyboard, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Ошибка в start_auth_session: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
    
    def process_auth_message(message):
        try:
            user_state = tg.get_state(message.chat.id, message.from_user.id)
            if not user_state or user_state["state"] != CBT_STARS_AUTH_COMPLETE:
                return False
            
            data = user_state["data"]
            session_name = data["session_name"]
            step = data["step"]
            text = message.text.strip()
            
            if not manager:
                tg.clear_state(message.chat.id, message.from_user.id)
                bot.send_message(message.chat.id, "❌ Менеджер не инициализирован")
                return True
            
            if step == "phone":
                data["phone"] = text
                data["step"] = "code"
                
                success, msg = manager.authorize_session(session_name, phone=text)
                
                if success:
                    tg.set_state(message.chat.id, message.message_id, message.from_user.id,
                                 CBT_STARS_AUTH_COMPLETE, data)
                    
                    bot.send_message(message.chat.id, f"✅ Код отправлен на {text}\n\nВведите код из Telegram:",
                                     reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
                else:
                    tg.clear_state(message.chat.id, message.from_user.id)
                    bot.send_message(message.chat.id, f"❌ Ошибка: {msg}")

            elif step == "code":
                data["step"] = "password"
                data["code"] = text

                tg.set_state(message.chat.id, message.message_id, message.from_user.id,
                             CBT_STARS_AUTH_COMPLETE, data)
                
                bot.send_message(message.chat.id, "Введите пароль если есть, если нет, то отправьте '-':",
                                 reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
                
            elif step == "password":
                password = text if text != "-" else None
                data["password"] = password

                success, msg = manager.authorize_session(session_name, phone=data["phone"],
                                                         code=data["code"], password=password)
                
                tg.clear_state(message.chat.id, message.from_user.id)
                
                if success:
                    bot.send_message(message.chat.id, f"✅ Сессия {session_name} успешно авторизована",
                                     reply_markup=K().add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}")))
                else:
                    bot.send_message(message.chat.id, f"❌ Ошибка: {msg}")
            
            return True
        except Exception as e:
            logger.error(f"Ошибка в process_auth_message: {e}")
            return False
    
    def handle_add_session_cmd(message):
        try:
            parts = message.text.split(':')
            if len(parts) != 3:
                bot.send_message(message.chat.id, "❌ Формат: session_name:api_id:api_hash")
                return
            
            session_name, api_id, api_hash = parts
            
            for session in api_sessions:
                if session["session_name"] == session_name:
                    bot.send_message(message.chat.id, f"❌ Сессия '{session_name}' уже существует")
                    return
            
            new_session = {
                "session_name": session_name.strip(),
                "api_id": api_id.strip(),
                "api_hash": api_hash.strip(),
                "session_string": None
            }
            
            api_sessions.append(new_session)
            save_config()
            
            manager.senders.append(StarGiftSender(session_name.strip(), api_id.strip(), api_hash.strip()))
            logger.info(f"Добавлена новая сессия: {session_name}")
            
            bot.send_message(message.chat.id, f"✅ Сессия {session_name} добавлена!\n\nТеперь авторизуйте её через меню '📱 Сессии' или импортируйте готовую строку.",
                             reply_markup=K().add(B("📱 Сессии", callback_data=CBT_STARS_VIEW_SESSION)))
            
        except Exception as e:
            logger.error(f"Ошибка в handle_add_session_cmd: {e}")
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
        finally:
            tg.clear_state(message.chat.id, message.from_user.id)
    
    def handle_del_session_cmd(message):
        try:
            session_name = message.text.strip()
            
            if not manager:
                bot.send_message(message.chat.id, "❌ Менеджер не инициализирован")
                tg.clear_state(message.chat.id, message.from_user.id)
                return
            
            if manager.delete_session(session_name):
                for i, session in enumerate(api_sessions):
                    if session["session_name"] == session_name:
                        api_sessions.pop(i)
                        save_config()
                        logger.info(f"Сессия {session_name} удалена из настроек")
                        break
                
                bot.send_message(message.chat.id, f"✅ Сессия {session_name} удалена")
            else:
                bot.send_message(message.chat.id, "❌ Сессия не найдена")
            
        except Exception as e:
            logger.error(f"Ошибка в handle_del_session_cmd: {e}")
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
        finally:
            tg.clear_state(message.chat.id, message.from_user.id)
    
    # ==================== Обработчики лотов ====================
    
    def open_lots_menu(call):
        try:
            keyboard = K()
            
            if gift_lots:
                for lot in gift_lots:
                    keyboard.add(B(f"📦 {lot}", callback_data=f"stars_lot_info:{lot}"))
            else:
                keyboard.add(B("📭 Нет лотов", callback_data="no_action"))
            
            keyboard.row(
                B("➕ Добавить лот", callback_data=CBT_STARS_ADD_LOT),
                B("🗑️ Удалить лот", callback_data=CBT_STARS_DEL_LOT)
            )
            keyboard.add(B("◀️ Назад", callback_data=CBT_STARS_SETTINGS))
            
            text = f"""📦 *Управление лотами*

Всего лотов: {len(gift_lots)}

Список лотов:"""
            
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=keyboard, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Ошибка в open_lots_menu: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
    
    def handle_add_lot_cmd(message):
        try:
            lot_id = message.text.strip()
            
            if not lot_id:
                bot.send_message(message.chat.id, "❌ Введите ID лота")
                return
            
            if lot_id not in gift_lots:
                gift_lots.append(lot_id)
                save_config()
                logger.info(f"Добавлен лот: {lot_id}")
                bot.send_message(message.chat.id, f"✅ Лот {lot_id} добавлен")
            else:
                bot.send_message(message.chat.id, "⚠️ Лот уже существует")
            
        except Exception as e:
            logger.error(f"Ошибка в handle_add_lot_cmd: {e}")
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
        finally:
            tg.clear_state(message.chat.id, message.from_user.id)
    
    def handle_del_lot_cmd(message):
        try:
            lot_id = message.text.strip()
            
            if lot_id in gift_lots:
                gift_lots.remove(lot_id)
                save_config()
                logger.info(f"Удален лот: {lot_id}")
                bot.send_message(message.chat.id, f"✅ Лот {lot_id} удален")
            else:
                bot.send_message(message.chat.id, "❌ Лот не найден")
            
        except Exception as e:
            logger.error(f"Ошибка в handle_del_lot_cmd: {e}")
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
        finally:
            tg.clear_state(message.chat.id, message.from_user.id)
    
    # ==================== Обработчики настроек ====================
    
    def open_settings_menu(call):
        try:
            keyboard = K()
            keyboard.add(B("📦 Управление лотами", callback_data=CBT_STARS_LOTS_MENU))
            
            visibility = "🙈 Скрыт" if gift_settings.get("hide_name", True) else "👁 Виден"
            auto_send = "🟢 Вкл" if gift_settings.get("auto_send", True) else "🔴 Выкл"
            skip_selection = "🟢 Пропускать" if gift_settings.get("skip_gift_selection", False) else "🔴 Выбирать"
            message = gift_settings.get("message", "")
            
            keyboard.row(
                B("✉️ Сообщение", callback_data=CBT_STARS_CHANGE_MSG),
                B(f"👁 {visibility}", callback_data=CBT_STARS_TOGGLE_VISIBILITY)
            )
            keyboard.row(
                B(f"🔄 {auto_send}", callback_data=CBT_STARS_TOGGLE_AUTO),
                B(f"⏩ {skip_selection}", callback_data=CBT_STARS_TOGGLE_SKIP)
            )
            keyboard.add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
            
            text = f"""⚙️ *Настройки Stars Gifts*

*Текущие настройки:*
• Сообщение: {message[:30] + '...' if len(message) > 30 else message or 'Не задано'}
• Видимость: {visibility}
• Автоотправка: {'Включена' if gift_settings.get('auto_send', True) else 'Выключена'}
• Выбор подарка: {'Пропускается' if gift_settings.get('skip_gift_selection', False) else 'Требуется'}

*Управление:*"""
            
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                  reply_markup=keyboard, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Ошибка в open_settings_menu: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
    
    def handle_change_message_cmd(message):
        try:    
            text = message.text.strip()
        
            if text == "-":
                gift_settings["message"] = ""
                status = "очищено"
                logger.info("Сообщение подарка очищено")
            else:
                gift_settings["message"] = text
                status = f"сохранено: {text[:50]}{'...' if len(text) > 50 else ''}"
                logger.info(f"Сообщение подарка сохранено: {text}")
            
            bot.send_message(message.chat.id, f"✅ Сообщение {status}",
                             reply_markup=K().add(B("◀️ Назад", callback_data=CBT_STARS_SETTINGS)))
            
        except Exception as e:
            logger.error(f"Ошибка в handle_change_message_cmd: {e}")
            bot.send_message(message.chat.id, f"❌ Ошибка: {e}")
        finally:
            tg.clear_state(message.chat.id, message.from_user.id)
    
    # ==================== Обработчики баланса и статистики ====================
    
    def show_balance(call):
        try:
            if not manager or not manager.senders:
                bot.answer_callback_query(call.id, "❌ Нет сессий", show_alert=True)
                return
            
            bot.answer_callback_query(call.id, "🔄 Получаю баланс...")
            
            def get_balances_and_send():
                try:
                    balances = manager.get_all_balances()
                    total = 0
                    response = "*💰 Баланс звезд:*\n\n"
                    
                    for bal in balances:
                        if "✅" in bal["status"]:
                            response += f"📱 {bal['session']}: `{bal['balance']:,}` ⭐\n"
                            total += bal['balance']
                        else:
                            response += f"📱 {bal['session']}: {bal['status']}\n"
                    
                    if total > 0:
                        response += f"\n💎 *ИТОГО:* `{total:,}` ⭐"
                    else:
                        response += f"\n💎 *ИТОГО:* 0 ⭐"
                    
                    keyboard = K()
                    keyboard.add(B("🔍 Детали", callback_data=CBT_STARS_VIEW_SESSION))
                    keyboard.add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
                    
                    bot.send_message(call.message.chat.id, response, reply_markup=keyboard, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Ошибка в show_balance: {e}")
                    bot.send_message(call.message.chat.id, f"❌ Ошибка получения баланса: {e}")
            
            threading.Thread(target=get_balances_and_send, daemon=True).start()
        except Exception as e:
            logger.error(f"Ошибка в show_balance обработке: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
    
    def show_statistics(call):
        try:
            stats = get_statistics_summary()
            daily_stats = ""
            
            for date, data in sorted(statistics["daily_stats"].items(), reverse=True)[:7]:
                daily_stats += f"• {date}: {data['gifts']} подарков ({data['revenue']}⭐)\n"
            
            text = f"""📈 *Статистика продаж*

*За день:*
• Подарков: {stats['today']['gifts']}
• Звезд: {stats['today']['revenue']} ⭐

*За неделю:*
• Подарков: {stats['week']['gifts']}
• Звезд: {stats['week']['revenue']} ⭐

*За месяц:*
• Подарков: {stats['month']['gifts']}
• Звезд: {stats['month']['revenue']} ⭐

*Всего за все время:*
• Подарков: {stats['total']['gifts']}
• Звезд: {stats['total']['revenue']} ⭐

*Последние 7 дней:*
{daily_stats if daily_stats else '• Нет данных'}"""
            
            keyboard = K()
            keyboard.row(
                B("🔄 Обновить", callback_data=CBT_STARS_STATS),
                B("🗑️ Очистить", callback_data=CBT_STARS_CLEAR_STATS)
            )
            keyboard.add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
            
            bot.edit_message_text(text, call.message.chat.id, call.message.message_id,
                                reply_markup=keyboard, parse_mode="Markdown")
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Ошибка в show_statistics: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
    
    def clear_statistics(call):
        global statistics
        statistics = {"total_sent": 0, "total_revenue": 0, "daily_stats": {}, "weekly_stats": {}, "monthly_stats": {}}
        save_config()
        bot.answer_callback_query(call.id, "✅ Статистика очищена")
        show_statistics(call)
    
    # ==================== Обработчики инициализации ====================
    
    def initialize_sessions(call):
        try:
            if not manager:
                bot.answer_callback_query(call.id, "❌ Нет сессий", show_alert=True)
                return
            
            bot.answer_callback_query(call.id, "🔄 Инициализация...")
            
            def init_and_report():
                try:
                    results = manager.initialize_all_sessions()
                    response = "*🔄 Результаты инициализации:*\n\n"
                    
                    for session_name, success, msg in results:
                        status = "✅" if success else "❌"
                        response += f"{status} {session_name}: {msg}\n"
                    
                    keyboard = K()
                    keyboard.add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
                    
                    bot.send_message(call.message.chat.id, response, reply_markup=keyboard, parse_mode="Markdown")
                except Exception as e:
                    logger.error(f"Ошибка в init_and_report: {e}")
                    bot.send_message(call.message.chat.id, f"❌ Ошибка инициализации: {e}")
            
            threading.Thread(target=init_and_report, daemon=True).start()
        except Exception as e:
            logger.error(f"Ошибка в initialize_sessions: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
    
    # ==================== Обработчики автоотправки ====================
    
    def run_bot(call):
        try:
            if not gift_lots:
                bot.answer_callback_query(call.id, "❌ Нет лотов", show_alert=True)
                return
            
            if not manager:
                bot.answer_callback_query(call.id, "❌ Нет сессий", show_alert=True)
                return
            
            active_count = sum(1 for s in manager.senders if s.initialized)
            if active_count == 0:
                bot.answer_callback_query(call.id, "❌ Нет активных сессий", show_alert=True)
                return
            
            gift_settings["auto_send"] = True
            save_config()
            
            logger.info("Автоотправка включена")
            bot.answer_callback_query(call.id, "✅ Автоотправка включена")
            
            bot.edit_message_text(f"✅ *Автоотправка включена!*\n\n• Отслеживается лотов: {len(gift_lots)}\n• Активных сессий: {active_count}\n• Бот будет автоматически обрабатывать новые заказы",
                                  call.message.chat.id, call.message.message_id,
                                  reply_markup=K().add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}")),
                                  parse_mode="Markdown")
            
        except Exception as e:
            logger.error(f"Ошибка в run_bot: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
    
    def stop_bot(call):
        try:
            gift_settings["auto_send"] = False
            save_config()
            
            logger.info("Автоотправка выключена")
            bot.answer_callback_query(call.id, "⏹️ Автоотправка выключена")
            
            bot.edit_message_text("⏹️ *Автоотправка выключена*",
                                  call.message.chat.id, call.message.message_id,
                                  reply_markup=K().add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}")),
                                  parse_mode="Markdown")
            
        except Exception as e:
            logger.error(f"Ошибка в stop_bot: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
    
    # ==================== Обработчики логов и восстановления ====================
    
    def show_logs(call):
        try:
            logs = get_logs(20)
            log_text = "📊 *Последние 20 логов:*\n\n"
            log_text += "```\n"
            for log in logs[-20:]:
                log_text += log.strip() + "\n"
            log_text += "```"
            
            keyboard = K()
            keyboard.add(B("🔄 Обновить", callback_data=CBT_STARS_SHOW_LOGS))
            keyboard.add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
            
            bot.edit_message_text(
                log_text,
                call.message.chat.id,
                call.message.message_id,
                reply_markup=keyboard,
                parse_mode="Markdown"
            )
            bot.answer_callback_query(call.id)
        except Exception as e:
            logger.error(f"Ошибка в show_logs: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
    
    
    def restore_lots_handler(call):
        try:
            if not gift_lots:
                bot.answer_callback_query(call.id, "❌ Нет лотов в настройках", show_alert=True)
                return
            
            bot.answer_callback_query(call.id, "🔄 Восстанавливаю лоты...")
            
            def restore_and_report():
                try:
                    from curl_cffi import requests as creq
                    
                    restored = 0
                    failed = 0
                    
                    headers_post = {
                        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/127.0.0.1 Safari/537.36',
                        'Accept': 'application/json, text/javascript, */*; q=0.01',
                        'Accept-Language': 'ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7',
                        'Content-Type': 'application/x-www-form-urlencoded; charset=UTF-8',
                        'X-Requested-With': 'XMLHttpRequest',
                        'sec-ch-ua': '"Chromium";v="136", "Google Chrome";v="136", "Not.A/Brand";v="99"',
                        'sec-ch-ua-mobile': '?0',
                        'sec-ch-ua-platform': '"Windows"',
                    }
                    
                    for lot_id in gift_lots:
                        try:
                            logger.info(f"Восстановление лота {lot_id}...")
                            
                            # 1. Get lot fields via FunPayAPI (updates PHPSESSID)
                            lot_fields = cardinal.account.get_lot_fields(int(lot_id))
                            
                            if lot_fields.active:
                                logger.info(f"Лот {lot_id} уже активен")
                                restored += 1
                                continue
                            
                            # 2. Activate and renew fields (includes form_created_at)
                            lot_fields.active = True
                            lot_fields.renew_fields()
                            
                            # 3. POST via curl_cffi with browser TLS fingerprint
                            session = creq.Session()
                            session.cookies.update({
                                'golden_key': cardinal.account.golden_key,
                                'PHPSESSID': cardinal.account.phpsessid,
                            })
                            
                            headers_post_lot = headers_post.copy()
                            headers_post_lot['Referer'] = f'https://funpay.com/lots/offerEdit?node=2418&offer={lot_id}'
                            headers_post_lot['Origin'] = 'https://funpay.com'
                            
                            r = session.post(
                                'https://funpay.com/lots/offerSave',
                                data=lot_fields.fields, headers=headers_post_lot,
                                impersonate='chrome136'
                            )
                            
                            response_text = r.text.replace('\\/', '/')
                            if r.status_code == 200 and '"url":"https://funpay.com/lots/2418/trade"' in response_text:
                                logger.info(f"✅ Лот {lot_id} восстановлен")
                                restored += 1
                            else:
                                logger.error(f"❌ Лот {lot_id}: {r.status_code} - {r.text[:200]}")
                                failed += 1
                            
                        except Exception as e:
                            logger.error(f"❌ Ошибка восстановления лота {lot_id}: {e}")
                            failed += 1
                    
                    response_text = f"✅ *Восстановление лотов завершено*\n\n• Успешно: {restored}\n• Не удалось: {failed}"
                    keyboard = K()
                    keyboard.add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
                    bot.send_message(call.message.chat.id, response_text, reply_markup=keyboard, parse_mode="Markdown")
                    
                except Exception as e:
                    logger.error(f"Ошибка в restore_and_report: {e}")
                    import traceback
                    traceback.print_exc()
                    bot.send_message(call.message.chat.id, f"❌ Ошибка восстановления: {e}")
            
            threading.Thread(target=restore_and_report, daemon=True).start()
            
        except Exception as e:
            logger.error(f"Ошибка в restore_lots_handler: {e}")
            bot.answer_callback_query(call.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
    
    # ==================== Обработчики экспорта/импорта сессий ====================
    
    def export_session_callback(call):
        if not manager or not manager.senders:
            bot.answer_callback_query(call.id, "❌ Нет сессий для экспорта", show_alert=True)
            return
        keyboard = K()
        for sender in manager.senders:
            if sender.initialized:
                keyboard.add(B(f"📤 {sender.session_name}", callback_data=f"stars_export_do:{sender.session_name}"))
        keyboard.add(B("◀️ Назад", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        bot.edit_message_text(
            "📤 *Экспорт строки сессии*\n\nВыберите сессию для экспорта:",
            call.message.chat.id, call.message.message_id,
            reply_markup=keyboard, parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)
    
    def do_export_callback(call):
        session_name = call.data.replace("stars_export_do:", "")
        bot.answer_callback_query(call.id, "🔄 Экспортирую...")
        
        def export_and_send():
            if manager:
                string_session, msg = manager.export_session_string(session_name)
                if string_session:
                    text = f"✅ *Строка сессии для {session_name}:*\n\n```\n{string_session}\n```\n\n⚠️ Храните в безопасности!"
                    bot.send_message(call.message.chat.id, text, parse_mode="Markdown")
                else:
                    bot.send_message(call.message.chat.id, f"❌ Ошибка: {msg}")
        
        threading.Thread(target=export_and_send, daemon=True).start()
    
    def import_session_callback(call):
        tg.set_state(call.message.chat.id, call.message.message_id, call.from_user.id,
                     CBT_STARS_IMPORT_SESSION, {"step": "name"})
        keyboard = K().add(B("❌ Отмена", callback_data=f"{CBT.PLUGIN_SETTINGS}:{UUID}"))
        bot.edit_message_text(
            "📥 *Импорт строки сессии*\n\n"
            "Введите имя для новой сессии *или* отправьте всё одной строкой:\n"
            "`name:api_id:api_hash:session_string`",
            call.message.chat.id, call.message.message_id,
            reply_markup=keyboard, parse_mode="Markdown"
        )
        bot.answer_callback_query(call.id)
    
    # ==================== Обработчики сообщений с состояниями ====================
    
    @bot.message_handler(func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_STARS_ADD_SESSION))
    def handle_add_session_wrapper(m):
        handle_add_session_cmd(m)
    
    @bot.message_handler(func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_STARS_DEL_SESSION))
    def handle_del_session_wrapper(m):
        handle_del_session_cmd(m)
    
    @bot.message_handler(func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_STARS_ADD_LOT))
    def handle_add_lot_wrapper(m):
        handle_add_lot_cmd(m)
    
    @bot.message_handler(func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_STARS_DEL_LOT))
    def handle_del_lot_wrapper(m):
        handle_del_lot_cmd(m)
    
    @bot.message_handler(func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_STARS_CHANGE_MSG))
    def handle_change_message_wrapper(m):
        handle_change_message_cmd(m)
    
    @bot.message_handler(func=lambda m: tg.check_state(m.chat.id, m.from_user.id, CBT_STARS_IMPORT_SESSION))
    def import_session_message_handler(m):
        user_state = tg.get_state(m.chat.id, m.from_user.id)
        if not user_state:
            return
        data = user_state["data"]
        step = data["step"]
        text = m.text.strip()

        if step == "name" and text.count(":") >= 3:
            parts = text.split(":", 3)
            name, api_id, api_hash, session_string = parts[0], parts[1], parts[2], parts[3]
            if manager:
                success, msg = manager.import_session_string(name, api_id, api_hash, session_string)
                tg.clear_state(m.chat.id, m.from_user.id)
                bot.send_message(m.chat.id, f"{'✅' if success else '❌'} {msg}")
            else:
                tg.clear_state(m.chat.id, m.from_user.id)
                bot.send_message(m.chat.id, "❌ Менеджер не инициализирован")
            return

        if step == "name":
            data["session_name"] = text
            data["step"] = "api_id"
            tg.set_state(m.chat.id, m.message_id, m.from_user.id, CBT_STARS_IMPORT_SESSION, data)
            bot.send_message(m.chat.id, "📱 Введите API ID:")
        elif step == "api_id":
            data["api_id"] = text
            data["step"] = "api_hash"
            tg.set_state(m.chat.id, m.message_id, m.from_user.id, CBT_STARS_IMPORT_SESSION, data)
            bot.send_message(m.chat.id, "🔑 Введите API Hash:")
        elif step == "api_hash":
            data["api_hash"] = text
            data["step"] = "session_string"
            tg.set_state(m.chat.id, m.message_id, m.from_user.id, CBT_STARS_IMPORT_SESSION, data)
            bot.send_message(m.chat.id, "🔐 Введите строку сессии:")
        elif step == "session_string":
            data["session_string"] = text
            if manager:
                success, msg = manager.import_session_string(
                    data["session_name"], data["api_id"], data["api_hash"], data["session_string"]
                )
                tg.clear_state(m.chat.id, m.from_user.id)
                bot.send_message(m.chat.id, f"{'✅' if success else '❌'} {msg}")
    
    @bot.message_handler(func=lambda m: True)
    def general_message_handler(m):
        if process_auth_message(m):
            return
    
    # ==================== Регистрация callback-обработчиков ====================
    
    @bot.callback_query_handler(func=lambda c: f"{CBT.PLUGIN_SETTINGS}:{UUID}" in c.data)
    def handle_plugin_settings(c):
        open_plugin_settings(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_VIEW_SESSION)
    def handle_view_session(c):
        open_sessions_menu(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_SETTINGS)
    def handle_settings(c):
        open_settings_menu(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_LOTS_MENU)
    def handle_lots_menu(c):
        open_lots_menu(c)
    
    @bot.callback_query_handler(func=lambda c: c.data.startswith("stars_lot_info:"))
    def handle_lot_info(c):
        try:
            lot_id = c.data.replace("stars_lot_info:", "")
            bot.answer_callback_query(c.id, f"📦 Лот ID: {lot_id}")
            keyboard = K()
            keyboard.add(B("◀️ Назад", callback_data=CBT_STARS_LOTS_MENU))
            bot.edit_message_text(
                f"📦 *Информация о лоте*\n\nID: `{lot_id}`\n\nСтатус: {'✅ Активен' if lot_id in gift_lots else '❌ Не отслеживается'}",
                c.message.chat.id, c.message.message_id,
                reply_markup=keyboard, parse_mode="Markdown"
            )
        except Exception as e:
            logger.error(f"Ошибка в handle_lot_info: {e}")
            bot.answer_callback_query(c.id, f"❌ Ошибка: {str(e)[:100]}", show_alert=True)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_BALANCE)
    def handle_balance(c):
        show_balance(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_STATS)
    def handle_stats(c):
        show_statistics(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_CLEAR_STATS)
    def handle_clear_stats(c):
        clear_statistics(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_SHOW_LOGS)
    def handle_show_logs(c):
        show_logs(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_INIT_SESSIONS)
    def handle_init_sessions(c):
        initialize_sessions(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_RUN)
    def handle_run(c):
        run_bot(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_STOP)
    def handle_stop(c):
        stop_bot(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_ADD_SESSION)
    def handle_add_session_btn(c):
        tg.set_state(c.message.chat.id, c.message.message_id, c.from_user.id, CBT_STARS_ADD_SESSION, {})
        bot.send_message(c.message.chat.id, "📱 Введите данные сессии в формате:\nsession_name:api_id:api_hash\n\nПример:\nmy_session:123456:abcdef0abcdef",
                         reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
        bot.answer_callback_query(c.id)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_DEL_SESSION)
    def handle_del_session_btn(c):
        tg.set_state(c.message.chat.id, c.message.message_id, c.from_user.id, CBT_STARS_DEL_SESSION, {})
        bot.send_message(c.message.chat.id, "🗑️ Введите имя сессии для удаления:",
                         reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
        bot.answer_callback_query(c.id)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_EXPORT_SESSION)
    def handle_export_session(c):
        export_session_callback(c)
    
    @bot.callback_query_handler(func=lambda c: c.data.startswith("stars_export_do:"))
    def handle_do_export(c):
        do_export_callback(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_IMPORT_SESSION)
    def handle_import_session(c):
        import_session_callback(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_ADD_LOT)
    def handle_add_lot_btn(c):
        tg.set_state(c.message.chat.id, c.message.message_id, c.from_user.id, CBT_STARS_ADD_LOT, {})
        bot.send_message(c.message.chat.id, "📦 Введите ID лота:")
        bot.answer_callback_query(c.id)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_DEL_LOT)
    def handle_del_lot_btn(c):
        tg.set_state(c.message.chat.id, c.message.message_id, c.from_user.id, CBT_STARS_DEL_LOT, {})
        bot.send_message(c.message.chat.id, "🗑️ Введите ID лота для удаления:",
                         reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
        bot.answer_callback_query(c.id)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_RESTORE_LOTS)
    def handle_restore_lots(c):
        restore_lots_handler(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_CHANGE_MSG)
    def handle_change_message_btn(c):
        tg.set_state(c.message.chat.id, c.message.message_id, c.from_user.id, CBT_STARS_CHANGE_MSG, {})
        current = gift_settings.get("message", "")
        bot.send_message(c.message.chat.id, f"✉️ Введите сообщение для подарков:\n\nТекущее: {current if current else 'Не задано'}\n\nДля очистки отправьте '-'",
                         reply_markup=tg_bot.static_keyboards.CLEAR_STATE_BTN())
        bot.answer_callback_query(c.id)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_TOGGLE_VISIBILITY)
    def handle_toggle_visibility(c):
        gift_settings["hide_name"] = not gift_settings.get("hide_name", True)
        save_config()
        logger.info(f"Видимость изменена: {'скрыт' if gift_settings['hide_name'] else 'виден'}")
        bot.answer_callback_query(c.id, "✅ Настройка сохранена")
        open_settings_menu(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_TOGGLE_AUTO)
    def handle_toggle_auto(c):
        gift_settings["auto_send"] = not gift_settings.get("auto_send", True)
        save_config()
        logger.info(f"Автоотправка изменена: {'включена' if gift_settings['auto_send'] else 'выключена'}")
        bot.answer_callback_query(c.id, "✅ Настройка сохранена")
        open_settings_menu(c)
    
    @bot.callback_query_handler(func=lambda c: c.data == CBT_STARS_TOGGLE_SKIP)
    def handle_toggle_skip(c):
        gift_settings["skip_gift_selection"] = not gift_settings.get("skip_gift_selection", False)
        save_config()
        logger.info(f"Режим выбора подарка изменен: {'пропускать' if gift_settings['skip_gift_selection'] else 'выбирать'}")
        bot.answer_callback_query(c.id, "✅ Настройка сохранена")
        open_settings_menu(c)
    
    @bot.callback_query_handler(func=lambda c: CBT_STARS_AUTH in c.data and not c.data.startswith("StarsAuthComplete"))
    def handle_auth(c):
        start_auth_session(c)

def order_hook(cardinal: Cardinal, event):
    """Хук для новых заказов"""
    from FunPayAPI.updater.events import NewOrderEvent
    
    if not isinstance(event, NewOrderEvent):
        return
    
    if not gift_settings.get("auto_send", True):
        return

    if not gift_lots:
        return
    
    order_data = {
        "order_id": event.order.id,
        "buyer_username": event.order.buyer_username,
        "description": event.order.description,
        "price": event.order.price,
        "amount": event.order.parse_amount(),
        "received_at": time.time()
    }
    
    if async_thread and async_thread.order_manager:
        run_async_in_thread(async_thread.order_manager.process_order, order_data)

def message_hook(cardinal: Cardinal, event):
    """Хук для новых сообщений"""
    from FunPayAPI.updater.events import NewMessageEvent
    
    if not isinstance(event, NewMessageEvent):
        return
    
    try:
        username = event.message.author
        message = event.message.text.strip()
        
        logger.info(f"Получено сообщение от {username}: {message}")
        
        # Обработка системных уведомлений о возврате
        if "вернул деньги покупателю" in message or "вернул оплату" in message:
            match = re.search(r'#([a-zA-Z0-9_]{5,32})', message)
            if match:
                del_order = match.group(1)
                logger.info(f'Обнаружен возврат по заказу {del_order}, очищаем из плагина')
                if async_thread and async_thread.order_manager:
                    async_thread.order_manager.cleanup_order(del_order)
        
        # Пропускаем системные оповещения FunPay
        if username == "FunPay оповещение":
            return
        
        if async_thread and async_thread.order_manager:
            run_async_in_thread(async_thread.order_manager.process_message, username, message)
            
    except Exception as e:
        logger.error(f"Ошибка в message_hook: {e}")

# Хуки для Cardinal
BIND_TO_PRE_INIT = [init]
BIND_TO_NEW_MESSAGE = [message_hook]
BIND_TO_NEW_ORDER = [order_hook]
BIND_TO_DELETE = None