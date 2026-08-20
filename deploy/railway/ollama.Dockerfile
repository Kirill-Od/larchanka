# Сервис Ollama для Railway.
# Собственный образ нужен, чтобы подменить ENTRYPOINT: у ollama/ollama он
# указывает прямо на бинарник, и кастомную команду старта туда не подставить.
FROM ollama/ollama:latest

# IPv6 обязателен для приватной сети Railway
ENV OLLAMA_HOST="[::]:11434"
ENV PULL_MODEL="qwen3:1.7b"

COPY deploy/railway/ollama-entrypoint.sh /usr/local/bin/entrypoint.sh
RUN chmod +x /usr/local/bin/entrypoint.sh

ENTRYPOINT ["/bin/sh", "/usr/local/bin/entrypoint.sh"]
