# -*- coding: utf-8 -*-
"""
cr_basic_auth_patch.py — обход BMC, которые никогда не отдают сессионный токен.

Наблюдаемая ошибка (конкретный стенд, 192.168.10.37):
    [cr_login_redirect_patch] POST https://192.168.10.37:443/redfish/v1/SessionService/Sessions
                              -> HTTP 308, Location: https://192.168.10.37/redfish/v1/SessionService/Sessions
    [CRITICAL]: Unable to connect to Host '192.168.10.37:443', max retries exhausted.

Диагноз:
    BMC отвечает на POST Sessions редиректом 308 Permanent Redirect, и заголовок
    Location указывает ровно на ТОТ ЖЕ самый URL (само-редирект). Это «лукбэк»
    прошивки: любой POST на этот путь получает 308->self независимо от тела
    запроса. Сессионный логин через POST Sessions на таком устройстве НЕВОЗМОЖЕН
    в принципе — токен (X-Auth-Token) не выдаётся никогда.

    Предыдущий патч (cr_login_redirect_patch) резолвил относительный Location и
    повторял POST после само-редиректа — но повтор упирается в тот же 308, и
    цикл collect-а падает с rc=2 («max retries exhausted»).

    Примечание по стенду: устройство 192.168.10.37 идентифицировано как
    HPE iLO4 (firmware 2.82). Для iLO4 это известное поведение: Redfish Session
    Service на 2.82 нестабилен (3xx/405 на POST Sessions, особенно при
    переполненной таблице сессий или после серии неудачных входов), тогда как
    обычный Basic-auth к /redfish/v1/* работает корректно.

Решение:
    Не пытаться создавать Redfish-сессию вообще. Подменяем метод логина так,
    чтобы он использовал Basic-auth: штатный connection.login(...,
    session_enabled=False) redfish-библиотеки (заголовки Authorization: Basic,
    никаких POST Sessions). Проверено на XCC-подобных устройствах: GET-запросы
    к /redfish/v1/* под basic-auth работают даже когда POST Sessions бессмыслен.

    Дополнительно: если basic-auth тоже не проходит, печатаем конкретную
    диагностику (TCP/DNS + коды GET root / GET SessionService), чтобы по
    docker logs было видно, где именно проблема (сеть vs креды vs firmware).

Использование:
    python -m cr_basic_auth_patch <аргументы check_redfish>
    Включается автоматически servers_parser.py, если для сервера в servers.yaml
    задано `basic_auth: true` (или глобально env CR_FORCE_BASIC_AUTH=1).
    Для остальных серверов остаётся стандартный path с redirect-патчем.
"""

import os
import sys


def _rf_err(name):
    """Класс ошибки из redfish.rest.v1 (или динамический fallback для тестов)."""
    try:
        import redfish.rest.v1 as rv1
        return getattr(rv1, name)
    except Exception:
        return type(name, (Exception,), {})


def _login_failure(conn_obj, exc):
    """Понятная ошибка basic-auth c живой диагностикой HTTP-кодов.

    Возвращает исключение (вызывающий код должен его raise). В тексте:
      * GET /redfish/v1/ под Basic-auth — если вместо JSON приходит HTML
        ('<html', 'iLO.getBaseUrl'), значит Redfish Service Root на iLO4 2.8x
        ВЫКЛЮЧЕН/НЕДОСТУПЕН — лечится только апгрейдом firmware iLO или
        режимом `api: ilorest` в servers.yaml;
      * GET /redfish/v1/SessionService — виден статус сессионного сервиса.
    """
    import traceback
    txt = "".join(traceback.format_exception_only(type(exc), exc)).strip()
    lines = [f"[cr_basic_auth_patch] Basic-auth логин не удался: {txt}"]
    try:
        import requests
        host = getattr(getattr(conn_obj, "cli_args", None), "host", None)
        user = getattr(conn_obj, "username", None)
        pw = getattr(conn_obj, "password", None)
        if host and user and pw:
            for path in ("/redfish/v1/", "/redfish/v1/SessionService"):
                try:
                    r = requests.get(f"https://{host}{path}", auth=(user, pw),
                                     timeout=15, verify=False)
                    body = (r.text or "")[:160].replace("\n", " ")
                    looks_html = "<html" in body.lower() or "ilo.getbaseurl" in body.lower()
                    tag = "HTML вместо JSON — Redfish root НЕДОСТУПЕН (iLO4 2.8x)" \
                          if looks_html else "JSON/Redfish"
                    lines.append(f"  диагностика: GET {path} -> HTTP {r.status_code} "
                                 f"[{tag}] {body[:100]}")
                except Exception as de:
                    lines.append(f"  диагностика: GET {path} -> {type(de).__name__}: {de}")
            lines.append("  Если выше 'HTML вместо JSON': включите Redfish на iLO "
                         "(iLO Settings > Security Access Options) или обновите "
                         "firmware; временное решение — `api: ilorest` в servers.yaml.")
    except Exception:
        pass
    try:
        import redfish.rest.v1 as rv1
        return rv1.RetriesExhaustedError("\n".join(lines))
    except Exception:
        return RuntimeError("\n".join(lines))


