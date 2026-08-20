# Деплой на Railway

Два сервиса в одном проекте: **bot** и **ollama**. Общаются по приватной сети Railway,
наружу не смотрит ни один.

```
Telegram API  ←→  bot (polling)  ──→  ollama.railway.internal:11434
                                        └── volume /root/.ollama (модель)
```

Транспорт — `polling`: публичный домен не нужен, входящих портов у бота нет.

## Что понадобится

- Аккаунт Railway с планом **Hobby** (нужны volume и ~4 GB RAM под модель).
- Railway CLI: `brew install railway`, затем `railway login`.
- Закоммиченный код (деплой идёт из текущего каталога через `railway up`).

## 1. Создать проект

```bash
railway init --name homework-bot
```

## 2. Сервис ollama

```bash
railway add --service ollama
railway link --service ollama
```

Переменные сервиса:

```bash
railway variables --set 'RAILWAY_DOCKERFILE_PATH=deploy/railway/ollama.Dockerfile' \
                  --set 'OLLAMA_HOST=[::]:11434' \
                  --set 'PULL_MODEL=qwen3:1.7b'
```

**Volume обязателен**, иначе модель будет качаться при каждом рестарте:
в дашборде сервиса → Data → Add Volume → mount path `/root/.ollama`, 5 GB.

Деплой:

```bash
railway up --service ollama
```

Первый запуск качает 1.4 GB — в логах будет `Качаю модель qwen3:1.7b`, затем `Ollama готова`.

## 3. Сервис bot

```bash
railway add --service bot
railway link --service bot
railway variables --set 'TELEGRAM_BOT_TOKEN=<токен от @BotFather>' \
                  --set 'OLLAMA_URL=http://ollama.railway.internal:11434' \
                  --set 'LLM_PROVIDER=ollama' \
                  --set 'OLLAMA_MODEL=qwen3:1.7b' \
                  --set 'LLM_TIMEOUT=180' \
                  --set 'TELEGRAM_TRANSPORT=polling'
railway up --service bot
```

`.env` на Railway не нужен и не используется: переменные окружения приоритетнее файла,
это заложено в `bot/config.py`.

Признак успеха в логах:

```
Бот @<имя> запущен
Провайдер ollama готов, модель qwen3:1.7b
Long polling запущен
```

## Подводные камни

**IPv6.** Приватная сеть Railway работает только по IPv6. Ollama, слушающая `0.0.0.0`,
из другого сервиса недоступна — отсюда `OLLAMA_HOST=[::]:11434` в образе. Симптом при
ошибке: бот бесконечно пишет `Провайдер ollama ещё не отвечает`.

**ENTRYPOINT.** У официального образа `ollama/ollama` ENTRYPOINT указывает прямо на
бинарник, поэтому «Custom Start Command» в Railway туда не подставляется. Для этого
собран свой образ из `deploy/railway/ollama.Dockerfile`.

**Останови локального бота.** Два процесса с одним токеном конфликтуют: Telegram отдаёт
апдейт только одному `getUpdates`. В логах это видно как `Конфликт getUpdates` (HTTP 409).

**Медленнее локального.** На общих CPU Railway ответ `qwen3:1.7b` занимает 10–30 секунд
против 2–4 на Apple Silicon. Отсюда `LLM_TIMEOUT=180`.

**Стоимость.** Тарификация по факту потребления. Ollama держит модель в памяти и выгружает
её через 5 минут простоя, так что основной расход — RAM во время ответов плюс volume.
Ориентир для круглосуточной работы: заметно больше $5 стартового кредита Hobby.
Чтобы не тратить впустую, останавливай сервисы, когда бот не нужен.

## Эксплуатация

```bash
railway logs --service bot          # логи бота
railway logs --service ollama       # логи модели
railway up --service bot            # передеплой после правок
railway variables --service bot     # посмотреть переменные
railway down                        # остановить
```

## Если модель не влезает

`qwen3:1.7b` требует ~4 GB RAM. Варианты, если упирается:

- `PULL_MODEL=tinyllama` и `OLLAMA_MODEL=tinyllama` — хватит 2 GB.
- Держать Ollama на своей машине или VPS с GPU, а на Railway оставить только бота:
  тогда `OLLAMA_URL` указывает наружу, и Ollama **обязательно** закрывается firewall
  или VPN — у неё нет аутентификации.
