# Бэкенд: простые границы, надёжные операции

## Размер архитектуры

Сохрани текущий стек. Если проекта нет: один модульный сервис, aiogram 3 для Python либо поддерживаемый SDK проекта; бизнес-операции не зависят от Telegram types. Бэкенд — бизнес-логика и хранение, не обязательно REST-сервер. FastAPI/другой HTTP framework нужен при внешнем API, веб-админке, Mini App либо выбранной схеме webhook; не добавляй два сервера для одного endpoint.

Пример ответственности каталогов, а не обязательный scaffold:

```text
app/
  config         # проверенный конфиг, env secrets
  bot/           # routers, filters, middleware, keyboards, renderers, FSM
  services/      # use cases, permission decisions, transitions
  db/            # models, repositories, transactions
  jobs/          # только требуемые reminders/outbox/long tasks
  web/           # только требуемые webhook/API/admin
migrations/
tests/
```

Handler извлекает контекст и вызывает use case; service проверяет права/инварианты и изменяет данные в транзакции; renderer строит текст/кнопки. Не помещай SQL, платёжную логику и выдачу ролей в один callback handler.

## Выбор хранения

| Условия | Выбор |
|---|---|
| Маленький один процесс, низкая конкуренция | SQLite на постоянном томе с транзакциями и проверенными ограничениями; не файл в ephemeral filesystem |
| Несколько процессов/реплик, записи/баланс/конкурентные операции | PostgreSQL и миграции; интеграционную проверку гонок делай на нём, не подменяй SQLite |
| Шаги диалога теряют смысл после рестарта | MemoryStorage допустим только при явной договорённости; пользователь получает восстановление |
| Черновик должен пережить рестарт | Durable FSM storage или таблица conversation drafts; Redis с persistence/TTL, если он выбран, а не автоматически |
| Напоминания/рассылки/долгая интеграция | Устойчивые задания/outbox + worker, только если нужны продукту |

FSM хранит текущий диалог; заказы, права, настройки и итоговые операции живут в основной БД. Redis без persistence/правильной политики eviction сам по себе не гарантирует сохранность. Установи scope FSM с учётом bot, user, chat и thread; event isolation помогает одному диалогу, но не заменяет DB-защиту при разных клиентах/операторах.

## Данные и инварианты

Используй миграции, не только `create_all` на старте. Telegram user/chat id — BIGINT/64-bit; username необязателен и изменяем. Отрицательные chat id не теряй. В мультиботе ключи и dedup включают bot_id; для организаций — tenant_id. Время хранится в UTC, отображается в выбранной IANA timezone; для календарей проверь DST и границы суток.

Выбирай только необходимые таблицы:
- `users`: telegram_user_id, доступ/blocked, locale и timestamps; профиль не источник ролей.
- `role_bindings`: actor id, role/permission, scope/tenant/chat, кто и когда назначил.
- Доменный объект: владелец/назначенный оператор, status, version, timestamps, нужные поля и ограничения.
- `settings`: типизированные значения/область/version; секреты не в открытом JSON.
- `audit_events`: actor, action, object, результат, безопасный before/after, время/correlation.
- `inbox_updates`/`processed_updates`: уникальный `(bot_id, update_id)`, состояния/время/данные восстановления по выбранному протоколу.
- `outbox`/`jobs`: только при внешних side effects/заданиях с требованием восстановления.

Проверяй инварианты в БД: уникальная активная бронь/ограничение пересекающихся интервалов, неотрицательный остаток/сумма, уникальный provider charge id. Проверка SELECT → INSERT без constraint/lock не защищает от гонки.

## Доставка updates и повторы

Telegram может повторить webhook, клиент — нажать разные callbacks с одинаковым действием. Это разные уровни повторов:
1. `(bot_id, update_id)` защищает обработку доставки.
2. Версия/статус объекта, одноразовый action token или доменный idempotency key защищает смысл операции при новом update id.
3. Внешний provider idempotency key защищает оплату/заказ у провайдера, если поддерживается.

