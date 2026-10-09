# -*- coding: utf-8 -*-
"""
cr_ilorest_patch.py — сбор инвентаря iLO4 через HP REST (ilorest) вместо Redfish.

Диагноз (наблюдаемые ошибки на стенде 192.168.10.37, HPE iLO4 firmware 2.82):
    1) POST https://.../redfish/v1/SessionService/Sessions -> HTTP 308,
       Location == тот же URL (само-редирект). Сессионный токен не выдаётся никогда.
    2) Попытка Basic-auth к Redfish тоже проваливается («max retries exhausted»).
    3) Ключевое наблюдение (вывод curl из контейнера): вместо JSON с Redfish root
       BMC отдаёт HTML веб-консоли iLO (фрагменты "iLO.getBaseUrl()", "showLogin",
       iframe appFrame). Это классический симптом iLO4 fw 2.8x: Redfish Service Root
       НЕ включён в прошивке — любые пути /redfish/v1/* редиректятся на HTML-логин.

Вывод: для этого устройства Redfish-путь принципиально нерабочий (лечится только
апгрейдом firmware iLO4 до 2.50+ с включённым Redfish в Embedded Directory Services /
User Administration). Но инвентарь iLO4 полностью доступен через родственный API —
HP REST / iLO RESTful API утилиты HPE `ilorest` (https://github.com/HewlettPackard/ilorest),
которая умеет работать по basic-auth без Redfish Session Service.

Решение (этот модуль):
    * servers_parser.py направляет серверы с флагом `api: ilorest` сюда;
    * скрипт собирает команды `ilorest ... --selector OemHpProfileSet`, парсит JSON,
      конвертирует в формат inventory-а check_redfish и пишет в
      /app/inventory/<name>.json (тот же shared volume, netbox-sync не различает источник);
    * поддерживаются overrides per-server: ilorest_username/ilorest_password
      (или username/password), ilorest_port (или port, по умолчанию 443).

Ограничения MVP (честно):
    * CPU/DIMM/NIC могут быть смёржены в один тип (по наличию полей), т.к. в
      формате check_redfish это разные inventory_type;
    * PSU/Drive/cooling не собираются (расширение — по запросу);
    * health = OK при успешном сборе (per-item health из ilorest не маппится).

Требование к image: ilorest должен быть установлен (см. Dockerfile, env CR_ILOREST_AUTOINSTALL).
"""

import json
import os
import shutil
import subprocess
import sys
import time

DEFAULT_TIMEOUT = 120


def log(msg):
    print(f"[cr_ilorest_patch] {msg}", file=sys.stderr, flush=True)


def find_ilorest():
    """Путь к бинарю ilorest: env CR_ILOREST_BIN -> PATH -> pip-user bin."""
    p = os.environ.get("CR_ILOREST_BIN", "")
    if p and os.path.exists(p):
        return p
    f = shutil.which("ilorest")
    if f:
        return f
    cand = os.path.join(os.path.dirname(sys.executable), "ilorest")
    if os.path.exists(cand):
        return cand
    user_bin = os.path.join(os.path.expanduser("~"), ".local", "bin", "ilorest")
    if os.path.exists(user_bin):
        return user_bin
    return None


def ensure_ilorest(autoinstall=True):
    """Найти ilorest; при необходимости установить через pip (--user)."""
    path = find_ilorest()
    if path:
        return path
    if not autoinstall:
        return None
    log("ilorest не найден — устанавливаю через pip (--user)...")
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--user", "ilorest"],
            capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            log(f"pip install ilorest завершился с rc={r.returncode}: "
                f"{(r.stderr or '')[-300:]}")
            return None
    except Exception as e:
        log(f"Не удалось установить ilorest: {e}")
        return None
    return find_ilorest()


def ilorest_command(server):
    """Собрать базовую команду ilorest с кредом и портом из конфига сервера."""
    host = str(server.get("host", "")).strip()
    port = server.get("ilorest_port", server.get("port", 443))
    user = (server.get("ilorest_username") or server.get("username")
            or os.environ.get("REDFISH_USERNAME", ""))
    pw = (server.get("ilorest_password") or server.get("password")
          or os.environ.get("REDFISH_PASSWORD", ""))
    if not host:
        raise ValueError("В конфиге сервера не задан host")
    if not user or not pw:
        raise ValueError("Не заданы учётные данные для ilorest "
                         "(username/password или REDFISH_USERNAME/REDFISH_PASSWORD)")
    target = f"{host}:{port}" if int(port) != 443 else host
    return ["ilorest", f"--url={target}", f"--login={user}",
            f"--password={pw}", "--selector=OemHpProfileSet"]


def run_cmd(cmd, timeout):
    """Выполнить команду, вернуть (rc, stdout, stderr). Пароль маскируется в команде-логе."""
    masked = [c for c in cmd]
    for i, c in enumerate(masked):
        if c.startswith("--password="):
            masked[i] = "--password=***"
    log("Запуск: " + " ".join(masked))
    t0 = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    dur = time.time() - t0
    if proc.returncode != 0:
        # ilorest иногда кладет ошибку в stdout — печатаем оба потока
        log(f"ilorest вернул rc={proc.returncode} за {dur:.1f}s")
        err_tail = ((proc.stderr or "")[-1000:] + "\n" + (proc.stdout or "")[-500:])
        raise RuntimeError(f"ilorest rc={proc.returncode}: {err_tail.strip()}")
    return proc.stdout, proc.stderr


