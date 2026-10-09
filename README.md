# MVP: автоматическая инвентаризация серверов через Redfish → NetBox

Docker-compose стек для автоматического сбора аппаратного инвентаря физических
серверов через **Redfish API** (HPE iLO, Dell iDRAC, Lenovo XCC) и синхронизации
его в **NetBox** (≥ 4.3) с использованием модели **Modules**
(Module Bay + Module Type + Module) вместо Inventory Items.

Все Module Bays, Module Types и Modules создаются автоматически — предзаполнять
справочники в NetBox **не нужно**.

---

## Архитектура

```
┌─────────────────────┐             ┌──────────────────────────┐
│  Redfish API BMC    │             │   NetBox (другая сеть)   │
│  (iLO / iDRAC / XCC)│             │      REST API ≥ 4.3      │
└─────────▲───────────┘             ▲──────────┬───────────────┘
          │ HTTPS                   │          │ read/write
          │ (redfish-network)       │          │ (netbox-network)
┌─────────┴───────────┐             │   ┌──────┴───────────────┐
│ check_redfish-      │             │   │ netbox-sync          │
│ collector           │             │   │ (ghcr.io/bb-ricardo/ │
│ servers_parser.py   │             │   │  netbox-sync:latest) │
│ цикл: CHECK_INTERVAL│             │   │ цикл: SYNC_INTERVAL  │
└─────────┬───────────┘             │   └──────▲───────────────┘
          │ пишет JSON              │           │ читает JSON (ro)
          ▼                        │            │
   ┌─────────────────────────────────────────────────┐
   │   volume inventory-data → /app/inventory/       │
   │   server01.json   server02.json   ...           │
   └─────────────────────────────────────────────────┘
```

Пайплайн:

```
Redfish API → check_redfish (inventory JSON) → общий volume → netbox-sync → NetBox
                                                                Device → Module Bays
                                                                       → Modules → Module Types
```

## Структура проекта

```
netbox-redfish-mvp/
├── docker-compose.yml            # два сервиса, volumes, networks, healthcheck
├── .env.example                  # шаблон настроек (копируется в .env)
├── .gitignore                    # .env и servers.yaml (секреты) не коммитятся
├── check_redfish/
│   ├── Dockerfile                # python:3.9-slim + check_redfish + pyyaml
│   ├── entrypoint.sh             # цикл сбора по CHECK_INTERVAL + heartbeat
│   ├── servers_parser.py         # обход servers.yaml, запуск check_redfish
│   └── servers.yaml.example      # шаблон списка серверов
├── netbox-sync/
│   ├── entrypoint.sh             # ожидание inventory, цикл SYNC_INTERVAL
│   └── settings.yaml             # конфигурация netbox-sync (без секретов)
└── README.md
```

---

## Пошаговая инструкция запуска

### Шаг 1. Подготовка конфигурации

```bash
cd netbox-redfish-mvp

# Настройки окружения и секреты
cp .env.example .env
nano .env          # заполнить REDFISH_*, NBS_NETBOX_*, при желании интервалы

# Список серверов
cp check_redfish/servers.yaml.example check_redfish/servers.yaml
nano check_redfish/servers.yaml
```

### Шаг 2. Создание API-токена в NetBox

1. Войдите в NetBox под учётной записью сервисного пользователя.
2. Откройте **Profile → API Tokens** (URL `/user/api-tokens/`) → **Add a Token**.
3. Укажите имя (например, `netbox-sync`), ключ скопируйте в `.env` →
   `NBS_NETBOX_API_TOKEN`.
4. Токену нужны права **read/write** на модели:
   - `dcim`: device, device type, manufacturer, site, role, platform,
     module bay, module type, module, interface, power port, power outlet,
     inventory item, location, rack;
   - `extras`: custom field, custom field value, tag;
   - `tenancy`: tenant; `circuits`: virtual circuit (если используются).
5. Рекомендуется отдельный пользователь `svc-netbox-sync`, а не личная учётная
   запись администратора.

### Шаг 3. Первый запуск (рекомендуется с DEBUG2)

Для первого прогона включите максимальную подробность логов — видно, как именно
объекты маппятся в NetBox и на чём спотыкается синхронизация:

