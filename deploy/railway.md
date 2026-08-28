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

Обязательно ограничь число потоков в сервисе **bot** (переменная провайдера,
см. ниже): в контейнере Ollama видит все ядра хоста и запускает поток на каждое,
хотя cgroup выделяет вчетверо меньше. На Railway это разница в разы.

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
                  --set 'OLLAMA_NUM_THREAD=8' \
                  --set 'OLLAMA_THINK=false' \
                  --set 'OLLAMA_NUM_CTX=8192' \
                  --set 'LLM_TIMEOUT=180' \
                  --set 'TELEGRAM_TRANSPORT=polling' \
                  --set 'ALLOWED_USER_IDS=<твой user_id>' \
                  --set 'AGENT_MAX_STEPS=6' \
                  --set 'AGENT_TASK_TIMEOUT=600' \
                  --set 'EXEC_TIMEOUT=30'
railway up --service bot
```

`.env` на Railway не нужен и не используется: переменные окружения приоритетнее файла,
это заложено в `bot/config.py`. Обратная сторона — **незаданная переменная берёт
значение по умолчанию из кода**. Для `OLLAMA_URL` это `http://localhost:11434`,
то есть сам контейнер бота, и в логах будет `сервис недоступен ([Errno 111]
Connection refused)`. Проверить, что реально проставилось: `railway variables --service bot`.

### Переменные агента

| Переменная | Зачем на Railway |
|---|---|
| `ALLOWED_USER_IDS` | **Обязательно.** У агента есть `exec`: без whitelist команды в контейнере выполнит любой, кто нашёл бота |
| `AGENT_MAX_STEPS=6` | CPU на Railway медленный; шесть шагов рутине хватает, а расход ограничивает |
| `AGENT_TASK_TIMEOUT=600` | Бюджет на весь запрос: несколько шагов по `LLM_TIMEOUT` каждый |
| `EXEC_TIMEOUT=30` | Сеть из контейнера медленнее локальной, `curl` иногда не успевает за 20 с |
| `EXEC_ALLOWED_BINARIES` | Строгий режим, если бот доступен не только тебе: `curl,cat,ls,date,head,grep,wc` |
| `OLLAMA_THINK=false` | Размышления qwen3 на общем CPU — основная трата времени: 150 токенов там, где хватает 5 |
| `OLLAMA_NUM_CTX=8192` | Ollama по умолчанию берёт 4096, и на финальном шаге контекст перестаёт влезать |

Скиллы и `data/` уезжают внутрь образа (`COPY` в `Dockerfile`), отдельный volume
им не нужен. Правка скилла = передеплой `railway up --service bot`. `curl` в образ
добавлен специально: без него скиллы, ходящие к HTTP API, не работают.

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

**Забыли `OLLAMA_URL`.** Симптом — `сервис недоступен по адресу
http://localhost:11434 ([Errno 111] Connection refused)`: бот стучится сам в себя.
Лечится одной командой:

```bash
railway variables --service bot --set 'OLLAMA_URL=http://ollama.railway.internal:11434'
railway up --service bot
```

**Пустой ответ на последнем шаге.** Симптом — агент честно отработал скилл и
все команды, а в чат прилетело `модель вернула пустой ответ`. Reasoning-модель
пишет размышления в отдельное поле ответа, и если бюджет генерации кончился
раньше, чем она перешла к тексту, `content` приходит пустым. Лечится
`OLLAMA_THINK=false` и `OLLAMA_NUM_CTX=8192`.

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
