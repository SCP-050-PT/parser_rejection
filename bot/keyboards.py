# bot/keyboards.py
"""
Клавиатуры для VK бота.
Парсер отказов zakupki.gov.ru.
Версия: Автоматический режим (упрощенное меню)
"""

from vkbottle import Keyboard, KeyboardButtonColor, Text


class Buttons:
    """Тексты кнопок главного меню"""

    SETTINGS = "⚙️ Текущие настройки"
    NOTIFICATIONS = "🔔 Уведомления"
    FILE = "📁 Файл на Диске"
    ADMIN_PANEL = "👥 Админ-панель"
    BACK = "🔙 Назад"


def get_main_menu(is_admin: bool = False) -> str:
    """Главное меню (reply keyboard для VK)"""
    kb = Keyboard(one_time=False, inline=False)

    if is_admin:
        kb.add(Text(Buttons.SETTINGS), color=KeyboardButtonColor.SECONDARY)
        kb.add(Text(Buttons.FILE), color=KeyboardButtonColor.SECONDARY)
        kb.row()
        kb.add(Text(Buttons.ADMIN_PANEL), color=KeyboardButtonColor.PRIMARY)
        kb.add(Text(Buttons.NOTIFICATIONS), color=KeyboardButtonColor.SECONDARY)
    else:
        kb.add(Text(Buttons.SETTINGS), color=KeyboardButtonColor.SECONDARY)
        kb.row()
        kb.add(Text(Buttons.FILE), color=KeyboardButtonColor.SECONDARY)
        kb.row()
        kb.add(Text(Buttons.NOTIFICATIONS), color=KeyboardButtonColor.SECONDARY)

    return kb.get_json()


def get_back_keyboard() -> str:
    """Клавиатура только с кнопкой 'Назад'"""
    kb = Keyboard(one_time=True, inline=False)
    kb.add(Text(Buttons.BACK), color=KeyboardButtonColor.NEGATIVE)
    return kb.get_json()


def get_empty_keyboard() -> str:
    """Скрывает клавиатуру"""
    kb = Keyboard(one_time=True, inline=False)
    return kb.get_json()