```bash
# временное переопределение уровня логирования без правки .env:
NBS_COMMON_LOG_LEVEL=DEBUG2 docker compose up -d --build

docker compose logs -f check_redfish-collector   # ждём первый сбор (~60..300 с)
docker compose logs -f netbox-sync               # затем первая синхронизация
```

Когда всё отлажено — вернитесь к `NBS_COMMON_LOG_LEVEL=INFO` в `.env` и
пересоздайте контейнер: `docker compose up -d`.

### Шаг 4. Проверка результата

```bash
docker compose ps        # оба сервиса healthy (после start_period)

# JSON-файлы инвентаря в общем volume:
docker run --rm -v <project>_inventory-data:/inv alpine ls -l /inv
```

В NetBox откройте устройство → вкладка **Modules**: должны появиться Module Bays
(`CPU.Socket 1`, `DIMM 1`, `NIC.Slot 1`, `PSU Bay 1`, ...) с установленными
Modules и созданными Module Types (производитель, part number).

### Обычный рабочий цикл

- коллектор обновляет JSON каждые `CHECK_INTERVAL` (по умолчанию 14400 с = 4 ч);
- netbox-sync перечитывает volume и синхронизирует каждые `SYNC_INTERVAL`
  (по умолчанию 900 с = 15 мин);
- после старта контейнеров обе фазы начинаются с задержкой `STARTUP_DELAY`
  (по умолчанию 60 с);
- правки `servers.yaml` применяются со следующего цикла — перезапуск не нужен.

---

## Как добавить серверы в servers.yaml

Файл `check_redfish/servers.yaml` — простой список:

```yaml
servers:
  - name: server01                      # уникальное имя → /app/inventory/server01.json
    host: server01-ilo.example.com      # FQDN или IP BMC
    # port: 443                         # опционально, по умолчанию 443
    # username: separate-admin          # опционально: переопределяет креды из .env
    # password: separate-secret
    # netbox_device_id: 12              # опционально: числовой id устройства в NetBox
```

Правила:

- `name` — только буквы/цифры/`.`/`_`/`-`; дубликаты недопустимы.
- Учётные данные по умолчанию берутся из `.env` (`REDFISH_USERNAME/PASSWORD`);
  поля `username/password` у конкретного сервера перекрывают их (удобно для
  «чужих» BMC).
- `netbox_device_id` задавать **не обязательно**: без него устройство
  находится по серийному номеру (или Dell Service Tag).
- Удаление сервера из списка ⇒ его JSON будет удалён из volume, а при
  `prune_enabled: true` соответствующие объекты исчезнут и из NetBox
  (после prune-цикла).
- После сохранения файла ничего перезапускать не нужно — следующий цикл
  сбора подхватит изменения.

---

## Как данные маппятся в NetBox (режим Modules)

### Идентификация устройства (порядок попыток)

1. `meta.inventory_id` в JSON — если задан числом, матчинг строго по id устройства;
2. `inventory.system[0].serial` — серийный номер сервера;
3. Dell Service Tag (для iDRAC совпадает с serial).

### Структура объектов

```
Device: "server01" (найден по serial)
│
├── Module Bay: "CPU.Socket 1"        ← слот (стабильный идентификатор)
│   └── Module (status: active)       ← установленный компонент
│       ├── Module Type: "Intel Xeon Gold 6248R"
│       │     ├── manufacturer: "Intel"
│       │     └── part_number: "..."
│       ├── serial: "..."
│       └── custom_fields:
│             inventory_type: "CPU", inventory_size: "24/48" (cores/threads),
│             inventory_speed: "3.0GHz", health: "OK"
│
├── Module Bay: "CPU.Socket 2" → Module → тот же Module Type
│
├── Module Bay: "DIMM 1" .. "DIMM 24"
│   └── Module → Module Type "M393A4K40CB2-CVF" (Samsung)
│       custom_fields: inventory_type=DIMM, size=32GB, speed=2933MHz, health
│
├── Module Bay: "NIC.Slot 1"
│   └── Module "HPE Ethernet 10Gb 2-port"
│       ├── Interface "NIC.Slot.1-1"    ← привязана к module
│       └── Interface "NIC.Slot.1-2"
│
├── Module Bay: "PSU Bay 1"
│   └── Module "865414-B21"
│       └── Power Port "Power Supply 1" ← привязан к module
│
└── Module Bay: "iLO 5"
    └── Module "iLO 5"
        └── Interface "iLO 5 (mgmt)"    ← mgmt_only: true
```

