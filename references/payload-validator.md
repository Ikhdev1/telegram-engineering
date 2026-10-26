# Валидатор Telegram payload

Локальная офлайн-проверка **структуры** запросов Bot API. Не отправляет запросы, не требует токена, сети, SDK или сторонних пакетов. Это не серверный парсер Telegram: успешный локальный результат означает только «в известном снимке контракта не найдено ошибок», а не «Telegram примет сообщение».

Снимок: **Bot API 10.3, 24 августа 2026**; проверено 2026-10-26 по официальным страницам методов и типов. При обновлении API сначала перечитай контракт и changelog, затем правь снимок полей и тесты.

- [Bot API](https://core.telegram.org/bots/api.md), [changelog](https://core.telegram.org/bots/api-changelog.md)
- [sendMessage](https://core.telegram.org/bots/api/methods/sendmessage.md), [editMessageText](https://core.telegram.org/bots/api/methods/editmessagetext.md), [answerCallbackQuery](https://core.telegram.org/bots/api/methods/answercallbackquery.md)
- [sendRichMessage](https://core.telegram.org/bots/api/methods/sendrichmessage.md), [editEphemeralMessageText](https://core.telegram.org/bots/api/methods/editephemeralmessagetext.md)
- [InputRichMessage](https://core.telegram.org/bots/api/types/inputrichmessage.md), [rich messages](https://core.telegram.org/bots/api/rich-messages.md)
- [EphemeralMessageParameters](https://core.telegram.org/bots/api/types/ephemeralmessageparameters.md), [InlineKeyboardButton](https://core.telegram.org/bots/api/types/inlinekeyboardbutton.md), [RichMessageButton](https://core.telegram.org/bots/api/types/richmessagebutton.md)
- [ReplyParameters](https://core.telegram.org/bots/api/types/replyparameters.md), [RichBlock](https://core.telegram.org/bots/api/types/richblock.md), [RichText](https://core.telegram.org/bots/api/types/richtext.md)
- [Formatting](https://core.telegram.org/bots/api/formatting.md), [MessageEntity](https://core.telegram.org/bots/api/types/messageentity.md)

## Что покрыто

| Метод | Обязательные данные | Что проверяется дополнительно |
|---|---|---|
| `sendMessage` | `chat_id`, `text` | обычный/HTML/Markdown текст, `parse_mode`/`entities`, лимит 1–4096, ссылки, `reply_markup`, `reply_parameters`, `link_preview_options`, `ephemeral_message_parameters` |
| `sendRichMessage` | `chat_id`, `rich_message` | ровно один из `html`/`markdown`/`blocks`; rich HTML/лимиты/таблицы/медиа; rich-кнопки; `reply_parameters`; `reply_markup`; ephemeral-контекст |
| `editMessageText` | `inline_message_id` **или** `chat_id`+`message_id`; ровно один из `text`/`rich_message` | конфликт адресов, несовместимость `parse_mode`/`entities` с rich, только inline `reply_markup`, превью ссылки |
| `editEphemeralMessageText` | `chat_id`, `receiver_user_id`, `ephemeral_message_id`; ровно один из `text`/`rich_message` | верхнеуровневые поля ephemeral-редактирования, inline `reply_markup`, ограничения ephemeral |
| `answerCallbackQuery` | `callback_query_id` | текст 0–200, `show_alert`, URL, `cache_time` |

Другие методы намеренно не угадываются: для них `validate_request` возвращает finding `unsupported_method`, CLI завершается с кодом 2. Имена пяти поддерживаемых методов сравниваются без учёта регистра, как и в Bot API (`SendMessage` = `sendMessage`); граница набора методов от этого не расширяется. Так валидатор не делает вид, что знает контракт `sendPhoto`, платежей, медиа-групп или draft-методов.

## Публичный интерфейс

```python
from telegram_payload import validate_request

findings = validate_request("sendMessage", {
    "chat_id": 123456789,
    "text": "<b>Готово</b>",
    "parse_mode": "HTML",
})
```

Каждый finding — словарь ровно с четырьмя ключами:

```json
{
  "severity": "error",
  "code": "text_too_long",
  "path": "$.text",
  "message": "text exceeds the ordinary-message limit of 4096 characters after parsing."
}
```

`severity` — только `error` или `warning`. `path` — JSONPath-подобный путь; например `$.reply_markup.inline_keyboard[0][1].callback_data`. Сообщения не содержат текст сообщения, callback data, URL, идентификаторы, имена anchor или entity: только имена полей, тегов, типов и числовые лимиты/размеры. Это позволяет безопасно выводить findings в лог.

Все строки payload, включая вложенные ключи и значения, проверяются на UTF-8: unpaired surrogate даёт `invalid_unicode`. Markdown-подобный текст и rich HTML проверяются эвристически, но кодировка проверяется до разбора.

В выводе сохраняются первые 200 findings; при отбрасывании добавляется marker `findings_truncated` (итого до 201 элемента). Если из-за лимита отбрасывается **ошибка**, marker имеет `severity=error`; CLI возвращает 1. Предупреждения не могут скрыть позднюю ошибку и дать exit 0; `validator_failure` остаётся видимым и тоже даёт exit 1.

Функция не бросает исключение на неизвестный метод или payload не-объект: возвращает finding. Неожиданный сбой внутри самой проверки превращается в `validator_failure`, чтобы CLI не падал.

## CLI

```
python scripts/telegram_payload.py --method METHOD --file PATH [--json]
```

`--file` — **сырое тело запроса**, а не конверт фикстуры. `--method` принимает те же пять имён без учёта регистра. `--json` печатает массив findings в JSON; без флага — строки `SEVERITY code path: message`.

| Код выхода | Значение |
|---|---|
| 0 | ошибок нет; warnings допустимы |
| 1 | есть хотя бы один finding с `severity=error` |
| 2 | неизвестный метод, ошибка аргументов, чтения файла, UTF-8 или разбора JSON |

Точные команды из корня пакета (используй активный Python/venv проекта):

```
python scripts/telegram_payload.py --method sendMessage --file request.json
python scripts/telegram_payload.py --method sendRichMessage --file request.json --json
python scripts/telegram_payload.py --method editMessageText --file request.json
python -m unittest discover -s tests -v
```

Пример сырого `request.json` для `sendMessage`:

```json
{
  "chat_id": 123456789,
  "text": "<b>Заявка №42</b>\nСтатус: подтверждена",
  "parse_mode": "HTML"
}
```

## Фикстуры

`assets/payloads/*.json` — реалистичные **валидные** примеры в конверте:

```json
{
  "method": "sendMessage",
  "payload": {
    "chat_id": 123456789,
    "text": "hello"
  }
}
```

| Файл | Метод | Назначение |
|---|---|---|
| `send_message_ticket.json` | `sendMessage` | карточка заявки, HTML-экранирование, inline-кнопки |
| `send_rich_pricing.json` | `sendRichMessage` | rich-таблица, caption, details, строка кнопок |
| `send_rich_article.json` | `sendRichMessage` | rich-статья: заголовки, anchor, figure, blockquote, чеклист, details |
| `edit_message_status.json` | `editMessageText` | редактирование карточки и inline-клавиатуры |
| `edit_ephemeral_text.json` | `editEphemeralMessageText` | верхнеуровневые ephemeral-поля |
| `send_ephemeral_reply.json` | `sendMessage` | вложенный `ephemeral_message_parameters` |
| `send_reply_keyboard.json` | `sendMessage` | reply-клавиатура и запрос контакта |
| `answer_callback_toast.json` | `answerCallbackQuery` | короткий toast |

CLI не разворачивает конверт: тесты и внешние инструменты берут `payload` и вызывают `validate_request(method, payload)`; для CLI сохрани `payload` отдельным файлом. Конверт выбран, чтобы каталог фикстур был самоописательным и его мог проверить упаковщик скилла. Для rich-фикстур ожидаем warning `rich_html_unverified` (о проверке сниппетом, а не серверным парсером); ошибок в фикстурах нет, поэтому CLI на них завершается с кодом 0.

## Обычный текст и HTML

- Лимит **1–4096 символов после разбора entities**. Валидатор считает Unicode code points, а не UTF-8 байты: 3000 emoji проходят (12000 байт), 5000 emoji — ошибка. Официальная страница говорит «characters»; серверный счётчик остаётся источником истины.
- Без `parse_mode` текст считается буквальным; HTML-теги в нём не разметка.
- `chat_id`: integer (не 0) или username `@username` длиной 5–32 символов. Чистая десятичная строка (`"123"`, `"-100..."`) принимается как явное локальное исключение для SDK; Bot API документирует строковый target как username, поэтому серверная гарантия для числовой строки не подразумевается. Прочие строки — ошибка `invalid_chat_username`.
- `parse_mode` — только `HTML`, `MarkdownV2`, `Markdown`; `entities` и `parse_mode` взаимоисключающие.
- MarkdownV2/legacy Markdown проверяются огрублённо: выводится `markdown_syntax_unverified`. Code spans и fenced code считаются как видимый литеральный текст; legacy `>`, `|`, `~` — обычные символы. Если оценка видимого текста равна нулю, это `empty_text_unverified` warning, а не жёсткий `empty_text`; серверный парсер остаётся авторитетным. Если исходник длиннее 4096, но оценка в пределах лимита, добавляется `text_length_unverified`.
- Для HTML разрешены только `b/strong`, `i/em`, `u/ins`, `s/strike/del`, `span class="tg-spoiler"`, `tg-spoiler`, `a href`, `code`, `pre`, `blockquote`/`expandable`, `tg-emoji`, `tg-time`. `<h1>`, `<table>`, `<details>`, `<p>`, `<br>` в обычном HTML не поддерживаются.
- Именованные entities: только `&lt; &gt; &amp; &quot;`; numeric entities разрешены (кроме нуля, суррогатов и выхода за Unicode). Сырые `<` и `&` в тексте — ошибка; сырой `>` — warning.
- Проверяются парность тегов, атрибуты, `class="language-..."` только у `<code>` внутри `<pre>`, запрет форматирования внутри `pre/code`, вложенных ссылок и blockquote (включая вложенность через промежуточные formatting-теги). Комментарии, DOCTYPE, processing instructions и `<!ENTITY ...>` отклоняются.

`entities`: `offset`/`length` — целые в **UTF-16 code units**, не Python `len()` и не байты. Проверяются неотрицательность, положительная длина, границы текста, попадание на границу суррогатной пары, известные типы Bot API 10.3, обязательные поля (`text_link.url`, `text_mention.user`, `custom_emoji.custom_emoji_id`, `date_time.unix_time`) и документированные правила вложенности. `entities` без `text` — ошибка.

## Rich messages

`rich_message` принимает ровно одно из `html`, `markdown`, `blocks`; поля `media`, `is_rtl`, `skip_entity_detection` проверяются по типам. Неизвестные поля внутри `rich_message`, `media` и `ephemeral_message_parameters` — ошибки снимка 10.3.

Rich HTML проверяется как приближение поддерживаемого набора Bot API 10.3: заголовки `h1`–`h6`, `p`, `pre`/`code`, `footer`, `hr`, списки, `blockquote`/`aside`, `details`/`summary`, `table`/`tr`/`th`/`td`/`caption`, `figure`/`figcaption`, media-теги, `tg-map`, `tg-collage`/`tg-slideshow`, `tg-math`/`tg-math-block`, `tg-reference`, `tg-emoji`, `tg-time`, `a name`/`href`, `tg-button`/`tg-button-row`, `input type="checkbox"`. Неизвестный тег — ошибка; неизвестный атрибут — ошибка. Это снимок документации, а не доказательство, что клиент не поддержит больше.

Content model: media-блоки (`img`, `video`, `audio`, `tg-document`) допустимы на верхнем уровне, внутри `figure`, `tg-collage`, `tg-slideshow`, `details`, `li` и `blockquote`. RichText-only контейнеры (`p`, `h1`–`h6`, `footer`, `aside`, `summary`, `figcaption`, `caption`, `cite`, `tg-reference`, `tg-math`, `tg-math-block`, `a`) не могут содержать block/media children; табличные ячейки — только inline. Rich blockquote может содержать вложенный RichBlock; ordinary blockquote-entities по-прежнему не вкладываются.

Что считается точно:

- видимый текст rich HTML — до **32768 символов** (code points); теги не входят, текст внутри `tg-emoji` и формулы входят;
- более **20 колонок** в строке таблицы (с учётом `colspan`) — ошибка;
- более **50 медиаэлементов** в rich HTML или в `media` (без двойного счёта) — ошибка; Markdown-изображения считаются эвристически и при превышении дают `rich_media_limit_unverified`;
- media-блоки внутри `details`, `li` и `blockquote` разрешены как RichBlock children; block/media внутри RichText-only контейнеров — ошибка;
- callback-данные rich-кнопки — 1–64 **UTF-8 байта**;
- отсутствие `media`-записи для `tg://photo|video|document|audio?id=...` в HTML — ошибка; в Markdown такой regex-намек даёт warning, потому что контекст кода/ссылки не разобран;
- нарушение форм `figure`/`tg-collage`/`tg-slideshow`, пустая строка кнопок, непустой label у кнопки.

Что является **warning**, а не приговором:

- rich HTML вообще: сам allowlist тегов/атрибутов — приближение снимка Bot API 10.3, серверный парсер остаётся авторитетным;
- исходный HTML глубже 16 уровней или больше 500 блочных узлов: соответствие HTML-дерева серверным блокам неоднозначно;
- `markdown` длиннее 32768: сервер считает уже разобранный rich-текст; локально проверяется только исходник;
- `markdown` вообще: синтаксис не разбирается полностью;
- Markdown-изображений больше 50: счётчик по regex, а не по разобранному дереву;
- `blocks`: проверяются только массив, объекты и непустой `type`; схемы конкретных блоков не подменяются догадками;
- неразрешённый `#anchor`: warning, потому что сервер/клиент может иметь свои правила;
- `rich_button` вне `p`/`tg-button-row`, отсутствие `summary` у `details`.

Rich `tg-button`: `type` обязателен и должен быть одним из `url`, `callback_data`, `web_app`, `login_url`, `switch_inline_query`, `switch_inline_query_current_chat`, `switch_inline_query_chosen_chat`, `copy_text`, `disabled`. Проверяются обязательные и лишние action-атрибуты, `data` 1–64 байта, HTTPS для `web_app`/`login_url`, `style` только `danger/success/primary/link`, причём `link` — только для `callback_data`. У `copy_text` атрибут `text` 1–256 символов. Label может содержать только текст, `tg-emoji` и `tg-time`.

Rich Markdown и `blocks` не считаются полностью проверенными: валидатор не должен отклонять валидную разметку из-за неполного локального парсера. Для них явно выводятся предупреждения.

## Клавиатуры и кнопки

- `inline_keyboard` — массив непустых массивов кнопок. Для `InlineKeyboardButton` ровно одно action-поле; `text`, `style`, `icon_custom_emoji_id` action не считаются.
- `style` у обычной кнопки — только `danger/success/primary`; `link` относится к rich-кнопкам и только к `callback_data`.
- `callback_data` — строка 1–64 **UTF-8 байта**: 16 emoji по 4 байта проходят, 17 — нет.
- `url` кнопки — HTTP(S) или `tg://`; `javascript:`, `data:`, `file:` отклоняются. `login_url`/`web_app` требуют HTTPS.
- `callback_game` и `pay` обязаны быть первой кнопкой первой строки. `pay` дополнительно отклоняется, потому что валидатор не покрывает invoice-методы.
- Reply-клавиатура: строки непустые, кнопка — непустая строка или объект с `text`, не более одного `request_*`/`web_app` действия. `ReplyKeyboardRemove`/`ForceReply` требуют `true`, placeholder 1–64.
- `editMessageText`/`editEphemeralMessageText` принимают только `InlineKeyboardMarkup`; reply-клавиатура и force-reply отклоняются.
- Если в payload есть `business_connection_id`, отклоняются reply-клавиатуры, `web_app` и switch-inline кнопки. Для ephemeral-контекста отклоняются `login_url` и `web_app`, а обычный callback остаётся допустимым.
- Тип чата, права бота, доступность Web App домена и private-only ограничения по самому JSON не определяются. Для `web_app` без явного ephemeral/business-контекста выводится `chat_context_unverified`.

## ReplyParameters

`reply_parameters` проверяется по Bot API 10.3, но не отправляет reply. Обязателен ровно один target: `message_id` или `ephemeral_message_id`; оба сразу или ни одного — ошибка. `chat_id` принимает integer или `@username` (с тем же локальным numeric-string исключением), но не поддерживается для ephemeral, business-сообщений и direct messages topics. `allow_sending_without_reply` — boolean; `true` несовместим с reply в другой чат и с ephemeral.

- `quote` — строка 0–1024 символов после разбора entities; `quote_parse_mode` и `quote_entities` взаимоисключающие и требуют `quote`.
- `quote_entities` переиспользует проверку `MessageEntity` (UTF-16 offsets, типы, вложенность).
- `quote_position` — целое ≥0 в UTF-16 code units; `checklist_task_id` — positive integer; `poll_option_id` — непустая строка.
- `ephemeral_message_id` требует, чтобы send-запрос сам был ephemeral (`ephemeral_message_parameters.receiver_user_id`); reply на ephemeral всегда ephemeral. `allow_sending_without_reply=true` для такого reply — ошибка. `quote` для ephemeral документирован как ignored, поэтому его длина там не отклоняется.
- Неизвестные поля внутри `reply_parameters` — ошибка снимка 10.3.

## Ephemeral: send против edit

В 10.3 исходящая ephemeral-настройка в `sendMessage`/`sendRichMessage` вложена:

```json
{
  "chat_id": -1001234567890,
  "text": "hello",
  "ephemeral_message_parameters": {
    "receiver_user_id": 123456789,
    "callback_query_id": "cbq"
  }
}
```

Верхнеуровневые `receiver_user_id`, `callback_query_id`, `replace_callback_query_message` у send-методов — ошибка `legacy_ephemeral_field` с указанием на 10.3. В `editEphemeralMessageText`, наоборот, обязательны верхнеуровневые `chat_id`, `receiver_user_id`, `ephemeral_message_id`; `ephemeral_message_parameters` там — ошибка `ephemeral_shape_mismatch`.

Если у send-ephemeral нет ни `callback_query_id`, ни `reply_parameters.ephemeral_message_id`, выводится warning: без live-прав администратора такое сообщение может не отправиться, а payload этого не доказывает. Обратно: `reply_parameters.ephemeral_message_id` без вложенного `ephemeral_message_parameters.receiver_user_id` — ошибка, потому что reply на ephemeral обязан сам быть ephemeral.

## `answerCallbackQuery`

`callback_query_id` обязателен и непуст. `text` — строка 0–200 символов (пустая допустима). `show_alert` — строго boolean; `cache_time` — целое ≥0; `url` — HTTP(S) или `tg://`. Для URL не на `t.me`/`telegram.me` выводится warning, потому что произвольный URL работает только для game-callback, а происхождение callback по payload не установить.

## Ограничения

Валидатор **не** проверяет и не обещает:

- реальный ответ Telegram, права бота, тип чата, участие пользователя, бизнес-подключение, доступность URL/медиа, лимиты частоты;
- серверный HTML/Markdown-парсер и точное число символов после него; спорные места помечены warning;
- полную схему rich Markdown, всех `InputRichBlock` и всех `InputMedia`-вариантов;
- точное совпадение `ReplyParameters.quote` с исходным сообщением, существование `message_id`/`poll_option_id` и live-права на reply; проверяются структура, типы и видимые ограничения;
- поля вне снимка Bot API 10.3. Неизвестное поле — ошибка `unknown_field`, а не молчаливое игнорирование. Для нового API сначала обнови `METHOD_FIELDS`/вложенные allowlist и тесты, иначе валидатор будет отставать;
- методы отправки медиа, платежей, игр, draft, бизнес-методы, `sendDocument` и т. п.;
- секреты и содержимое payload в сообщениях об ошибках. CLI также не печатает payload. Имена anchor и entity не интерполируются в findings.

Валидатор не вводит недокументированный лимит числа entities или media-записей. Вложенность entities обрабатывается сортировкой и sweep за O(n log n), duplicate media ids — через set; размер вывода findings ограничен 200, но при отбрасывании ошибки exit code остаётся 1. Это локальная граница вывода, а не утверждение о серверном лимите.

Локальный успех не заменяет тестовый запрос в разрешённом тестовом чате и проверку фактического клиентского рендера. При расхождении официального контракта и этого файла приоритет у официального контракта; обнови снимок и тесты.

## Поддержка

- `scripts/telegram_payload.py` — реализация и CLI.
- `tests/test_payload.py` — unittest-тесты поведения: границы, UTF-8/UTF-16, рекурсивная Unicode-валидность, Markdown code spans, HTML-экранирование, rich 12000, content model/media, таблицы, `ReplyParameters`, клавиатуры, method casing, findings cap, ephemeral split, CLI-коды выхода, отсутствие утечки payload и anchor/entity имён.
- `assets/payloads/*.json` — валидные конверты для примеров и интеграционных проверок.

Проверка запускается из корня пакета обычной командой `python -m unittest discover -s tests -v`; живые вызовы Telegram для неё не нужны. Перед релизом полезно отдельно прогнать CLI на сырых payload (поле `payload` можно извлечь из конвертов `assets/payloads/*.json`).