`update_id` принадлежит внешнему объекту **Update**, а не `CallbackQuery` или `Message`. Передай его из ingress/outer middleware в use case отдельно; в aiogram получай исходный update через контекст обработчика, не пиши `query.update_id`. Actor при этом берётся из callback sender. Проверь это реальным dispatcher smoke, иначе даже хороший алгоритм dedup упадёт до транзакции.

**Нельзя:** отдельно commit «update обработан», затем выполнить действие. При падении работа потеряется. Для быстрых DB-only операций запись dedup, изменение объекта и успешное завершение commit должны быть в **одной транзакции**. После rollback повтор выполняется заново. Если нужна предварительная inbox-запись: она означает `pending`, а не `done`; worker забирает её с recoverable lease/статусом, завершает вместе с доменным commit. Просроченный `processing` должен снова стать доступен.

Алгоритм подтверждения заявки (псевдокод; адаптируй к БД, не копируй как готовый SQL):

```text
on_callback(update):
  acknowledge_callback_quickly()           # не сообщение об успехе
  command = parse_allowlisted_callback()
  actor_id = update.callback_query.from.id
  BEGIN
    claim delivery with UNIQUE(bot_id, update_id)
      # insert в этой транзакции; конфликт committed done => уже выполнено
      # pending inbox => не бросать работу, claim lease/row lock по протоколу
    actor = load current access/role bindings
    request = load object with row lock (or prepare compare-and-swap)
    authorize(actor, command, request, current_assignment_and_tenant)
    verify server-issued context/version/expiry if relevant
    changed = UPDATE request
      SET status='confirmed', version=version+1
      WHERE id=:id AND version=:expected AND status='pending'
    if changed != 1:
      store safe stale/conflict outcome; mark delivery done
    else:
      append audit
      enqueue required outbound notification in same transaction
      mark delivery done
  COMMIT
  render current authorized object/result
```

Повторная команда с **другим** update id также не должна второй раз списать деньги/резервировать слот: проверка состояния/constraint обязательна. Права и назначение могут быть отозваны между чтением и commit: для чувствительных операций согласуй блокировки/версионирование authorization scope с конкурентной выдачей/отзывом ролей; не используй бесконечный кэш permission.

## Исходящие side effects

DB commit и Telegram `sendMessage` не атомарны. Если требуется гарантированно восстановить уведомление после commit, используй transactional outbox: worker забирает задание, делает отправку, сохраняет outcome; повторы учитывают 429/retry_after. Между отправкой и записью результата возможен crash и дубликат. Telegram sendMessage не имеет общего idempotency key: **exactly-once доставку не обещай**. Для критичного результата источник истины — БД и экран статуса; в тексте используй стабильный номер операции. Не держи транзакцию открытой во время медленного сетевого вызова.

При неоднозначном сетевом timeout сначала учитывай, что side effect мог выполниться. Повторять платёж/создание внешнего заказа вслепую нельзя; нужна сверка или provider idempotency. Для 400 исправляй вход/разметку, для 403 blocked отмечай недоступность, для 429 учитывай retry_after, для временных ошибок — ограниченная политика по операции. Не оборачивай всё в бесконечный retry или `except: pass`.

## Ввод и ресурсы

Проверяй размер/тип файлов и текста, MIME, допустимые значения, даты, валюту и сумму на сервере. SQL параметризуй. Чужие URL/файлы не должны давать произвольный доступ к локальным/служебным адресам; действуй по требованию интеграции, не запускай содержимое загрузок. Async handlers не выполняют blocking HTTP/DB/CPU-код в event loop; закрывай sessions/connections на shutdown.

Оплата, если запрошена: проверь актуальные Stars/physical-goods правила, provider callback signature, сумму/валюту/объект, уникальность charge id; не подтверждай оплату по кнопке «Я оплатил» или redirect. Выдавай товар по проверенному successful_payment/подтверждённому событию провайдера, с аудитом и правилами возврата.

Источники: [Bot API updates](https://core.telegram.org/bots/api.md#getting-updates), [webhook](https://core.telegram.org/bots/api/methods/setwebhook.md), [aiogram FSM](https://docs.aiogram.dev/en/latest/dispatcher/finite_state_machine/index.html).
