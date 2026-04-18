FROM python:3.12-slim

# git нужен для git-backed memory
# bubblewrap — опциональный bash sandbox для worker-агентов
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        bubblewrap && \
    rm -rf /var/lib/apt/lists/*

# Создаём пользователя (UID/GID можно переопределить при сборке)
ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} botuser && \
    useradd -u ${UID} -g ${GID} -m botuser

WORKDIR /app

# Зависимости (без тестовых)
RUN pip install --no-cache-dir \
    claude-agent-sdk>=0.1.58 \
    python-telegram-bot>=21.0 \
    python-dotenv>=1.0.0 \
    pyyaml>=6.0

# Код
COPY --chown=botuser:botuser src/ src/
COPY --chown=botuser:botuser agents/ agents/

# Entrypoint: auto-detect Claude CLI из смонтированного volumes
COPY --chown=botuser:botuser entrypoint.sh /app/entrypoint.sh

# Работаем от обычного пользователя (не root)
USER botuser

# Claude CLI монтируется с хоста через docker-compose
# Память хранится в volume (не теряется при перезапуске)
VOLUME /app/agents/me/memory

ENTRYPOINT ["/app/entrypoint.sh"]
CMD ["python3", "-m", "src.main"]