def apply_patch():
    """Подменяет login_with_redirect_handling() на чистый Basic-auth. Идемпотентно."""
    try:
        from cr_module.classes import redfish as rf_mod
    except ImportError:
        return False

    RedfishConnection = rf_mod.RedfishConnection
    if getattr(RedfishConnection, "_cr_basic_auth_patched", False):
        return True

    def basic_only_login(self):
        # Никаких POST Sessions — чистый HTTP Basic-auth.
        # ВАЖНО: в новых версиях python-redfish у HttpClient.login параметр
        # `session_enabled` НЕ существует (там `auth=AuthMethod.SESSION|BASIC`);
        # старый вызов молча падал в TypeError и маскировался generic-ошибкой
        # check_redfish. Пробуем оба варианта API, при неудаче бросаем ПОНЯТНУЮ
        # ошибку с реальными HTTP-кодами диагностики.
        import redfish as rf_lib
        last_exc = None
        try:
            auth_basic = rf_lib.AuthMethod.BASIC
        except Exception:
            auth_basic = "basic"
        for kwargs in ({"auth": auth_basic}, {"session_enabled": False}):
            try:
                self.connection.login(
                    username=self.username,
                    password=self.password,
                    **kwargs,
                )
                print("[cr_basic_auth_patch] Логин выполнен через Basic-auth "
                      "(сессии отключены: CR_FORCE_BASIC_AUTH / basic_auth: true; "
                      f"параметр login: {next(iter(kwargs))}).",
                      file=sys.stderr)
                return
            except TypeError as te:
                # несовместимая сигнатура login() — пробуем следующий вариант
                last_exc = te
                continue
            except Exception as e:
                raise _login_failure(self, e) from e
        # оба варианта дали TypeError — достучаться до BMC не смогли
        raise _login_failure(self, last_exc if last_exc is not None
                            else RuntimeError("неизвестная сигнатура login()"))


    RedfishConnection.login_with_redirect_handling = basic_only_login
    RedfishConnection._cr_basic_auth_patched = True
    return True


def main():
    patched = apply_patch()
    if not patched:
        print("[cr_basic_auth_patch] WARNING: cr_module не найден, патч не применён",
              file=sys.stderr)
    else:
        print("[cr_basic_auth_patch] Режим Basic-auth включён (POST Sessions не используется).",
              file=sys.stderr)

    from check_redfish import main as cr_main
    try:
        cr_main()
    except SystemExit:
        raise
    except BaseException as e:
        # Если basic-auth тоже упал — добавляем диагностику соединения,
        # чтобы отличить «сеть/порт» от «учётки» от «firmware».
        text = f"{type(e).__name__}: {e}".lower()
        if ("unreachable" in text or "retries exhausted" in text
                or "unable to connect" in text or "authentication" in text):
            host = None
            argv = sys.argv[1:]
            for flag in ("-H", "--host"):
                if flag in argv and len(argv) > argv.index(flag) + 1:
                    host = argv[argv.index(flag) + 1]
                    break
            if host:
                try:
                    # переиспользуем сетевую диагностику из redirect-патча
                    import cr_login_redirect_patch as rp
                    print("[cr_basic_auth_patch] Диагностика соединения:", file=sys.stderr)
                    print(rp._network_diagnosis(host), file=sys.stderr)
                except Exception:
                    pass
        raise


if __name__ == "__main__":
    main()
