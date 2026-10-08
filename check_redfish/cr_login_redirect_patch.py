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

Дополнительно (наблюдаемый симптом 2): BMC Lenovo XCC отвечает на POST Sessions редиректом
308 Permanent Redirect с Location == тот же самый URL. При ручном curl без --max-redirs это
выглядит как «зацикливание», а requests внутри redfish-клиента выбрасывает
RetriesExhaustedError («max retries exhausted»). Поэтому патч:
  * перехватывает ВСЕ HTTP-запросы через requests.sessions.Session.request и для GET/HEAD
    автоматически проходит цепочку 3xx-редиректов вручную (до CR_MAX_REDIRECTS шагов,
    по умолчанию 5) — вместо падения requests TooManyRedirects;
  * в логине повторяет POST на Location (307/308 сохраняют метод и тело); если целевой URL
    совпадает с исходным (само-редирект), запрос повторяется ещё раз (BMC после первой
    «прогревающей» попытки обычно отдаёт 201 + X-Auth-Token); если токена так и нет —
    срабатывает Basic-auth fallback;
  * печатает реальные коды ответов и Location в stderr — чтобы по docker logs было видно,
    что именно отвечает BMC (в т.ч. заголовок Location у 308).

Подключение: python -m cr_login_redirect_patch <аргументы check_redfish>
(см. entrypoint.sh / servers_parser.py — включается env CR_FIX_REDIRECT=1, по умолчанию включён).
"""

import os
import sys
from urllib.parse import urljoin, urlparse


def _max_redirects():
    """Максимум шагов редиректа для ручного обхода 3xx-цепочек (env CR_MAX_REDIRECTS)."""
    try:
        return max(1, int(os.environ.get("CR_MAX_REDIRECTS", "5")))
    except ValueError:
        return 5


def _is_transient_conn_error(exc):
    """True, если исключение — сетевая ошибка (retry-able), а не ошибка BMC/аутентификации."""
    try:
        import requests
    except ImportError:
        return False
    return isinstance(exc, (requests.exceptions.ConnectionError,
                            requests.exceptions.Timeout))


# --- Хелперы доступа к классам ошибок redfish-библиотеки --------------------- #
# Используется ленивый импорт: на хосте для тестов эти классы могут отсутствовать.
def _rf_err(name, fallback_base=Exception):
    """Возвращает класс ошибки из redfish.rest.v1 (или динамический fallback)."""
    try:
        import redfish.rest.v1 as rv1
        return getattr(rv1, name)
    except Exception:
        if fallback_base is None:
            raise
        return type(name, (fallback_base,), {})


def redfish_invalid_credentials_error():
    return _rf_err("InvalidCredentialsError")


def redfish_rest_retries_exhausted(msg):
    """Экземпляр RetriesExhaustedError (check_redfish мапит его в понятный CRITICAL)."""
    return _rf_err("RetriesExhaustedError")(msg)


def redfish_server_down_or_unreachable(msg):
    """Экземпляр ServerDownOrUnreachableError с ПОНЯТНЫМ текстом.

    ВАЖНО: stock check_redfish ловит это исключение в init_connection() и печатает
    generic 'Host ... down or unreachable.' без текста причины. Поэтому текст
    дублируется здесь, в stderr — чтобы в 'docker logs' было видно реальную причину.
    """
    exc = _rf_err("ServerDownOrUnreachableError")(msg)
    print(f"[cr_login_redirect_patch] {msg}", file=sys.stderr)
    return exc


def _follow_get_redirects(session, method, url, kwargs):
    """Ручной обход цепочки 3xx-редиректов для GET/HEAD (requests их не проходит сам,
    когда allow_redirects=False, а с allow_redirects=True зацикливается на само-редиректах).

    session — объект requests.Session; оригинальный Session.request вызывается через
    __class__ (он может быть уже запатчен этим же кодом — рекурсии нет, т.к. request_fn
    это saved-ссылка на НЕпатченый метод). Возвращает последний Response.
    Логирует каждый шаг в stderr (видно в docker logs без DEBUG).
    """
    max_steps = _max_redirects()
    request_fn = getattr(session.__class__, "_cr_orig_request", None)
    if request_fn is None:  # патч ещё не применён — используем текущий метод как есть
        request_fn = session.request
    resp = None
    cur = url
    for step in range(max_steps + 1):
        kw = dict(kwargs)
        kw["allow_redirects"] = False
        resp = request_fn(session, method, cur, **kw)
        if resp.status_code not in (301, 302, 303, 307, 308):
            return resp
        loc = resp.headers.get("Location") or resp.headers.get("location")
        if not loc:
            return resp
        nxt = urljoin(cur, loc)
        print(f"[cr_login_redirect_patch] {method} {cur} -> HTTP {resp.status_code}, "
              f"Location: {loc}; шаг {step + 1}/{max_steps}", file=sys.stderr)
        if nxt == cur:
            # само-редирект: повтор ещё не имеет смысла — возвращаем как есть
            print("[cr_login_redirect_patch] Редирект указывает на тот же URL "
                  "(само-редирект BMC) — прекращаем обход.", file=sys.stderr)
            return resp
        # 303 к GET; 307/308 сохраняют метод (для GET это тоже GET)
        cur = nxt
    print(f"[cr_login_redirect_patch] Превышен лимит редиректов ({max_steps}) для {url}",
          file=sys.stderr)
    return resp


def _patch_requests_session():
    """Патчит requests.sessions.Session.request: включает автоматический обход 3xx
    для всех запросов collector-а (Redfish root, Services, Systems и т.д.), которые
    stock check_redfish отправляет с allow_redirects=False. Идемпотентно."""
    import requests
    from requests.sessions import Session

    if getattr(Session, "_cr_auto_redirect_patched", False):
        return
    orig_request = Session.request
    # сохраняем ссылку на оригинал — по ней обход редиректов вызывает реальные запросы
    Session._cr_orig_request = orig_request

    def patched_request(self, method, url, **kwargs):
        ar = kwargs.get("allow_redirects", True)
        m = str(method).upper()
        if ar is False and m in ("GET", "HEAD"):
            # GET/HEAD с отключёнными редиректами на BMC с 308 зависают/падают —
            # проходим цепочку вручную (с логом каждого шага)
            return _follow_get_redirects(self, method, url, kwargs)
        return orig_request(self, method, url, **kwargs)

    Session.request = patched_request
    Session._cr_auto_redirect_patched = True
    print("[cr_login_redirect_patch] Автообход 3xx-редиректов для GET-запросов включён "
          f"(лимит: {_max_redirects()}, env CR_MAX_REDIRECTS).", file=sys.stderr)


def apply_patch():
    """Возвращает True, если патчинг применён успешно."""
    try:
        import requests
        from cr_module.classes import redfish as rf_mod
    except ImportError:
        return False

    # автообход редиректов на уровне requests (не зависит от cr_module)
    try:
        _patch_requests_session()
    except Exception as e:
        print(f"[cr_login_redirect_patch] WARNING: патч requests.Session не применён: {e}",
              file=sys.stderr)

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

        # Запасной вариант: stock-логин redfish-библиотеки (Basic-auth fallback).
        # Некоторые BMC (отдельные прошивки XCC/Supermicro/iDRAC) не отдают
        # X-Auth-Token на POST Sessions в обход redfish-клиента (например,
        # сессионная политика требует заголовков Redfish-Version/OData-Version,
        # которые подставляет только сам redfish-клиент). В этом случае штатный
        # connection.login() работает корректно — используем его как fallback.
        def basic_fallback():
            try:
                self.connection.login(
                    username=self.username, password=self.password,
                    session_enabled=False,
                )
                print("[cr_login_redirect_patch] Логин выполнен через Basic-auth "
                      "fallback (redfish-клиент библиотеки).", file=sys.stderr)
                return True
            except Exception as fe:
                if _is_transient_conn_error(fe):
                    raise  # сетевая проблема — отдаём наверх как ретраабельную
                print(f"[cr_login_redirect_patch] Basic-auth fallback не удался: {fe}",
                      file=sys.stderr)
                return False

        # Общий кодированный запрос (SSL verify=False — как в оригинале).
        # Транзиентные сетевые ошибки (BMC временами рвёт keep-alive соединения)
        # повторяются несколько раз с паузой — это лечит «max retries exhausted».
        retry_max = int(os.environ.get("CR_LOGIN_RETRIES", "3"))
        retry_wait = float(os.environ.get("CR_LOGIN_RETRY_WAIT", "2"))
        last_exc = None

        for attempt in range(1, retry_max + 1):
            try:
                resp1 = requests.post(
                    sessions_url, json=login_payload,
                    timeout=self.cli_args.timeout,
                    verify=False, allow_redirects=False,
                )
                print(f"[cr_login_redirect_patch] POST {sessions_url} -> HTTP "
                      f"{resp1.status_code}"
                      + (f", Location: {resp1.headers.get('Location')}"
                         if resp1.headers.get("Location") else ""), file=sys.stderr)
                response = resp1
                if resp1.status_code in (301, 302, 303, 307, 308):
                    redirect_url = resp1.headers.get("Location") or resp1.headers.get("location")
                    if not redirect_url:
                        raise Exception("Redirect response missing Location header")
                    # резолвим относительный Location против исходного URL сессий
                    redirect_abs = urljoin(sessions_url, redirect_url)
                    if not _patched_is_safe(redirect_abs, self.cli_args.host):
                        raise Exception(f"Unsafe redirect location: {redirect_url}")
                    response = requests.post(
                        redirect_abs, json=login_payload,
                        timeout=self.cli_args.timeout,
                        verify=False, allow_redirects=False,
                    )
                    print(f"[cr_login_redirect_patch] POST (redirect) {redirect_abs} -> HTTP "
                          f"{response.status_code}", file=sys.stderr)

                    # Само-редирект (характерно для Lenovo XCC: 308 -> тот же URL):
                    # BMC часто отдаёт 201 + X-Auth-Token со второй попытки.
                    if (response.status_code in (301, 302, 303, 307, 308)
                            and urljoin(redirect_abs,
                                        response.headers.get("Location") or "") == redirect_abs):
                        response = requests.post(
                            redirect_abs, json=login_payload,
                            timeout=self.cli_args.timeout,
                            verify=False, allow_redirects=False,
                        )
                        print(f"[cr_login_redirect_patch] POST (повтор после само-редиректа) "
                              f"{redirect_abs} -> HTTP {response.status_code}", file=sys.stderr)

                session_token = response.headers.get("X-Auth-Token")
                session_location = response.headers.get("Location")

                if session_token is not None:
                    self.connection.set_session_key(session_token)
                    if session_location is not None:
                        self.connection.set_session_location(session_location)
                    return

                # BMC принял редирект/POST, но токена нет — пробуем Basic-auth fallback
                # (401 здесь означает реальные неверные креды — fallback не поможет)
                if response.status_code != 401 and basic_fallback():
                    return

                if response.status_code == 201:
                    raise Exception("Login succeeded but no session token received")
                elif response.status_code == 401:
                    import redfish
                    raise redfish.rest.v1.InvalidCredentialsError("Authentication failed")
                elif response.status_code >= 500:
                    raise Exception(f"Server error during login: HTTP {response.status_code}")
                else:
                    body = (response.text or "")[:200]
                    raise Exception(
                        f"Login failed with status {response.status_code}: {body} "
                        f"(проверьте учётные данные и что Redfish включён на BMC; "
                        f"диагностика: CR_PREFLIGHT=1)"
                    )

            except requests.exceptions.Timeout:
                raise redfish_rest_retries_exhausted("Request timeout")
            except requests.exceptions.SSLError:
                raise redfish_server_down_or_unreachable("SSL connection failed")
            except requests.exceptions.ConnectionError as e:
                # Соединение разорвано/refused — для некоторых BMC это транзиентно:
                # повторяем попытку вместо мгновенного падения.
                last_exc = e
                if attempt < retry_max:
                    print(f"[cr_login_redirect_patch] Попытка {attempt}/{retry_max}: "
                          f"соединение прервано ({type(e).__name__}), повтор через "
                          f"{retry_wait}s...", file=sys.stderr)
                    import time as _t
                    _t.sleep(retry_wait)
                    continue
                raise redfish_server_down_or_unreachable(
                    f"Connection failed after {retry_max} attempts: {e}")
            except redfish_invalid_credentials_error() as e:
                raise  # неверные креды — не ретраим, чтобы не блокировать учётку на BMC

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


def _network_diagnosis(host, port_hint=None):
    """Диагностика сети до BMC при ConnectionError: TCP-порт + ICMP-подобная проверка.

    Печатает в stderr конкретные гипотезы (маршрут / firewall / docker-сеть), чтобы по
    'docker logs' можно было отличить недоступный BMC от проблем с учётками/BMC-API.
    Возвращает строку-резюме (может быть пустой).
    """
    import socket
    hp = str(host or "").strip()
    if "://" in hp:
        hp = hp.split("://", 1)[1]
    hostname = hp.rsplit("/", 1)[0]
    if ":" in hostname:
        hname, _, pstr = hostname.rpartition(":")
        port = int(pstr) if pstr.isdigit() else 443
        hostname = hname
    else:
        port = int(port_hint) if port_hint else 443

    lines = []
    # 1) Разрешение имени (для IP пропускаем)
    try:
        infos = socket.getaddrinfo(hostname, port, proto=socket.IPPROTO_TCP)
        ip = infos[0][4][0]
        lines.append(f"DNS: {hostname} -> {ip}")
    except socket.gaierror as e:
        lines.append(f"DNS: НЕ резолвится '{hostname}': {e}. "
                     "Проверьте host в servers.yaml / DNS контейнера.")
        return "\n".join(lines)

    # 2) TCP connect с таймаутом
    try:
        s = socket.create_connection((hostname, port), timeout=8)
        s.close()
        lines.append(f"TCP: порт {port} на {hostname} ОТКРЫТ — проблема не в сети; "
                     "см. текст ошибки логина выше (учётки / Redfish выключен на BMC). "
                     "Включите диагностику: CR_PREFLIGHT=1")
    except OSError as e:
        errno_ = getattr(e, "errno", None)
        hint = {
            111: "Connection refused — на BMC нет HTTPS на этом порту (проверьте port в servers.yaml)",
            113: "No route to host — контейнер не видит подсеть BMC (redfish-network/маршруты)",
            101: "Network unreachable — docker-сеть не маршрутизируется к BMC",
        }.get(errno_, "")
        lines.append(f"TCP: connect {hostname}:{port} не удался: {type(e).__name__} ({e}). {hint}".rstrip())
    return "\n".join(lines)


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
    try:
        cr_main()
    except SystemExit:
        raise
    except BaseException as e:
        # Stock check_redfish при сетевых сбоях печатает только generic
        # 'max retries exhausted' без причины. Добавляем диагностику сети,
        # чтобы в docker logs было видно реальный корень проблемы.
        text = f"{type(e).__name__}: {e}"
        lowered = text.lower()
        if ("unreachable" in lowered or "retries exhausted" in lowered
                or "unable to connect" in lowered):
            try:
                argv = sys.argv[1:]
                host = _extract_opt(argv, "-H") or _extract_opt(argv, "--host")
                if host:
                    print("[cr_login_redirect_patch] Диагностика соединения:", file=sys.stderr)
                    print(_network_diagnosis(host), file=sys.stderr)
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
