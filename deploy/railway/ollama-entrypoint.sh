#!/bin/sh
# Старт Ollama на Railway: поднимает сервер и один раз докачивает модель в volume.
set -e

# Приватная сеть Railway работает по IPv6, поэтому слушать надо на [::],
# иначе сервис-бот не достучится до ollama.railway.internal.
: "${OLLAMA_HOST:=[::]:11434}"
: "${PULL_MODEL:=qwen3:1.7b}"
export OLLAMA_HOST

echo "Запускаю Ollama на ${OLLAMA_HOST}"
ollama serve &
SERVE_PID=$!

attempt=0
until ollama list >/dev/null 2>&1; do
    attempt=$((attempt + 1))
    if [ "$attempt" -gt 60 ]; then
        echo "Ollama не поднялась за 60 секунд" >&2
        exit 1
    fi
    sleep 1
done

# Модель лежит в volume, поэтому качается только при первом запуске.
if ollama list | awk 'NR > 1 {print $1}' | grep -qx "${PULL_MODEL}"; then
    echo "Модель ${PULL_MODEL} уже есть в volume"
else
    echo "Качаю модель ${PULL_MODEL} (это долго только в первый раз)"
    ollama pull "${PULL_MODEL}"
fi

echo "Ollama готова"
wait "${SERVE_PID}"
