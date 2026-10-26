# Галерея интерфейсов: как читать и открывать

`assets/gallery/index.html` — один офлайн-файл с интерактивными макетами Telegram-паттернов. Он помогает выбрать структуру экранов, увидеть границу ролей и объяснить переход, а не изображает настоящий клиент Telegram. Все данные, роли, версии, аудит и результаты иллюстративные. Страница не делает сетевых запросов, не содержит токенов и не проверяет авторизацию.

Снимок: **26.10.2026**, контекст **Bot API 10.3**. Перед реализацией сверяй [текущий контракт Bot API](https://core.telegram.org/bots/api.md) и changelog.

## Открыть локально

Зависимостей и сборки нет. Из корня пакета:

**Прямое открытие файла**

```powershell
# Windows PowerShell
Start-Process .\assets\gallery\index.html
```

```bat
:: Windows cmd
start "" "assets\gallery\index.html"
```

```bash
# macOS
open assets/gallery/index.html
# Linux
xdg-open assets/gallery/index.html
```

Прямой `file://` работает, потому что HTML не использует модули, `fetch` и внешние ресурсы. Некоторые браузеры ограничивают `localStorage` на `file://`; тогда переключатель темы не запомнится между перезагрузками. Для проверки в одинаковых условиях лучше локальный сервер.

**Локальный HTTP-сервер**

```bash
python -m http.server 8000 --bind 127.0.0.1
```

Если команда `python` недоступна, используй `python3`. Затем открой:

```text
http://127.0.0.1:8000/assets/gallery/index.html
```

Сервер читает только локальные файлы пакета. Порт и bind-адрес можно изменить; наружу сервер не публикуй.

## Что это и чего здесь нет

Галерея показывает:

- иерархию меню и gated-пункты управления;
- пошаговую форму с Back/Cancel и предпросмотром;
- список с фильтром, стабильными страницами и карточкой;
- личные и рабочие настройки с отдельным сохранением;
- очередь оператора с назначением и внутренней заметкой;
- границу ролей и подтверждение изменения роли;
- опасное действие с последствиями и tombstone;
- пустое состояние, ошибку загрузки и stale-кнопку;
- rich-пост с таблицей, details и callback-кнопками.

Галерея **не** показывает и не подтверждает:

- реальный вид Telegram на мобильном или десктопе;
- права бота в чате, серверный RBAC, ownership или аудит;
- доставку, платежи, сохранение настроек или удаление данных;
- работу с настоящими аккаунтами, токенами или API;
- скриншоты. Здесь нет растровых изображений и выдачи макета за клиент.

Верхний дисклеймер, подпись устройства и подвал — часть контракта. Не удаляй их, если файл используется как публичный референс.

## Управление и якоря

Сценарии доступны по hash-якорям и с клавиатуры:

```text
#menu #form #list #settings #queue #access #danger #states #rich
```

- Левая навигация — обычные ссылки. Активный сценарий получает `aria-current="page"`.
- Переключатель роли — нативные radio `input[name="role"]`; работают стрелки и Tab.
- `#theme-toggle` меняет светлую/тёмную тему; выбор хранится в `localStorage` под ключом `te-gallery-theme`.
- Кнопки ширины `[data-action="width"][data-value="normal|narrow"]` меняют ширину макета на десктопе (396/360 px). На узком экране макет занимает доступную ширину.
- `#live-status` — `role="status"`, сообщает о локальном действии. Это не серверный статус.
- `#chat-stream` имеет `data-scene` и `data-role`; удобно для стабильных проверок. `#specimen` имеет `data-width`.
- Enter в поле формы отправляет описание; Escape отменяет открытое подтверждение в сценариях «опасное действие» и «доступ», а также закрывает переназначение.
- В сценарии `#rich` блок `details` раскрывается нативно; CTA показывает локальную трассу callback, не отправляя её.

### Селекторы для smoke-проверки

| Действие | Селектор / атрибут |
|---|---|
| Открыть сценарий | `a[href="#form"]`, `#chat-stream[data-scene="form"]` |
| Выбрать роль | `input[name="role"][value="operator"]` |
| Тема и ширина | `#theme-toggle`, `[data-action="width"][data-value="narrow"]` |
| Меню | `[data-action="nav"][data-value="form|list|settings|queue|access|menu"]` |
| Форма | `[data-action="form-topic"]`, `#form-input`, `#form-composer`, `[data-action="form-confirm"]`, `[data-action="form-back"]`, `[data-action="form-cancel"]` |
| Список | `[data-action="list-filter"]`, `[data-action="list-open"]`, `[data-action="list-page"]`, `[data-action="list-back"]` |
| Настройки | `#set-personal-notify`, `#set-personal-digest`, `#set-personal-quiet`, `[data-action="settings-save"]`, `[data-action="settings-cancel"]` |
| Очередь | `[data-action="queue-take"]`, `[data-action="queue-close"]`, `[data-action="queue-return"]`, `[data-action="queue-reassign-open"]`, `[data-action="queue-reassign-apply"]` |
| Доступ | `#access-target`, `#access-role`, `[data-action="access-review"]`, `[data-action="access-confirm"]`, `[data-action="access-cancel"]` |
| Опасное действие | `[data-action="danger-start"]`, `[data-action="danger-confirm"]`, `[data-action="danger-cancel"]`, `[data-action="danger-reset"]` |
| Состояния | `[data-action="states-variant"][data-value="empty|error|stale"]`, `[data-action="states-retry"]`, `[data-action="states-confirm"]`, `[data-action="states-refresh"]` |
| Rich | `#rich-details`, `[data-action="rich-cta"]` |

Эти селекторы — часть референса. Если меняешь разметку, проверь, что сценарии остаются доступны с клавиатуры и что `data-scene`/`data-role` обновляются.

## Как выбирать паттерн

| Паттерн | Когда | Не использовать / следующий шаг |
|---|---|---|
| Главное меню | 3–6 разделов, есть одно основное действие | Не складывать в меню все команды; управление показывать по permission |
| Пошаговая форма | 2–4 смысловых шага, нужен предпросмотр | Для длинной формы/календаря рассмотреть Mini App; не терять ввод на Back |
| Список и карточка | Много объектов, фильтр, повторяющиеся действия | Для очереди нужен отдельный scope и assignee; пагинация — со стабильной сортировкой |
| Настройки | Есть реальные изменяемые значения | Не показывать пустые разделы; личные и рабочие значения разделять |
| Очередь оператора | Назначение, статусы, внутренние заметки | Не давать клиенту очередь; reassign — отдельное право |
| Доступ и роли | Смена ролей, scope и аудит | Не делать «первого пользователя владельцем»; owner — из конфигурации |
| Опасное подтверждение | Необратимое или дорогое действие | Не подтверждать каждый клик; показывать последствия, а не только цвет |
| Состояния | Пусто/ошибка/stale возможны в работе | Не выдавать 403 за ошибку загрузки; stale не повторять |
| Rich-пост | Иерархия, таблица, details, длинный материал | Обычное сообщение — для короткого статуса; rich HTML не произвольная веб-страница |

Галерея — не набор обязательных экранов. Если в продукте нет операторов, оплат или расписания, не добавляй их только ради полноты. Сначала brief из [discovery.md](discovery.md), затем выбранные рецепты.

## Точная грамматика, на которую опирается галерея

- **Обычное сообщение** — `sendMessage` с `text` и необязательным `parse_mode`/`reply_markup`. HTML поддерживает ограниченный набор тегов; `<h1>`, `<table>` и `<details>` в обычный `parse_mode=HTML` не входят. Смотри [formatting.md](formatting.md).
- **Rich-сообщение** — `sendRichMessage` с `rich_message`, внутри ровно один из `html`, `markdown` или `blocks`; без `text` и `parse_mode`. Таблицы, details и rich-кнопки проверяй по [rich-messages.md](https://core.telegram.org/bots/api/rich-messages.md) и [InputRichMessage](https://core.telegram.org/bots/api/types/inputrichmessage.md).
- **Обычная inline-кнопка** содержит одно действие помимо оформления. Для callback используй `callback_data`, не `url` рядом с ним. Лимит `callback_data` — 1–64 байта UTF-8. Кнопка не является полномочием: actor берётся из update, роль/объект проверяются на сервере. В Bot API 10.3 `InlineKeyboardButton.disabled` принимает объект `DisabledButton`; это отдельный тип кнопки без действия, а не boolean-флаг рядом с callback. Серверная авторизация обязательна и для старых активных кнопок. `tg-button type="disabled"` относится к отдельному rich-контракту.
- **Rich-кнопка** — отдельный тип `tg-button`; для callback используется `type="callback_data"` и атрибут `data`, а не `callback_data`. Схема rich-кнопки и стили отличаются от `InlineKeyboardButton`; сверяй [RichMessageButton](https://core.telegram.org/bots/api/types/richmessagebutton.md).
- **Ответ на callback** быстрый (`answerCallbackQuery`), но это лишь ACK нажатия, не подтверждение бизнес-операции. Для stale/запрещённой кнопки нужен понятный ответ и актуальный экран.
- **Персональный ответ в группе** — `ephemeral_message_parameters` (`receiver_user_id`, при необходимости `callback_query_id`), не top-level поля. Доставка не гарантируется; это не журнал подтверждений. Смотри [telegram-surfaces.md](telegram-surfaces.md).
- Callback, текст пользователя и `initData` недоверенные. В галерее ввод формы выводится через текстовое экранирование; это демонстрация, а не доказательство безопасности.

Галерея намеренно **не связана** с интерфейсом reference-бота из `examples/` и не импортирует его namespace. Payload-фрагменты используют нейтральные имена callback.

## Паттерн → проверка

При переносе макета в рабочий бот проверь:

1. Меню: скрытый пункт не вызывает операцию у роли без permission.
2. Форма: Back сохраняет ввод, Cancel не создаёт объект, повторное подтверждение не создаёт второй.
3. Список: чужие объекты не видны; новая страница читается с сервера, а не из старого индекса.
4. Настройки: сохранение защищено версией; рабочая область требует отдельного права.
5. Очередь: два оператора не становятся исполнителями одновременно; внутренняя заметка не уходит клиенту.
6. Доступ: изменение роли подтверждается, пишется в аудит, отзыв влияет на старые кнопки.
7. Опасное действие: отмена не меняет данные; повтор/истечение не удаляет второй раз.
8. Состояния: 403, 5xx и пустой список различаются; stale показывает конфликт.
9. Rich: таблица/details отправляются через rich-контракт; обычный HTML не притворяется rich; лимиты проверены.
10. Мобильная ширина 360 px, десктоп 1280 px, обе темы: нет горизонтального скролла страницы; фокус видим.

## Ограничения проверки

Макет можно открыть без сети и пройти все переходы. Это доказывает только локальное поведение HTML/JS. Он **не доказывает**:

- как Telegram отрисует таблицу, details, rich-кнопки и переносы;
- что бот имеет нужные права в конкретном чате;
- что серверная авторизация, транзакции и аудит реализованы;
- что платежи, приглашения или напоминания работают.

Визуальную проверку в реальном клиенте и разрешённом тестовом чате выполняет владелец бота; без токена честно указывай: **живой Telegram API и клиентский вид не проверены**.

## Официальные источники

- [Bot API](https://core.telegram.org/bots/api.md) · [changelog](https://core.telegram.org/bots/api-changelog.md)
- [Rich messages](https://core.telegram.org/bots/api/rich-messages.md) · [sendRichMessage](https://core.telegram.org/bots/api/methods/sendrichmessage.md) · [InputRichMessage](https://core.telegram.org/bots/api/types/inputrichmessage.md)
- [sendMessage](https://core.telegram.org/bots/api/methods/sendmessage.md) · [InlineKeyboardButton](https://core.telegram.org/bots/api/types/inlinekeyboardbutton.md)
- [EphemeralMessageParameters](https://core.telegram.org/bots/api/types/ephemeralmessageparameters.md)
- [features](https://core.telegram.org/bots/features.md) · [Mini Apps](https://core.telegram.org/bots/webapps.md)

Снимок проверен 26.10.2026. При расхождении официального контракта и этого текста приоритет у официального контракта и установленной версии SDK.
