# Источники и обновление знаний

Проверено **2026-10-26**. На момент проверки актуальный changelog: **Bot API 10.3, 24 августа 2026**. Наличие записи в справочнике не заменяет проверку установленной версии и текущего контракта. Официальные страницы могут отдавать сокращённый markdown со ссылками на отдельные методы/типы: переходи по ним, не делай вывод «метода нет» по сокращённой главной странице.

## Основные источники

| Область | Источник |
|---|---|
| Статья, указанная оператором | https://telegram.org/blog/communities-editor-invisible-messages/ru |
| Введение rich для ботов | https://telegram.org/blog/watch-apps-and-more/ru |
| Текущий API | https://core.telegram.org/bots/api.md |
| Изменения и замены параметров | https://core.telegram.org/bots/api-changelog.md |
| Все доступные типы | https://core.telegram.org/bots/api/types.md |
| Обычное форматирование | https://core.telegram.org/bots/api/formatting.md |
| Rich grammar/лимиты/кнопки | https://core.telegram.org/bots/api/rich-messages.md |
| Rich input | https://core.telegram.org/bots/api/types/inputrichmessage.md |
| Отправка rich | https://core.telegram.org/bots/api/methods/sendrichmessage.md |
| Обычная отправка | https://core.telegram.org/bots/api/methods/sendmessage.md |
| Обычная inline-кнопка | https://core.telegram.org/bots/api/types/inlinekeyboardbutton.md |
| Rich-кнопка | https://core.telegram.org/bots/api/types/richmessagebutton.md |
| Персональный ответ в группе | https://core.telegram.org/bots/api/types/ephemeralmessageparameters.md |
| Получение updates/webhook | https://core.telegram.org/bots/api/methods/setwebhook.md |
| Настройки/поверхности/command scopes | https://core.telegram.org/bots/features.md |
| FAQ (возможны старые разделы) | https://core.telegram.org/bots/faq.md |
| Mini App и проверка initData | https://core.telegram.org/bots/webapps.md |
| Платежи за цифровые товары/услуги | https://core.telegram.org/bots/payments-stars.md |
| Другие платежи | https://core.telegram.org/bots/payments.md |
| Client API/TDLib и границы Bot API | https://core.telegram.org/api |
| Telegram usage/privacy/terms | https://telegram.org/tos и https://telegram.org/privacy |

## SDK

- aiogram: https://docs.aiogram.dev/en/latest/ ; https://github.com/aiogram/aiogram/releases . Проверен **3.31.0**, Bot API 10.3; поддерживает `InputRichMessage`, `SendRichMessage`, `EphemeralMessageParameters`, `bot.send_rich_message()`.
- grammY для существующего TypeScript-проекта: https://grammy.dev/ ; точную поддержку новых API проверь по зависимостям и release notes, версия в этом скилле не зафиксирована.
- python-telegram-bot для существующего проекта: https://docs.python-telegram-bot.org/ ; не мигрируй на aiogram только ради применения скилла.

Локальная версия SDK относится к **конкретному интерпретатору/venv**. Для Python сообщай `sys.executable`, `aiogram.__version__` и используй `python -m pip` именно этого окружения. Проверка случайного `python` из PATH не доказывает состояние проекта. Не обновляй глобальные пакеты из-за несовпадения с другим интерпретатором; в ответе отличай выполненную проверку от инструкции проверки.

При доступном Context7 сначала resolve library ID для используемого SDK, затем query по установленной версии и конкретной возможности. Для использованной проверки получен `/aiogram/aiogram/v3.31.0`. Официальный HTTP-контракт первичен при разногласии с поисковым сниппетом SDK. Если инструмента Context7 нет, читай официальные SDK docs/release notes напрямую.

## Что перепроверять первым

1. Точный endpoint, поля, обязательность, ограничения chat type/rights.
2. Сериализуемые модели SDK и lockfile; неизвестный параметр не должен исчезнуть из payload.
3. Лимиты текста/медиа и единицы измерения: rich chars, entity UTF-16 units, callback UTF-8 bytes.
4. Rich/ephemeral: в 10.3 top-level receiver_user_id/callback_query_id заменены `ephemeral_message_parameters`; не копируй пример 10.2 без миграции.
5. Тип кнопки и её отдельные ограничения. `style=link` у rich callback не доказывает поддержку у ordinary InlineKeyboardButton.
6. FAQ может отставать: абсолютный запрет bot-to-bot и совет прятать token в webhook URL не применяй вместо текущих features/changelog и отдельного webhook secret.
7. Цены/требования paid broadcasts/Stars — проверяй непосредственно перед согласованной платной функцией. Не обещай постоянные цены или фиксированную пропускную способность.

Сохраняй в состоянии проекта только относящиеся к задаче решения, версии, дату и проверенные ограничения. Если доступа к сети/докам нет, явно обозначь неизвестное; не утверждай, что свежие возможности отсутствуют лишь потому, что модель их не помнит.
