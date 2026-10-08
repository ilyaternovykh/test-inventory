# -*- coding: utf-8 -*-
"""
cr_login_redirect_patch.py — совместимость collect-а с BMC, отвечающими 3xx на POST сессий.

Проблема (наблюдаемая ошибка):
    [CRITICAL]: Unable to connect to Host '192.168.10.37:443': Unsafe redirect location:
                https://192.168.10.37/redfish/v1/SessionService/Sessions

Причина: в check_redfish >= 2.2.0 логин выполняется через requests.post(..., allow_redirects=False).
Некоторые BMC (Lenovo XCC, Supermicro, отдельные прошивки iDRAC/iLO) отвечают на
POST /redfish/v1/SessionService/Sessions редиректом 3xx, Location которого указывает на ТОТ ЖЕ
самый путь /redfish/v1/.../Sessions (часто относительный, без scheme/host). Валидатор
_is_safe_redirect() требует, чтобы parsed.hostname совпадал с cli_args.host; для относительного
Location hostname == None => проверка падает => исключение "Unsafe redirect location" => rc=2.
Это особенность конкретного BMC, а не сетевой недоступности хоста.

Решение: monkey-patch login_with_redirect_handling(): перед валидацией резолвим относительный
Location относительно исходного sessions_url (urljoin), и при совпадении пути разрешаем редирект.
Модуль патчит только сам себя (импортируется из site-packages), файлы пакета не меняются.

Подключение: python -m cr_login_redirect_patch <аргументы check_redfish>
(см. entrypoint.sh / servers_parser.py — включается env CR_FIX_REDIRECT=1, по умолчанию включён).
"""

import os
import sys
from urllib.parse import urljoin, urlparse


def apply_patch():
    """Возвращает True, если патчинг применён успешно."""
    try:
        import requests
        from cr_module.classes import redfish as rf_mod
    except ImportError:
        return False

    RedfishConnection = rf_mod.RedfishConnection
    # если патч уже применялся ранее — выходим
    if getattr(RedfishConnection, "_cr_redirect_patched", False):
        return True

    _orig_is_safe = RedfishConnection._is_safe_redirect.__func__ \
        if isinstance(RedfishConnection._is_safe_redirect, staticmethod) \
        else RedfishConnection._is_safe_redirect

    def _patched_is_safe(redirect_url, original_host):
        # базовая проверка оригинала
        if _orig_is_safe(redirect_url, original_host):
            return True
        # Разрешаем редирект на тот же sessions-путь того же host'а:
        #   - относительный Location ("/redfish/v1/...") — hostname пустой;
        #   - абсолютный, но host отличается регистром/IP vs DNS — сверяем путь.
        try:
            parsed = urlparse(redirect_url)
            if parsed.scheme in ("", "https") and parsed.path.startswith("/redfish/") \
                    and "/Sessions" in parsed.path:
                if parsed.hostname in (None, "") or parsed.hostname == original_host:
                    return True
        except Exception:
            pass
        return False

    RedfishConnection._is_safe_redirect = staticmethod(_patched_is_safe)

    def _patched_login(self):
        base_url = f"https://{self.cli_args.host}"
        sessions_url = f"{base_url}/redfish/v1/SessionService/Sessions"
        login_payload = {"UserName": self.username, "Password": self.password}

        # общий кодированный запрос (SSL verify=False — как в оригинале)
        def post(url):
            return requests.post(
                url, json=login_payload,
                timeout=self.cli_args.timeout,
                verify=False, allow_redirects=False,
            )

        response = post(sessions_url)

        if response.status_code in (301, 302, 303, 307, 308):
            redirect_url = response.headers.get("Location") or response.headers.get("location")
            if not redirect_url:
                raise Exception("Redirect response missing Location header")
            # резолвим относительный Location против исходного URL сессий
            redirect_abs = urljoin(sessions_url, redirect_url)
            if not _patched_is_safe(redirect_abs, self.cli_args.host):
                raise Exception(f"Unsafe redirect location: {redirect_url}")
            response = post(redirect_abs)

        session_token = response.headers.get("X-Auth-Token")
        session_location = response.headers.get("Location")

        if session_token is not None:
            self.connection.set_session_key(session_token)
            if session_location is not None:
                self.connection.set_session_location(session_location)
            return

        if response.status_code == 201:
            raise Exception("Login succeeded but no session token received")
        elif response.status_code == 401:
            import redfish
            raise redfish.rest.v1.InvalidCredentialsError("Authentication failed")
        elif response.status_code >= 500:
            raise Exception(f"Server error during login: {response.status_code}")
        else:
            raise Exception(f"Login failed with status {response.status_code}: {response.text}")

    RedfishConnection.login_with_redirect_handling = _patched_login
    RedfishConnection._cr_redirect_patched = True
    return True


