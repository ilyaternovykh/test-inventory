#!/bin/sh
# =============================================================================
# entrypoint.sh для образа ghcr.io/bb-ricardo/netbox-sync
#
# Назначение:
#   1. Ждёт появления хотя бы одного JSON-файла в /app/inventory (коллектор
#      ещё может быть на первичной паузе STARTUP_DELAY).
#   2. Делает паузу STARTUP_DELAY перед первой синхронизацией.
#   3. Запускает netbox_sync.py циклично каждые SYNC_INTERVAL секунд
#      (в оригинальном образе — однократный запуск; здесь добавляем цикл).
#   4. После каждого цикла трогает /app/status/last_run — heartbeat для
#      healthcheck из docker-compose.
#
# Реальные хост и токен NetBox приходят через переменные окружения
# NBS_NETBOX_HOST_FQDN / NBS_NETBOX_API_TOKEN и перекрывают значения из
# settings.yaml (механизм overrides netbox-sync), поэтому секреты не хранятся
# в файле конфигурации.
# =============================================================================
set -u

INVENTORY_DIR="${INVENTORY_DIR:-/app/inventory}"
STATUS_DIR="${STATUS_DIR:-/app/status}"
CONFIG_FILE="${CONFIG_FILE:-/app/settings.yaml}"
SYNC_INTERVAL="${SYNC_INTERVAL:-900}"     # период синхронизации, сек
STARTUP_DELAY="${STARTUP_DELAY:-60}"      # задержка перед первым прогоном, сек
WAIT_INVENTORY_TIMEOUT="${WAIT_INVENTORY_TIMEOUT:-3600}"  # макс. ожидание JSON, сек

echo "[netbox-sync-entrypoint] Старт: interval=${SYNC_INTERVAL}s, delay=${STARTUP_DELAY}s, config=${CONFIG_FILE}"

mkdir -p "${STATUS_DIR}"

# --- Ожидаем первый инвентарь от коллектора -----------------------------------
echo "[netbox-sync-entrypoint] Ожидание файлов инвентаря в ${INVENTORY_DIR}..."
WAITED=0
while [ ! -n "$(ls "${INVENTORY_DIR}"/*.json 2>/dev/null)" ]; do
    if [ "${WAITED}" -ge "${WAIT_INVENTORY_TIMEOUT}" ]; then
        echo "[netbox-sync-entrypoint] ВНИМАНИЕ: за ${WAIT_INVENTORY_TIMEOUT} с файлов так и нет."
        echo "[netbox-sync-entrypoint] Продолжаем — sync отработает по пустому каталогу и повторит позже."
        break
    fi
    sleep 10
    WAITED=$((WAITED + 10))
done
echo "[netbox-sync-entrypoint] Найдено JSON-файлов: $(ls "${INVENTORY_DIR}"/*.json 2>/dev/null | wc -l)"

# --- Пауза перед первой синхронизацией ----------------------------------------
echo "[netbox-sync-entrypoint] Ожидание ${STARTUP_DELAY} с перед первым прогоном..."
sleep "${STARTUP_DELAY}"

# --- Основной цикл -------------------------------------------------------------
while true; do
    echo "==============================================================="
    echo "[netbox-sync-entrypoint] $(date '+%F %T') Цикл синхронизации с NetBox"

    # Original image запускается командой из docker-compose:
    #   python -u /app/netbox_sync.py -c /app/settings.yaml
    # env NBS_* перекрывают значения из конфига (штатный механизм overrides).
    "$@"
    RC=$?

    if [ "${RC}" -eq 0 ]; then
        echo "[netbox-sync-entrypoint] Синхронизация завершена успешно"
    else
        # Не падаем: ошибки NetBox (unreachable, конфликт имён) часто временные
        echo "[netbox-sync-entrypoint] ОШИБКА: синхронизация завершилась с кодом ${RC}. Повтор через ${SYNC_INTERVAL} с."
    fi

    # Heartbeat для healthcheck
    touch "${STATUS_DIR}/last_run"

    echo "[netbox-sync-entrypoint] Следующий цикл через ${SYNC_INTERVAL} с"
    sleep "${SYNC_INTERVAL}"
done
