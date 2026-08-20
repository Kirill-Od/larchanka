# Деплой на сервер

Подробности вариантов и требований — в [PLAN.md](../PLAN.md), §5.

## Сценарий B: Docker Compose (рекомендуется)

Требования к VPS: 4 GB RAM для `qwen3:1.7b` (2 GB хватит для `tinyllama`), ~2 GB диска, Docker с плагином Compose.

```bash
# 1. Код на сервер
git clone <repo> /opt/homework-bot && cd /opt/homework-bot

# 2. Секреты. Файл создаётся вручную и в git не попадает
cp .env.example .env
nano .env                 # вписать TELEGRAM_BOT_TOKEN
chmod 600 .env

# 3. Запуск. OLLAMA_URL внутри compose проставляется автоматически
docker compose up -d --build

# 4. Один раз скачать модель внутрь volume ollama_data
docker compose exec ollama ollama pull qwen3:1.7b

# 5. Проверить
docker compose logs -f bot
```

Ожидаемая строка в логах: `Бот @<имя> запущен`.

### Эксплуатация

```bash
docker compose logs -f bot          # логи
docker compose restart bot          # перезапуск
docker compose down                 # остановка (модель в volume сохраняется)
git pull && docker compose up -d --build   # обновление
```

`restart: unless-stopped` поднимает бота после перезагрузки сервера.

## Сценарий C: systemd без Docker

```bash
sudo useradd --system --home /opt/homework-bot homework-bot
sudo git clone <repo> /opt/homework-bot
sudo chown -R homework-bot: /opt/homework-bot

# Секреты отдельно от репозитория
sudo cp /opt/homework-bot/.env.example /etc/homework-bot.env
sudo nano /etc/homework-bot.env
sudo chmod 600 /etc/homework-bot.env
sudo chown homework-bot: /etc/homework-bot.env

sudo cp /opt/homework-bot/deploy/homework-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now homework-bot
journalctl -u homework-bot -f
```

Ollama ставится отдельно (`curl -fsSL https://ollama.com/install.sh | sh`), работает как свой systemd-сервис на `localhost:11434`.

## Сценарий D: Ollama на отдельной машине

Если у VPS мало RAM, оставь на нём только бота, а модель держи на машине помощнее:

```
OLLAMA_URL=http://<gpu-host>:11434
```

**Обязательно** закрой Ollama файрволом или VPN — у неё нет аутентификации, и открытый в интернет порт означает, что твоей моделью пользуются все желающие.

## Транспорт webhook

По умолчанию используется long polling — ему не нужен ни домен, ни TLS. Webhook
имеет смысл, когда важен мгновенный отклик и меньше холостых запросов к API.

Telegram принимает только HTTPS, поэтому TLS терминирует обратный прокси, а бот
слушает локальный порт.

```bash
# .env
TELEGRAM_TRANSPORT=webhook
WEBHOOK_URL=https://bot.example.com
WEBHOOK_PORT=8080
WEBHOOK_PATH=telegram
WEBHOOK_SECRET_TOKEN=<длинная случайная строка>   # openssl rand -hex 32
```

В `docker-compose.yml` раскомментируйте проброс порта на loopback
(`127.0.0.1:8080:8080`) — наружу бот смотреть не должен.

Caddy (сам получит сертификат Let's Encrypt):

```
bot.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

Или nginx:

```nginx
server {
    listen 443 ssl;
    server_name bot.example.com;
    ssl_certificate     /etc/letsencrypt/live/bot.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/bot.example.com/privkey.pem;

    location /telegram {
        proxy_pass http://127.0.0.1:8080;
        proxy_set_header X-Telegram-Bot-Api-Secret-Token $http_x_telegram_bot_api_secret_token;
    }
}
```

Бот сам вызывает `setWebhook` при старте и `deleteWebhook` при остановке.
Проверить состояние: `curl "https://api.telegram.org/bot<TOKEN>/getWebhookInfo"`.

**`WEBHOOK_SECRET_TOKEN` обязателен.** Без него любой, кто узнает адрес, сможет
слать боту поддельные апдейты.

## Частые проблемы

| Симптом | Причина и решение |
|---|---|
| `Конфликт getUpdates` в логах | С тем же токеном запущена вторая копия бота (например, локальная) — останови её |
| `модель ... не найдена` | Не выполнен `ollama pull` — см. шаг 4 |
| `модель не ответила за N с` | Слабый CPU: увеличь `LLM_TIMEOUT` или перейди на `tinyllama` |
| Контейнер `ollama` уходит в unhealthy | Не хватает RAM — проверь `free -h` и `docker stats` |
| `TELEGRAM_BOT_TOKEN не задан` | Нет `.env` рядом с `docker-compose.yml` или переменная пустая |
| `Транспорт webhook не сконфигурирован` | Не задан `WEBHOOK_URL` или он не на `https://` |
| Webhook: апдейты не приходят | `getWebhookInfo` покажет `last_error_message`; частые причины — сертификат и неверный путь в прокси |
| В логах `Отклонён запрос с неверным secret token` | Прокси не пробрасывает заголовок `X-Telegram-Bot-Api-Secret-Token` |
| `процесс агента завис и был перезапущен` | Модель не уложилась в `LLM_TIMEOUT` + 15 с; проверь нагрузку и размер модели |