def parse_json(out):
    """Разбор stdout ilorest: может содержать предупреждающие строки до JSON."""
    out = (out or "").strip()
    if not out:
        raise ValueError("ilorest вернул пустой вывод")
    try:
        return json.loads(out)
    except Exception:
        pass
    start = min([i for i in (out.find("["), out.find("{")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError(f"В выводе ilorest не найден JSON: {out[:200]}")
    return json.loads(out[start:])


def pick(d, *keys):
    """Первое непустое значение среди ключей (с поддержкой вложенности 'a.b')."""
    for k in keys:
        cur = d
        ok = True
        for part in k.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, "", [], {}):
            return cur
    return None


def build_item(raw, itype):
    """Конвертация одного словаря ilorest в элемент inventory check_redfish."""
    name = pick(raw, "Name", "Model", "DeviceID", "UID") or f"{itype}-{id(raw) % 10000}"
    item = {
        "type": raw.get("@odata.type", ""),
        "status": {"health": "OK", "state": "Enabled"},
        "name": str(name),
        "raw": raw,
    }
    sn = pick(raw, "SerialNumber", "SN", "DeviceInfo.ASSET_TAG",
              "Oem.Hpe.DimmRankInfo")
    if sn:
        item["serial_number"] = str(sn)
    mpn = pick(raw, "Model", "PartNumber", "DeviceID")
    if mpn:
        item["model"] = str(mpn)
    return item


def gather(server, out_path, timeout=DEFAULT_TIMEOUT):
    """Основной цикл сбора: system + cpu + memory (+ nic best-effort) -> JSON."""
    base = ilorest_command(server)
    # порядок команд фиксирован; отсутствующие секции не критичны
    sections = [
        ("system", ["ilorest", "serverinfo"]),
        ("cpu", ["ilorest", "processor"]),
        ("memory", ["ilorest", "memory"]),
    ]
    inv = {"system": [], "cpu": [], "ram": [], "storage": {},
           "drive": [], "nic": [], "gpu": [], "psu": [], "fan": []}
    errors = []

    first_err = None
    for key, sub in sections:
        cmd = base + sub
        try:
            out, _ = run_cmd(cmd, timeout)
            data = parse_json(out)
        except Exception as e:
            errors.append(f"{key}: {e}")
            if first_err is None:
                first_err = e
            continue
        items = data if isinstance(data, list) else [data]
        for it in items:
            if not isinstance(it, dict):
                continue
            if key == "system":
                inv["system"].append(build_item(it, "System"))
            elif key == "cpu":
                inv["cpu"].append(build_item(it, "CPU"))
            elif key == "memory":
                inv["ram"].append(build_item(it, "DIMM"))

    # NIC — опционально: если профиль не отдаёт, просто пропускаем
    try:
        out, _ = run_cmd(base + ["ilorest", "networkadapter"], timeout)
        data = parse_json(out)
        items = data if isinstance(data, list) else [data]
        for it in items:
            if isinstance(it, dict):
                inv["nic"].append(build_item(it, "NIC"))
    except Exception as e:
        errors.append(f"nic: {e}")

    if not any(inv[k] for k in ("system", "cpu", "ram", "nic")):
        # ни одна секция не собрана — это ошибка подключения/кредов
        raise RuntimeError(f"Инвентарь не собран. Ошибки: {' | '.join(errors)}")

    meta = {
        "inventory_id": server.get("netbox_device_id"),
        "name": server.get("name"),
        "source": "ilorest (HPE REST, обход недоступного Redfish на iLO4 2.8x)",
        "collected_at": time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime()),
    }
    result = {"meta": meta, "inventory": inv, "errors": errors}

    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, out_path)
    counts = {k: len(v) for k, v in inv.items() if isinstance(v, list)}
    log(f"OK: записан {out_path}; секции: {counts}; ошибок секций: {len(errors)}")
    return 0


def main():
    argv = sys.argv[1:]
    host = user = password = name = out_path = None
    port = 443
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-H", "--host") and i + 1 < len(argv):
            hv = argv[i + 1]
            host, _, p = hv.partition(":")
            port = int(p) if p.isdigit() else 443
            i += 2
            continue
        if a in ("-u", "--user") and i + 1 < len(argv):
            user = argv[i + 1]; i += 2; continue
        if a in ("-p", "--password") and i + 1 < len(argv):
            password = argv[i + 1]; i += 2; continue
        if a == "--inventory_name" and i + 1 < len(argv):
            name = argv[i + 1]; i += 2; continue
        if a == "--inventory_file" and i + 1 < len(argv):
            out_path = argv[i + 1]; i += 2; continue
        i += 1

    if not host or not out_path:
        log("Нужны аргументы: -H host[:port] -u USER -p PASS "
            "--inventory_name NAME --inventory_file PATH")
        return 2

    server = {"name": name or host, "host": host, "port": port,
              "username": user, "password": password}
    if not ensure_ilorest(autoinstall=os.environ.get("CR_ILOREST_AUTOINSTALL", "1") != "0"):
        log("НЕ УДАЛОСЬ НАЙТИ/УСТАНОВИТЬ ilorest. Соберите image заново "
            "(Dockerfile устанавливает ilorest) или задайте путь через CR_ILOREST_BIN.")
        return 2

    try:
        return gather(server, out_path, timeout=int(os.environ.get("CR_ILOREST_TIMEOUT", DEFAULT_TIMEOUT)))
    except Exception as e:
        log(f"ОШИБКА сбора: {e}")
        return 2


if __name__ == "__main__":
    sys.exit(main())