### Custom Fields (создаются netbox-sync автоматически)

Уровень **Device**:

| Поле | Пример значения | Источник |
|---|---|---|
| `host_cpu_cores` | `48 Intel Xeon Gold 6248R` | сумма ядер CPU |
| `host_memory` | `768 GB` | суммарный объём DIMM |
| `power_state` | `On` | `PowerState` из Redfish |
| `health` | `OK` | агрегированный Status |
| `service_tag` | `ABC1234` | только Dell |

Уровень **Module / Inventory Item**:

| Поле | Пример | Комментарий |
|---|---|---|
| `firmware` | `2.84` | версия прошивки компонента |
| `inventory_type` | `CPU / DIMM / NIC / Power Supply / Physical Drive` | категория |
| `inventory_size` | `32GB`, `24/48`, `500W`, `480GB` | размер/ёмкость |
| `inventory_speed` | `2933MHz`, `3.0GHz`, `25Gbit/s` | скорость |
| `health` | `OK / Warning / Critical / Absent` | Status компонента |

---

## Переменные окружения (`.env`)

| Переменная | По умолчанию | Назначение |
|---|---|---|
| `REDFISH_USERNAME` / `REDFISH_PASSWORD` | — | общие креды BMC (обязательно) |
| `CHECK_INTERVAL` | `14400` | период сбора инвентаря, сек |
| `SYNC_INTERVAL` | `900` | период синхронизации, сек |
| `STARTUP_DELAY` | `60` | пауза перед первым прогоном, сек |
| `NBS_NETBOX_HOST_FQDN` | — | адрес NetBox без `https://` (обязательно) |
| `NBS_NETBOX_API_TOKEN` | — | API-токен NetBox (обязательно) |
| `NBS_COMMON_LOG_LEVEL` | `INFO` | `DEBUG2` для первой отладки |

Безопасность: `.env` и `check_redfish/servers.yaml` исключены из git
(`.gitignore`); пароли передаются только через окружение/монтируемый конфиг —
в образах и в репозитории секретов нет.

---

## Troubleshooting

### 1. Нет доступа к BMC (timeout / connection refused / SSL error)

Симптомы в логах коллектора: `ОШИБКА подключения (rc≠0)`,
`Таймаут — BMC не ответил`.

Проверки:

```bash
# С хоста, где крутится docker:
nc -vz server01-ilo.example.com 443
curl -sk https://server01-ilo.example.com/redfish/v1/ | head -30

# Изнутри контейнера collector (сеть redfish-network):
docker compose exec check_redfish-collector \
    curl -sk https://<BMC-FQDN>/redfish/v1/ | head -30
```

Типичные причины:
- BMC недоступен из сети контейнера (firewall/VLAN) — проверьте маршруты; при
  необходимости подключите сервис к внешней/docker-сети с доступом к OOB;
- не резолвится DNS BMC — добавьте `extra_hosts` в docker-compose или
  используйте IP в `servers.yaml`;
- неверные креды → HTTP 401 в выводе check_redfish; убедитесь, что пользователю
  BMC разрешён Redfish (роль Administrator/Operator);
- сессии iDRAC/iLO «Session lockout» — параметр `--nosession` уже добавлен
  парсером, это снижает риск блокировок.

Сервер с ошибкой пропускается, остальные обрабатываются штатно; цикл не падает.

### 1a. BMC отвечает редиректом: «Unsafe redirect location» / «max retries exhausted» / curl даёт `HTTP/1.1 308 Moved Permanently`

Это поведение Lenovo XCC (и части прошивок iDRAC/iLO): BMC отвечает **3xx-редиректом**
(часто 308 Permanent Redirect) на Redfish-URL, причём заголовок `Location` указывает
на **тот же самый путь** (само-редирект). Stock check_redfish такие ответы не понимает:
валидатор редиректа падает с `Unsafe redirect location`, а redfish-клиент — с
`max retries exhausted` (rc=2).

В коллекторе встроен патч `cr_login_redirect_patch.py` (включён по умолчанию), который:
- резолвит относительный `Location` и повторяет POST Sessions на целевой URL;
- при само-редиректе повторяет запрос ещё раз (XCC после «прогревающей» попытки обычно
  отдаёт `201 Created` + заголовок `X-Auth-Token`);
