#!/bin/sh
# =============================================================================
# entrypoint.sh коллектора check_redfish
#
# Логика:
#   1. Пауза STARTUP_DELAY секунд (по умолчанию 60) — чтобы не колотить в BMC
#      сразу при старте и дать docker-сетям подняться.
#   2. Цикл: servers_parser.py обходит все серверы из /app/servers.yaml,
#      запускает для каждого check_redfish и пишет /app/inventory/<name>.json.
#   3. В конце цикла обновляется heartbeat-файл /app/status/last_run
#      (используется healthcheck'ом из docker-compose).
#   4. Сон CHECK_INTERVAL секунд, затем шаг 2 повторяется. Список серверов
#      перечитывается каждый цикл — правки servers.yaml применяются без
#      перезапуска контейнера.
# =============================================================================
set -u

INVENTORY_DIR="${INVENTORY_DIR:-/app/inventory}"
STATUS_DIR="${STATUS_DIR:-/app/status}"
CHECK_INTERVAL="${CHECK_INTERVAL:-14400}"     # период полного прохода, сек
STARTUP_DELAY="${STARTUP_DELAY:-60}"          # задержка перед первым прогоном, сек

echo "[entrypoint] Старт коллектора check_redfish"
echo "[entrypoint]   inventory_dir=${INVENTORY_DIR}, interval=${CHECK_INTERVAL}s, startup_delay=${STARTUP_DELAY}s"

mkdir -p "${INVENTORY_DIR}" "${STATUS_DIR}"

# --- Первичная пауза ----------------------------------------------------------
echo "[entrypoint] Ожидание ${STARTUP_DELAY} с перед первым сбором..."
sleep "${STARTUP_DELAY}"

# --- Основной цикл ------------------------------------------------------------
while true; do
    echo "==============================================================="
    echo "[entrypoint] $(date '+%F %T') Начало цикла сбора инвентаря"

    # servers_parser.py сам логирует ошибки по каждому серверу и возвращает 0,
    # если хотя бы один сервер обработан; ненулевой код — только при проблемах
    # с конфигурацией (нет servers.yaml и т.п.).
    python3 /app/servers_parser.py \
        --config /app/servers.yaml \
        --output-dir "${INVENTORY_DIR}"
    RC=$?
    echo "[entrypoint] Цикл завершён с кодом ${RC}"

    if [ "${RC}" -eq 2 ]; then
        echo "[entrypoint] ОШИБКА: конфиг /app/servers.yaml недоступен или некорректен."
        echo "[entrypoint] Повтор через ${CHECK_INTERVAL} с. Проверьте монтирование файла."
    fi

    # Heartbeat для healthcheck: touch обновляет mtime файла
    touch "${STATUS_DIR}/last_run"

    echo "[entrypoint] Следующий цикл через ${CHECK_INTERVAL} с"
    sleep "${CHECK_INTERVAL}"
done
