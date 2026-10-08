# =============================================================================
# Image для коллектора check_redfish:
#   python:3.9-slim + check_redfish (из GitHub) + pyyaml (парсинг servers.yaml)
# =============================================================================
FROM python:3.9-slim

# Только stdout/stderr в логи, без буферизации Python
ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

# Системные зависимости: ca-certificates (HTTPS к BMC), curl (healthcheck/отладка)
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

# check_redfish ставим напрямую из GitHub (последняя версия), pyyaml — для
# чтения списка серверов из servers.yaml
RUN pip install --upgrade pip \
    && pip install "git+https://github.com/bb-Ricardo/check_redfish" \
    && pip install pyyaml

# Рабочая директория приложения
WORKDIR /app

# Скрипт-обёртка, парсер списка серверов и патч совместимости логина (3xx redirect)
COPY entrypoint.sh /app/entrypoint.sh
COPY servers_parser.py /app/servers_parser.py
COPY cr_login_redirect_patch.py /app/cr_login_redirect_patch.py
RUN chmod +x /app/entrypoint.sh

# Каталог модулей PYTHONPATH для collect-а (патч подхватывается через -m)
ENV PYTHONPATH=/app

# Каталог для JSON-результатов (перекрывается volume inventory-data)
RUN mkdir -p /app/inventory /app/status

# Циклический запуск сбора инвентаря по расписанию CHECK_INTERVAL
ENTRYPOINT ["/app/entrypoint.sh"]