- для GET-запросов вручную проходит цепочку 3xx-редиректов (лимит — env `CR_MAX_REDIRECTS`,
  по умолчанию 5) вместо падения requests;
- если токен так и не получен — автоматически переключается на Basic-auth fallback;
- печатает реальные коды ответов и `Location` в stderr (видно в `docker logs`).

Диагностика вашего BMC из контейнера (покажет, что именно отвечает firmware):

```bash
# 1) Включить preflight-диагностику collectors-а (печатает GET root и POST Sessions с Location):
docker compose exec check_redfish-collector sh -c \
  'CR_PREFLIGHT=1 python3 -m cr_login_redirect_patch -H <BMC_IP>:443 -u <user> -p "<pass>" --all --inventory --inventory_file /tmp/t.json --nosession'

# 2) Ручной curl-эквивалент (обратите внимание на < location: и код второй попытки):
docker compose exec check_redfish-collector curl -sk -i -X POST \
  https://<BMC_IP>/redfish/v1/SessionService/Sessions \
  -d '{"UserName":"admin","Password":"..."}' --max-redirs 3 -w '\n--- retry ---\n' \
  -H 'Content-Type: application/json'
```

Если после обновления образа ошибка осталась — проверьте, что образ пересобран
(`docker compose build check_redfish-collector && docker compose up -d`), и временно
увеличьте ретраи/таймауты в `.env`: `CR_CONN_RETRIES=8`, `CR_REQ_TIMEOUT=60`.
Отключить патч целиком: `CR_FIX_REDIRECT=0` (не рекомендуется для XCC).

**Крайний случай: 308-само-редирект, токен не выдаётся никогда.** Если в логах видно
`POST .../Sessions -> HTTP 308, Location: .../Sessions` (Location == тот же URL) и
collect падает с rc=2 даже с redirect-патчем — прошивка BMC заворачивает ЛЮБОЙ POST
на Sessions в себя, и сессионный логин невозможен в принципе. Наблюдались два класса
таких устройств: **HPE iLO4 (fw 2.82)** и **Lenovo XCC** — у iLO4 это типично при
выключенном/переполненном Redfish Session Service или после серии неудачных входов.
В этом случае включите режим чистого Basic-auth (без POST Sessions вообще):

```yaml
# servers.yaml — точечно по проблемному серверу (пример: iLO4 2.82):
servers:
  - name: test-01
    host: 192.168.10.37
    basic_auth: true      # сбор через Authorization: Basic, сессии не создаются
```

или глобально для всех серверов — `CR_FORCE_BASIC_AUTH=1` в `.env`. Реализация —
модуль `cr_basic_auth_patch.py`; GET-запросы к `/redfish/v1/*` под Basic-auth на
таких BMC работают штатно. При падении basic-auth коллектор дополнительно печатает
DNS/TCP-диагностику хоста. Быстрая проверка «жив ли» Basic-auth на вашем BMC из
контейнера collector:

```bash
docker compose exec check_redfish-collector curl -sk -u Administrator:ПАРОЛЬ \
  https://192.168.10.37/redfish/v1/Systems | head -c 200
# JSON с Members => basic-auth работает, ставьте basic_auth: true и пересоберите образ
```

**Если basic-auth тоже не помогает (наблюдавшийся случай: iLO4 fw 2.82).** Признак:
в ответ на `curl https://IP/redfish/v1/` вместо JSON приходит **HTML веб-консоли iLO**
(фрагменты `iLO.getBaseUrl()`, `showLogin(...)`, `<iframe id=appFrame>`). Это значит,
что Redfish Service Root на прошивке недоступен в принципе — любые пути `/redfish/v1/*`
редиректятся на HTML-логин, POST Sessions отвечает 308-само-редиректом, и никакой
Redfish-патч не поможет. Два пути:

1. Правильный: обновить firmware iLO4 (Redfish стабилен с 2.50+, убедитесь, что
   Redfish включён: *iLO Rest (Agentless Management)* / Embedded Directory Services)
   и/или сбросить зависшие сессии (`POST /json/launch_priv_ilorest_session` не нужен —
   достаточно перезагрузки iLO через «Reset iLO»);
