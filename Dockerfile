FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# Работаем не от root
RUN useradd --create-home --uid 10001 botuser
WORKDIR /app

# Сторонних зависимостей нет — копируем только код
COPY bot/ ./bot/

USER botuser
CMD ["python", "-m", "bot.main"]
