#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
servers_parser.py — обходчик списка серверов для коллектора check_redfish.

Что делает:
  1. Читает YAML-конфиг со списком серверов (по умолчанию /app/servers.yaml).
  2. Для каждого сервера запускает check_redfish.py с параметрами:
         -H host:port
         -u username -p password
         --all --inventory
         --inventory_file <output_dir>/<name>.json
         --inventory_id <netbox_device_id>    (если задан в конфиге)
         --inventory_name <name>
         --nosession
  3. Ошибки подключения к отдельным серверам НЕ прерывают общий цикл —
     они только логируются, и скрипт переходит к следующему серверу.
  4. Удаляет устаревшие .json файлы из выходного каталога, если сервер был
     убран из конфигурации (в каталоге остаются только файлы актуальных
     серверов).

Коды возврата:
  0 — все серверы обработаны (возможны отдельные ошибки подключения);
  1 — были ошибки подключения хотя бы к одному серверу;
  2 — фатальная проблема с конфигурацией (нет файла, битый YAML, пустой список).
"""

import argparse
import json
import logging
import os
import re
import shutil
import subprocess
import sys

import yaml

# --------------------------------------------------------------------------- #
# Логирование в stdout (подхватывается docker logs)
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    stream=sys.stdout,
)
LOG = logging.getLogger("servers_parser")

DEFAULT_CONFIG = "/app/servers.yaml"
DEFAULT_OUTPUT_DIR = "/app/inventory"

# Допустимые символы в имени сервера (защита от path traversal при записи файла)
SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def find_check_redfish() -> list:
    """Определяем команду запуска check_redfish внутри контейнера.

    Приоритет:
      1. Патч-обёртка cr_login_redirect_patch (env CR_FIX_REDIRECT != "0"):
         запускается как модуль `python -m cr_login_redirect_patch` — она
         чинит падение «Unsafe redirect location» на BMC, отвечающих 3xx
         редиректом на POST Sessions (XCC/Supermicro/часть iDRAC/iLO), и
         затем передаёт управление штатному check_redfish.main().
      2. env CR_SCRIPT / аргумент --cr-script (явный путь, для отладки).
      3. pip-installed раскладка (скрипт рядом с интерпретатором).
      4. git clone в типовые каталоги (/opt/check_redfish и т.п.).
      5. check_redfish(.py) в PATH.
      6. Модуль Python (-m check_redfish).
    Возвращает argv-префикс запуска (список).
    """
    candidates = []

    # 0) Патч-модуль совместимости 3xx-редиректа (по умолчанию включён)
    if os.environ.get("CR_FIX_REDIRECT", "1") != "0":
        try:
            import importlib.util
            if importlib.util.find_spec("cr_login_redirect_patch") is not None:
                return [sys.executable, "-m", "cr_login_redirect_patch"]
        except (ImportError, ValueError, ModuleNotFoundError):
            pass
        # fallback: файл рядом с этим скриптом (если PYTHONPATH=/app не задан)
        local_mod = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                 "cr_login_redirect_patch.py")
        if os.path.exists(local_mod):
            return [sys.executable, local_mod]

    # 1) Вариант "pip install git+https://..." — скрипт рядом с интерпретатором
    bin_dir = os.path.dirname(sys.executable)
    for name in ("check_redfish.py", "check_redfish"):
        path = os.path.join(bin_dir, name)
        if os.path.exists(path):
            candidates.append([sys.executable, path])

    # 0) Явное указание (env/CLI) — имеет приоритет
    # (обрабатывается в main через args.cr_script)

    # 1) Вариант "pip install git+https://..." — скрипт рядом с интерпретатором
    bin_dir = os.path.dirname(sys.executable)
    for name in ("check_redfish.py", "check_redfish"):
        path = os.path.join(bin_dir, name)
        if os.path.exists(path):
            candidates.append([sys.executable, path])

    # 2) Классическая раскладка репозитория (git clone + PYTHONPATH)
    for base in ("/app/check_redfish", "/opt/check_redfish", "/usr/local/src/check_redfish"):
        path = os.path.join(base, "check_redfish.py")
        if os.path.exists(path):
            candidates.append([sys.executable, path])

    # 3) Исполняемый файл в PATH (например, /usr/local/bin/check_redfish[.py])
    which = shutil.which("check_redfish.py") or shutil.which("check_redfish")
    if which:
        if which.endswith(".py"):
            candidates.append([sys.executable, which])
        else:
            candidates.append([which])

    # 4) Модуль Python (pip install может поставить пакет без console-script)
    try:
        import importlib.util
        for mod in ("check_redfish", "check_redfish.check_redfish"):
            if importlib.util.find_spec(mod) is not None:
                candidates.append([sys.executable, "-m", mod])
                break
    except (ImportError, ValueError, ModuleNotFoundError):
        pass

    if not candidates:
        LOG.error("Не найден check_redfish! Установлен ли пакет check_redfish?")
        sys.exit(2)

    LOG.info("Команда запуска check_redfish: %s", " ".join(candidates[0]))
    return candidates[0]


def load_servers(config_path: str) -> list:
    """Читает servers.yaml и возвращает список словарей-серверов.

    Возвращает None при фатальной ошибке конфига (нет файла, битый YAML,
    пустой список) — чтобы cleanup_stale_files не снёс весь инвентарь.
    """
    if not os.path.isfile(config_path):
        LOG.error("Файл конфигурации не найден: %s", config_path)
        return None  # None => фатальная ошибка конфига
    try:
        with open(config_path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
    except yaml.YAMLError as exc:
        LOG.error("Ошибка разбора YAML %s: %s", config_path, exc)
        return None  # None => фатальная ошибка конфига

    servers = data.get("servers") or []
    if not isinstance(servers, list) or not servers:
        LOG.error("В %s отсутствует непустой список 'servers'", config_path)
        return None
    return servers


def build_command(cr_prefix: list, server: dict, output_dir: str,
                  default_user: str, default_pass: str,
                  cr_retries: int = 5, cr_timeout: int = 30) -> tuple:
    """Собирает argv для одного сервера.

    Возвращает (cmd_list, inventory_json_path) или (None, причина_ошибки).
    Поля username/password сервера переопределяют общие REDFISH_* env.
    cr_retries/cr_timeout — параметры соединения check_redfish (-r/-t):
    увеличенные ретраи лечат 'max retries exhausted' на BMC, которые
    периодически обрывают keep-alive соединения.
    """
    name = str(server.get("name", "")).strip()
    host = str(server.get("host", "")).strip()

    if not name or not SAFE_NAME_RE.match(name):
        return None, f"некорректное имя сервера: '{name}'"
    if not host:
        return None, f"сервер '{name}': не задан host"

    port = server.get("port", 443)
    username = server.get("username") or default_user
    password = server.get("password") or default_pass

    if not username or not password:
        return None, (f"сервер '{name}': не заданы учётные данные "
                      "(ни в servers.yaml, ни в REDFISH_USERNAME/REDFISH_PASSWORD)")

    json_path = os.path.join(output_dir, f"{name}.json")

    cmd = list(cr_prefix) + [
        "-H", f"{host}:{port}",
        "-u", username,
        "-p", password,
        "--all",
        "--inventory",
        "--inventory_file", json_path,
        "--inventory_name", name,
        "--nosession",
    ]

    # Параметры соединения: только если nonzero (0 = оставить дефолт check_redfish)
    if cr_retries:
        cmd += ["--retries", str(int(cr_retries))]
    if cr_timeout:
        cmd += ["--timeout", str(int(cr_timeout))]

    # netbox_device_id — числовой id устройства в NetBox (meta.inventory_id),
    # если известен; netbox-sync будет матчить устройство строго по нему.
    netbox_id = server.get("netbox_device_id")
    if netbox_id is not None and str(netbox_id).strip() != "":
        cmd += ["--inventory_id", str(netbox_id).strip()]

    return cmd, json_path


def run_server(cmd: list, name: str, timeout: int) -> bool:
    """Запускает check_redfish для одного сервера. True = успех."""
    # Маскируем пароль в логе: аргумент, идущий сразу после "-p"
    safe_cmd = list(cmd)
    for idx in range(1, len(safe_cmd)):
        if safe_cmd[idx - 1] == "-p":
            safe_cmd[idx] = "***"
    LOG.info("[%s] Запуск: %s", name, " ".join(safe_cmd))

    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=timeout,
            check=False,
        )
        output = res.stdout.decode("utf-8", errors="replace")
        if res.returncode == 0:
            LOG.info("[%s] OK (rc=0)", name)
            return True
        LOG.error("[%s] ОШИБКА подключения (rc=%s):\n%s", name, res.returncode, output[-2000:])
        return False
    except subprocess.TimeoutExpired:
        LOG.error("[%s] Таймаут (%s с) — BMC не ответил", name, timeout)
        return False
    except OSError as exc:
        LOG.error("[%s] Не удалось запустить check_redfish: %s", name, exc)
        return False


def cleanup_stale_files(servers: list, output_dir: str) -> None:
    """Удаляет .json файлы серверов, которых больше нет в конфигурации."""
    expected = {f"{str(s.get('name', '')).strip()}.json" for s in servers
                if s.get("name")}
    if not os.path.isdir(output_dir):
        return
    for fname in os.listdir(output_dir):
        if not fname.endswith(".json"):
            continue
        if fname in expected:
            continue
        path = os.path.join(output_dir, fname)
        try:
            os.remove(path)
            LOG.info("Удалён устаревший файл инвентаря: %s (сервер отсутствует в servers.yaml)", path)
        except OSError as exc:
            LOG.warning("Не удалось удалить %s: %s", path, exc)


def validate_json(path: str, name: str) -> bool:
    """Проверяет, что на выходе получился валидный JSON с секцией inventory."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if "inventory" not in data:
            LOG.warning("[%s] В JSON отсутствует секция 'inventory' — проверьте вывод check_redfish", name)
            return False
        return True
    except (OSError, json.JSONDecodeError) as exc:
        LOG.error("[%s] Файл %s не является валидным JSON: %s", name, path, exc)
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description="Пакетный запуск check_redfish по списку серверов")
    ap.add_argument("--config", default=DEFAULT_CONFIG, help="путь к servers.yaml")
    ap.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR, help="каталог для JSON-отчётов")
    ap.add_argument("--timeout", type=int, default=600,
                    help="таймаут опроса одного сервера, секунд (по умолчанию 600)")
    ap.add_argument("--cr-retries", type=int,
                    default=int(os.environ.get("CR_CONN_RETRIES", "5")),
                    help="число ретраев redfish-клиента на запрос (-r): BMC часто "
                         "разрывают keep-alive соединения, 5 ретраев лечит "
                         "'max retries exhausted' (по умолчанию 5, env CR_CONN_RETRIES)")
    ap.add_argument("--cr-timeout", type=int,
                    default=int(os.environ.get("CR_REQ_TIMEOUT", "30")),
                    help="таймаут одного HTTP-запроса к BMC, сек (-t, по умолчанию 30)")
    ap.add_argument("--cr-script", default=os.environ.get("CR_SCRIPT", ""),
                    help="явный путь к check_redfish.py (иначе автопоиск)")
    args = ap.parse_args()

    default_user = os.environ.get("REDFISH_USERNAME", "")
    default_pass = os.environ.get("REDFISH_PASSWORD", "")

    os.makedirs(args.output_dir, exist_ok=True)

    servers = load_servers(args.config)
    if servers is None:
        return 2
    LOG.info("Загружено серверов: %d (конфиг: %s)", len(servers), args.config)

    # Явный путь из CLI/env важнее автопоиска
    if args.cr_script:
        cr_prefix = [sys.executable, args.cr_script]
        LOG.info("check_redfish задан явно: %s", args.cr_script)
    else:
        cr_prefix = find_check_redfish()

    ok_count = err_count = 0
    for server in servers:
        cmd, info = build_command(cr_prefix, server, args.output_dir, default_user, default_pass,
                                  cr_retries=args.cr_retries, cr_timeout=args.cr_timeout)
        if cmd is None:
            LOG.error("Пропуск сервера: %s", info)
            err_count += 1
            continue
        name = str(server.get("name")).strip()
        json_path = info  # при успешной сборке команды info — путь к JSON
        success = run_server(cmd, name, args.timeout)
        if success and not validate_json(json_path, name):
            success = False
        if success:
            ok_count += 1
        else:
            err_count += 1

    # Серверы, удалённые из конфигурации, не должны оставлять «осиротевшие» JSON
    cleanup_stale_files(servers, args.output_dir)

    LOG.info("ИТОГ: успешно=%d, с ошибками=%d, всего=%d", ok_count, err_count, len(servers))
    return 0 if err_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
