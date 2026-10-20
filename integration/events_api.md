# Kago Analytics — API приёма событий (для бота)

Бот отправляет события на аналитику POST-запросами. Аналитика складывает их в свою базу и считает по ним метрики. Читать БД бота не нужно.

- **Адрес:** `POST https://185.23.19.127/api/ingest` (после появления домена — он)
- **Авторизация:** заголовок `Authorization: Bearer <токен>`. Токен выпускается на сайте: *Интеграции → 2. Приём событий от бота → Выпустить токен*. Показывается один раз; потерян — перевыпустите (старый сразу перестанет работать).
- **Формат:** JSON. Одно событие, массив событий или `{"events": [...]}`. До **500 событий** и **1 МБ** на запрос.
- Сейчас сертификат самоподписанный. До появления домена лучше закрепить его в клиенте бота: скопируйте `/etc/kago/tls/selfsigned.crt` с сервера панели и передайте путь в `verify=` (в модулях — переменная `KAGO_CA_FILE`). Крайний вариант — отключить проверку (`verify=False`, `KAGO_INSECURE=1`).

## Конверт события

```json
{
  "id": "pay-8f3c…",
  "type": "payment.completed",
  "ts": "2026-10-05T14:03:11+00:00",
  "data": { }
}
```

| Поле | Описание |
|---|---|
| `id` | **Уникальный id события** (строка до 128 символов). По нему работает идемпотентность: повторная отправка того же `id` безопасна и не задваивает данные. Удобно брать `<тип>:<id объекта>:<версия>`, например `payment:<payment_id>` или `sub:<user_id>:<updated_at>`. |
| `type` | Тип события (таблица ниже). Неизвестный тип принимается и игнорируется — можно добавлять новые события заранее. |
| `ts` | Когда событие произошло в боте, **ISO 8601 с часовым поясом**. Не из будущего (допуск +24 ч). |
| `data` | Данные события. |

`user_id` везде — **id пользователя в БД бота** (`users.id`), числом или строкой.

## Типы событий

### `user.registered` — пользователь зарегистрировался

| Поле | Обязательно | Описание |
|---|---|---|
| `user_id` | да | |
| `country` | нет | Двухбуквенный код страны (`RU`). Нужен для блока «География». Если у бота страны нет — не присылайте. |
| `language` | нет | `ru`, `en`… |
| `source` | нет | Рекламная ссылка/источник (`ad_links.name`). По нему партнёру можно привязать рекламную ссылку. |
| `referrer_id` | нет | `users.id` пригласившего (из таблицы `referrals`). **Нужен для кабинетов партнёров**: по нему считаются приведённые пользователи. |
| `telegram_id` | нет | Telegram ID — для поиска пользователя в панели. |
| `username` | нет | Telegram @username без `@`. |
| `name` | нет | Имя пользователя. |

`ts` = время регистрации (`users.created_at`). Повторная отправка не «омолаживает» пользователя — сохраняется самая ранняя дата. Первые полученные `referrer_id` и `source` закрепляются: атрибуция к партнёру потом не меняется. Профильные поля (`name`, `username`, `country`, `language`) обновляются последним непустым значением.

### `referral.attached` — пользователь привязан к пригласившему

`{"user_id": …, "referrer_id": …}` — если в боте реферал привязывается отдельно от регистрации. Это то же самое, что `referrer_id` в `user.registered`, порядок событий не важен.

### `user.updated` — изменился профиль

Те же поля, что у `user.registered`, кроме даты регистрации: событие её не задаёт. Удобно при смене `username` и имени.

### `payment.completed` — оплата прошла

| Поле | Обязательно | Описание |
|---|---|---|
| `payment_id` | да | `transactions.payment_id` |
| `user_id` | да | |
| `amount` | да | Итоговая сумма к оплате (`pricing.final_amount`), число ≥ 0. |
| `currency` | да | `RUB`, `USD`, `XTR` … |
| `purchase_type` | нет | `NEW` / `RENEW` / `CHANGE` |
| `plan` | нет | Название тарифа |
| `gateway` | нет | Платёжка |
| `is_test` | нет | `true` для тестовых платежей — в выручку не попадут |

Бесплатные (`amount: 0`) принимаются, но в выручку и «плательщики» не входят.

### `payment.refunded` — возврат

`{"payment_id": "…"}` — присылайте **после** `payment.completed` этого платежа. Возвращённый платёж выходит из выручки.

### `subscription.updated` — подписка создана или изменилась

Присылайте при **любом** изменении: покупка, продление, смена статуса, истечение, блокировка. Сохраняется **последнее состояние** пользователя.

| Поле | Обязательно | Описание |
|---|---|---|
| `user_id` | да | |
| `status` | да | `ACTIVE`, `DISABLED`, `LIMITED`, `EXPIRED`, `DELETED` |
| `expire_at` | да | Когда истекает, ISO 8601 |
| `is_trial` | нет | Пробная подписка |
| `created_at` | нет | Когда подписка появилась у пользователя. Нужен при выгрузке истории — иначе считается временем события. |

События с более старым `ts`, чем уже применённое, **не затирают** свежее состояние — порядок доставки не важен. Если хотя бы одно событие пришло с `is_trial: true`, пользователь считается взявшим триал (дата — `created_at`), даже когда его подписка потом сменится на платную.

### `device.added` — устройство пользователя

| Поле | Обязательно | Описание |
|---|---|---|
| `user_id` | да | |
| `platform` | да | `iOS`, `Android`, `Windows`, `macOS`, `Linux` … (как хотите видеть на графике) |
| `device_id` | нет | Стабильный id устройства (HWID). Без него одна платформа = одно устройство на пользователя. |