def preflight_check(host, username, password, timeout=10):
    """Диагностика до запуска collect-а. Возвращает строку-вывод или None (если всё ок).

    Проверяет:
      1. Корректность host:port в аргументе -H (частая ошибка: 'https://IP' вместо 'IP').
      2. Достижимость Redfish root (/redfish/v1/) и наличие SessionService.
      3. Пробный POST на SessionService/Sessions: если BMC отвечает 3xx редиректом —
         печатает реальный заголовок Location (это и есть корень ошибки
         "Unsafe redirect location" в stock check_redfish >= 2.2.0).
    Сетевые/SSL ошибки НЕ приводят к падению — они только логируются, чтобы
    collect мог продолжить собственную обработку.
    """
    import requests
    requests.packages.urllib3.disable_warnings()

    lines = []
    raw_host = str(host or "").strip()

    # --- проверка формата host:port ---------------------------------------- #
    bad = None
    if "://" in raw_host:
        bad = ("в аргументе -H указан URL со схемой ('https://...'), а должен быть "
               "'хост' или 'хост:порт' без схемы")
    else:
        hp = raw_host.rsplit("/", 1)[0]
        if ":" in hp:
            h, _, p = hp.rpartition(":")
            if not p.isdigit():
                bad = f"порт '{p}' в '-H {raw_host}' не является числом"
    if bad:
        lines.append(f"[PREFLIGHT][ERROR] Некорректный -H '{raw_host}': {bad}. "
                     "Исправьте servers.yaml (host: должен быть вида '192.168.10.37', порт — отдельным полем port).")
        return "\n".join(lines)

    # нормализуем host[:port] для запросов
    base = f"https://{raw_host}"
    root_url = f"{base}/redfish/v1/"
    sess_url = f"{base}/redfish/v1/SessionService/Sessions"

    # --- 1) Redfish root ---------------------------------------------------- #
    try:
        r = requests.get(root_url, timeout=timeout, verify=False)
        lines.append(f"[PREFLIGHT] GET {root_url} -> HTTP {r.status_code}")
        if r.status_code != 200:
            lines.append(f"[PREFLIGHT][WARN] Redfish root вернул {r.status_code} — "
                         "BMC может не отдавать /redfish/v1 по HTTPS или требует другой порт.")
    except requests.exceptions.SSLError as e:
        lines.append(f"[PREFLIGHT][ERROR] SSL handshake не пройден для {base}: {e}. "
                     "Проверьте, что на этом порту работает HTTPS (для iDRAC/iLO/XCC это 443).")
        return "\n".join(lines)
    except requests.exceptions.ConnectionError as e:
        lines.append(f"[PREFLIGHT][ERROR] Соединение с {base} невозможно: {type(e).__name__}. "
                     "Проверьте доступность BMC из контейнера (сеть redfish-network / маршрут до подсети BMC).")
        return "\n".join(lines)
    except Exception as e:
        lines.append(f"[PREFLIGHT][ERROR] Ошибка запроса к {root_url}: {e}")
        return "\n".join(lines)

    # --- 2) SessionService -------------------------------------------------- #
    try:
        data = r.json()
    except Exception:
        data = {}
    links = data.get("Links", {}) if isinstance(data, dict) else {}
    sess_link = links.get("SessionService", {}).get("@odata.id") if isinstance(links, dict) else None
    if sess_link is None:
        lines.append("[PREFLIGHT][WARN] В Redfish root нет ссылки на SessionService — "
                     "логин через сессии может быть недоступен (попробуйте Basic-auth совместимый firmware).")
    else:
        try:
            rs = requests.get(f"{base}{sess_link}", timeout=timeout, verify=False,
                              auth=(username, password))
            lines.append(f"[PREFLIGHT] GET {sess_link} (basic auth) -> HTTP {rs.status_code}")
            if rs.status_code == 401:
                lines.append("[PREFLIGHT][ERROR] 401 на basic auth — учётные данные неверны "
                             "(проверьте REDFISH_USERNAME/PASSWORD или username/password сервера в servers.yaml).")
        except Exception as e:
            lines.append(f"[PREFLIGHT][WARN] Не удалось опросить {sess_link}: {e}")

    # --- 3) Пробный POST Sessions (демо редиректного поведения) -------------- #
    try:
        rp = requests.post(sess_url, json={"UserName": username, "Password": password},
                           timeout=timeout, verify=False, allow_redirects=False)
        loc = rp.headers.get("Location") or rp.headers.get("location")
        lines.append(f"[PREFLIGHT] POST {sess_url} -> HTTP {rp.status_code}" +
                     (f", Location: {loc}" if loc else ""))
        if rp.status_code in (301, 302, 303, 307, 308):
            lines.append("[PREFLIGHT][INFO] Этот BMC отвечает 3xx на POST Sessions — именно поэтому "
                         "stock check_redfish падал с 'Unsafe redirect location'. Патч резолвит "
                         "относительный Location и повторяет POST на целевой URL.")
    except Exception as e:
        lines.append(f"[PREFLIGHT][WARN] Пробный POST Sessions не выполнен: {e}")

    return "\n".join(lines)


def _extract_opt(argv, flag):
    """Возвращает значение флага вида ['-H', 'value'] из argv (или None)."""
    try:
        i = argv.index(flag)
        return argv[i + 1]
    except (ValueError, IndexError):
        return None


def main():
    """Запуск check_redfish.main() с активным патчем (модуль вызывается как
    `python -m cr_login_redirect_patch ...`). Аргументы передаются как есть."""
    patched = apply_patch()
    if not patched:
        # пакет недоступен — тихо деградируем до обычного запуска
        print("[cr_login_redirect_patch] WARNING: cr_module не найден, патч не применён", file=sys.stderr)
    else:
        print("[cr_login_redirect_patch] Патч 3xx-редиректа логина применён.", file=sys.stderr)

    # Preflight-диагностика включается env CR_PREFLIGHT=1 (по умолчанию выключена,
    # чтобы не светить креды в логах; включается при отладке конкретных BMC)
    if os.environ.get("CR_PREFLIGHT", "") == "1":
        argv = sys.argv[1:]
        host = _extract_opt(argv, "-H") or _extract_opt(argv, "--host")
        user = _extract_opt(argv, "-u") or _extract_opt(argv, "--username") or os.environ.get("REDFISH_USERNAME", "")
        pwd = _extract_opt(argv, "-p") or _extract_opt(argv, "--password") or os.environ.get("REDFISH_PASSWORD", "")
        if host:
            out = preflight_check(host, user, pwd)
            if out:
                print(out, file=sys.stderr)

    from check_redfish import main as cr_main
    cr_main()


if __name__ == "__main__":
    main()
