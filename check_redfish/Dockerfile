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
# ilorest — HPE REST CLI: используется режимом `api: ilorest` для серверов,
# у которых Redfish недоступен (iLO4 firmware 2.8x отдаёт HTML вместо
# /redfish/v1/*). Ставится сразу, чтобы в рантайме не зависеть от доступа к PyPI.
RUN pip install --upgrade pip \
    && pip install "git+https://github.com/bb-Ricardo/check_redfish" \
    && pip install pyyaml \
    && pip install ilorest || echo "[Dockerfile] WARNING: pip install ilorest не удался — режим api: ilorest будет ставить его автоматически при первом запуске (нужен доступ к PyPI из контейнера)"

# Рабочая директория приложения
WORKDIR /app

# Скрипт-обёртка, парсер списка серверов и патчи совместимости логина:
#   cr_login_redirect_patch — 3xx-редирект на POST Sessions (Unsafe redirect / 308)
#   cr_basic_auth_patch     — чистый Basic-auth для BMC с 308-само-редиректом
COPY entrypoint.sh /app/entrypoint.sh
COPY servers_parser.py /app/servers_parser.py
COPY cr_login_redirect_patch.py /app/cr_login_redirect_patch.py
COPY cr_basic_auth_patch.py /app/cr_basic_auth_patch.py
COPY cr_ilorest_patch.py /app/cr_ilorest_patch.py
RUN chmod +x /app/entrypoint.sh

# Каталог модулей PYTHONPATH для collect-а (патч подхватывается через -m)
ENV PYTHONPATH=/app

# Каталог для JSON-результатов (перекрывается volume inventory-data)
RUN mkdir -p /app/inventory /app/status

# Циклический запуск сбора инвентаря по расписанию CHECK_INTERVAL
ENTRYPOINT ["/app/entrypoint.sh"]