Нужно для блока «Платформы». Если устройства хранит только панель Remnawave — пришлите их в этом формате при синхронизации или напишите: возьмём из панели.

## Ответ

```json
{ "accepted": 28, "duplicates": 2, "ignored": 1, "errors": [ { "index": 4, "id": "b5", "status": "error", "error": "data.country: country — двухбуквенный код (RU)" } ] }
```

- `200` — запрос разобран; смотрите `errors`: невалидные события отклоняются **по отдельности**, остальные принимаются.
- `400` — не JSON / пусто / больше 500 событий. `401` — нет или неверный токен. `413` — больше 1 МБ.
- **Ретраи:** при сетевой ошибке, `5xx` или `429` повторяйте тот же запрос с теми же `id` — дублей не будет. `4xx` и `errors` повторять бессмысленно — сначала исправьте событие.

## Пример для бота (Python, httpx)

Отправка не должна тормозить обработку пользователя: кладём события в очередь и отправляем пачками из фоновой задачи.

```python
import asyncio, httpx
from datetime import datetime, timezone

ANALYTICS_URL = "https://185.23.19.127/api/ingest"
ANALYTICS_TOKEN = "kago_..."          # из .env бота

_queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=10_000)

def emit(type_: str, event_id: str, data: dict, ts: datetime | None = None) -> None:
    """Вызывать из бизнес-логики: не блокирует и не бросает исключений."""
    try:
        _queue.put_nowait({
            "id": event_id, "type": type_, "data": data,
            "ts": (ts or datetime.now(timezone.utc)).isoformat(),
        })
    except asyncio.QueueFull:
        pass  # аналитика не должна ронять бота

async def sender() -> None:
    """Запустить один раз при старте бота: asyncio.create_task(sender())."""
    async with httpx.AsyncClient(verify=False, timeout=10,   # verify=False — пока самоподписанный сертификат
                                 headers={"Authorization": f"Bearer {ANALYTICS_TOKEN}"}) as client:
        while True:
            batch = [await _queue.get()]
            await asyncio.sleep(1)                       # копим пачку
            while not _queue.empty() and len(batch) < 500:
                batch.append(_queue.get_nowait())
            for attempt in range(6):
                try:
                    r = await client.post(ANALYTICS_URL, json={"events": batch})
                    if r.status_code < 500 and r.status_code != 429:
                        break                            # 2xx/4xx — повторять нет смысла
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(2 ** attempt)        # 1, 2, 4, 8… секунд

# Примеры вызовов:
# emit("user.registered", f"reg:{user.id}",
#      {"user_id": user.id, "language": user.language.lower(), "telegram_id": user.telegram_id,
#       "username": user.username, "name": user.name,
#       "referrer_id": referrer.id if referrer else None,          # кто пригласил
#       "source": ad_link.name if ad_link else None}, user.created_at)
# emit("referral.attached", f"ref:{referred.id}", {"user_id": referred.id, "referrer_id": referrer.id})
# emit("payment.completed", f"payment:{tx.payment_id}",
#      {"payment_id": str(tx.payment_id), "user_id": tx.user_id,
#       "amount": float(tx.pricing.final_amount), "currency": tx.currency.value,
#       "purchase_type": tx.purchase_type.value, "gateway": tx.gateway_type.value, "is_test": tx.is_test})
# emit("subscription.updated", f"sub:{sub.user_id}:{sub.updated_at.timestamp()}",
#      {"user_id": sub.user_id, "status": sub.status.value, "expire_at": sub.expire_at.isoformat(), "is_trial": sub.is_trial})
```

Где вызывать `emit` (по структуре репозитория; точные места вам виднее):
- регистрация — `application/use_cases/user/commands/registration.py`;
- оплата и возврат — `application/use_cases/gateways/commands/payment.py` (там, где транзакция становится `COMPLETED`/`REFUNDED`);
- подписки — места, где создаётся/продлевается/меняет статус подписка, и `use_cases/remnawave/commands/synchronization.py` (синхронизация со статусами панели).

Очередь в памяти теряется при перезапуске бота. Если события критичны, храните их в Redis/БД бота (outbox) — формат запроса тот же.

## Выгрузка истории (backfill)

Чтобы на дашборде сразу были исторические цифры, один раз пройдитесь по таблицам бота и отправьте события с **историческими `ts`**: `user.registered` (`ts = users.created_at`), `payment.completed` (`ts = transactions.created_at`), `subscription.updated` (`ts` — сейчас, `created_at = subscriptions.created_at`), `device.added`. Пачками по 500. Дубли безопасны — можно запускать повторно.

Для Remnashop это делает готовый скрипт `remnashop_sync.py`. Он запускается на сервере бота и читает его PostgreSQL:

```bash
pip install "psycopg[binary]" httpx
export KAGO_URL=https://185.23.19.127/api/ingest KAGO_TOKEN=kago_... KAGO_CA_FILE=kago.crt
export REMNASHOP_DSN=postgresql://remnashop:<пароль>@127.0.0.1:5001/remnashop
python remnashop_sync.py            # разовая выгрузка всей истории
python remnashop_sync.py --watch 60 # и дальше досылать изменения раз в минуту
```

В режиме `--watch` скрипт отправляет только записи, изменившиеся с прошлого прохода (по `updated_at`). Так интеграция работает без правок в коде бота, с задержкой до N секунд.

## Проверка

```bash
curl -k -X POST https://185.23.19.127/api/ingest \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"id":"test-1","type":"user.registered","ts":"2026-10-05T12:00:00+00:00","data":{"user_id":1,"country":"RU"}}'
# {"accepted":1,"duplicates":0,"ignored":0,"errors":[]}
```

Счётчик принятых событий и время последнего видны на сайте: *Интеграции → 2. Приём событий от бота*.