2. Рабочий без апгрейда: режим **ilorest** (HPE REST API, не зависит от Redfish Session
   Service) — пометьте сервер в `servers.yaml`:

```yaml
servers:
  - name: test-01
    host: 192.168.10.37
    api: ilorest          # сбор через HPE ilorest вместо Redfish
    # netbox_device_id: 240  — опционально, как обычно
```

Коллектор сам установит/найдёт `ilorest` (в собранном из этого репозитория image он
ставится на этапе build), выполнит `serverinfo/processor/memory/networkadapter` и
запишет стандартный inventory-JSON в `/app/inventory/<name>.json` — netbox-sync не
различает источник. Ограничения MVP режима ilorest: собираются system/CPU/DIMM/NIC
(CPU и DIMM могут попасть в один тип компонента), PSU/диски/cooling — пока нет;
health выставляется OK при успешном сборе. Проверка вручную из контейнера:

```bash
docker compose exec check_redfish-collector ilorest --url=192.168.10.37 \
  --login=Administrator --password=ПАРОЛЬ --selector=OemHpProfileSet ilorest serverinfo
# JSON c "SerialNumber"/"PowerOnDate" => ilorest работает, ждите следующий цикл или
# перезапустите collector
```

### 2. Устройство не найдено в NetBox / создаётся дубль

- netbox-sync ищет устройство по **serial**. Убедитесь, что у существующего
  Device в NetBox поле *Serial number* заполнено ровно как в Redfish
  (регистр/пробелы значимы): `GET /api/dcim/devices/?serial=<SN>`;
- если serial отсутствует или не уникален — зафиксируйте соответствие: возьмите
  id устройства (`/api/dcim/devices/<id>/`) и пропишите `netbox_device_id` в
  `servers.yaml` (значение попадёт в `meta.inventory_id` JSON);
- «создаётся второй Device вместо обновления» — частое следствие смены serial на
  BMC (замена системной платы): исправьте serial в NetBox либо задайте
  `netbox_device_id`;
- в логе `DEBUG2` ищите строки вида `Matching device ... by serial/id` — видно,
  что искалось и что нашлось.

### 3. Ошибки синхронизации netbox-sync

- `HTTP 403 / Insufficient privileges` — токен без write-прав или не хватает
  permissions на конкретные модели (module type, custom field). См. Шаг 2;
- `HTTP 400 ... already exists` для Module Type — конфликт одноимённых типов
  разных вендоров: оставьте как есть (netbox-sync различает по
  manufacturer+model) или удалите вручную созданный дубль из NetBox;
- `Custom field ... does not exist` при первом запуске — нормально: поля
  создаются автоматически в первом цикле, ошибка уходит на следующем прогоне;
- `Connection refused / timeout` до NetBox — проверьте из контейнера:
  ```bash
  docker compose exec netbox-sync sh -c \
    "wget -qO- https://$NBS_NETBOX_HOST_FQDN/api/status/"
  ```
  при самоподписанном сертификате временно поставьте `tls_verify: false` в
  `settings.yaml`;
- контейнер в статусе `unhealthy` — смотрите
  `docker compose logs --since 30m netbox-sync`; обычно это повторяющаяся
  ошибка API; сам контейнер жив и retry выполняется каждый цикл;
- «каталог пуст, ничего не синхронизируется» — коллектор ещё не завершил
  первый проход (ждёт `STARTUP_DELAY` + время опроса всех BMC). entrypoint
  sync сам ожидает появления JSON-файлов — вмешательство не требуется.

### 4. Полезные команды

```bash
docker compose ps                            # статусы и healthcheck
docker compose logs -f --tail=100 check_redfish-collector
docker compose logs -f --tail=100 netbox-sync
docker compose restart netbox-sync           # принудительный новый цикл sync
docker compose down -v                       # полный сброс (УДАЛИТ volumes!)
```

---

## Ограничения MVP

- Один набор общих кредов BMC + точечные переопределения (нет интеграции с
  Vault/secret manager).
- Prune удаляет из NetBox только то, что управляется данным источником.
- Проверено на серверах HPE/Dell/Lenovo с корректной реализацией в Redfish
  ресурсов `Processor`, `Memory`, `Ethernet/Circuit`, `PowerSupply`, `Storage`,
  `ManagerForService`.
