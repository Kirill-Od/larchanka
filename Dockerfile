FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# curl нужен самому агенту: скиллы ходят им к HTTP API (wttr.in и прочие).
# ca-certificates — чтобы работал HTTPS. Больше в образе ничего лишнего.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Работаем не от root: инструмент exec выполняет команды с правами этого
# пользователя, и это главная линия защиты — sudo у него нет.
RUN useradd --create-home --uid 10001 botuser
WORKDIR /app

# Сторонних зависимостей нет — копируем только код, скиллы и демо-данные
COPY bot/ ./bot/
COPY skills/ ./skills/
COPY data/ ./data/

USER botuser
CMD ["python", "-m", "bot.main"]
