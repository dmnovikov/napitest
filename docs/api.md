# API и протокол

Все метки времени: UTC, ISO 8601 (`2026-10-10T12:00:00Z`) в REST и БД; в WebSocket-сообщениях агента `ts` это Unix-время в секундах (float).
Имена этапов: `ram`, `net`, `sdnand`, `flash`, `boot`.
Состояние этапа `state` ∈ `pending | running | pass | fail | skipped | error | interrupted`.
Режимы: `test_only`, `test_flash`, `flash_only`.

## 0. Обнаружение сервера: UDP 18089

В образах (тестовая microSD, финальный образ) IP сервера не записан. Агент и отчёт первой загрузки шлют
широковещательно на `255.255.255.255:18089` (`NAPI_DISCOVERY_PORT`, 0 выключает):

```json
{"type":"napi_discover","v":1}
```

Сервер отвечает отправителю адресом, по которому он виден из его подсети (или `NAPI_PUBLIC_URL`):

```json
{"type":"napi_server","v":1,"http":"http://192.168.30.56:8080","ws":"ws://192.168.30.56:8080/ws/agent","version":"0.1.0"}
```

Порядок у агента: `server_url` из конфига (если задан) → UDP-запрос → mDNS `_napi-factory._tcp`.
Найденный адрес запоминается; после 3 неудачных подключений поиск повторяется. Токен в обмене не участвует.
Порт 18089/udp должен быть открыт во входящих на сервере (как 8080/tcp и порты iperf3).

## 1. WebSocket агента: `GET /ws/agent`

Аутентификация: заголовок `Authorization: Bearer <agent_token>` (или `?token=` в URL). Неверный токен → закрытие с кодом 4401.
Все сообщения — JSON-объекты с полем `type`. Одно соединение на плату.

### 1.1 Агент → сервер

**hello** — первое сообщение после подключения.

```json
{
  "type": "hello",
  "proto": 1,
  "cpuid": "a3f2…" ,            // null, если OTP недоступен и ID ещё не выдан
  "id_source": "otp-verified",  // otp-verified | otp-cpuid | dt-serial | assigned | none
  "mac": "02:11:22:33:44:55",
  "mac_source": "cpuid-derived",// cpuid-derived | random | unknown
  "mac_unique_hint": true,      // агент проверил, что MAC не из пула «клонированных» значений
  "ip": "192.168.10.57",
  "hostname": "napi-test",
  "agent_version": "0.1.0",
  "test_sd_version": "2026.10.1",
  "kernel": "6.6.x-rockchip64",
  "ram_bytes": 536870912,
  "has_led": false,
  "simulated": false,           // true только у mock-бэкенда
  "inventory": { … },           // результат распознавания накопителей, см. 1.4
  "sd_selfcheck": {"state": "pass", "detail": "cached"},
  "stream": "8c1e…",            // id потока событий (uuid4, постоянный для очереди на этой microSD)
  "current_job": null            // или снимок незавершённого/последнего задания, см. heartbeat
}
```

**heartbeat** — раз в 3 с, полный снимок.

```json
{"type":"heartbeat","job_id":"j_…","mode":"test_flash","elapsed":134,
 "finished": false, "result": null,
 "stages":{"ram":{"state":"pass","t":41,"detail":"kernel memtest 0 err, memtester 2 pass"},
           "net":{"state":"pass","t":18,"detail":"100Full, 94 Mbit/s"},
           "sdnand":{"state":"running","pct":43,"detail":"паттерн 43 %"},
           "flash":{"state":"pending"},"boot":{"state":"pending"}}}
```

Без задания: `job_id: null`, `stages: {}`. Этап, пройденный с замечаниями, несёт `"warn": true`.

**Замечания (`warnings`)** — PASS с особенностью, которую надо видеть в отчёте (например, NET: TCP-приём ниже порога при нормальном UDP-приёме). На итог задания не влияют; в UI показываются ⚠, в CSV колонка `warnings`. `result` (когда `finished: true`) ∈ `pass | fail | interrupted | cancelled`.

**event** — значимые события; буферизуются на microSD и досылаются после реконнекта в порядке `seq`.

```json
{"type":"event","stream":"8c1e…","seq":17,"ts":1760097600.5,"job_id":"j_…",
 "kind":"stage_finished","stage":"ram","data":{…}}
```

| kind | stage | data |
|---|---|---|
| `job_started` | – | `{"mode": "..."}` |
| `stage_started` | да | `{}` |
| `stage_finished` | да | `{"state","t","detail","measurements":{…},"warnings":["…"],"log":"последние строки"}` |
| `log` | да/– | `{"lines": ["…"]}` |
| `job_finished` | – | `{"result","stages":{снимок},"detail"}` |

Сервер обрабатывает события идемпотентно по `(stream, seq)` и подтверждает `ack`. Агент удаляет из очереди всё с `seq ≤ ack.seq`.

**net_request / net_release** — слот iperf3 (планировщик ограничивает параллельность):

```json
{"type":"net_request","job_id":"j_…"}
{"type":"net_release","job_id":"j_…"}
```

**serial_request** — серийник для provision внутри FLASH:

```json
{"type":"serial_request","job_id":"j_…"}
```

### 1.2 Сервер → агент

```json
{"type":"hello_ack","device_id":12,"cpuid":"a3f2…","serial":"NC-2610-0017","server_time":1760097600.0}
{"type":"job","job_id":"j_…","mode":"test_flash","params":{…профиль, см. 1.3…}}
{"type":"cancel","job_id":"j_…"}
{"type":"ack","stream":"8c1e…","seq":17}
{"type":"net_grant","job_id":"j_…","host":"192.168.10.1","port":5202}
{"type":"serial","job_id":"j_…","serial":"NC-2610-0017","batch":"B-20261010-01","date":"2026-10-10"}
{"type":"serial","job_id":"j_…","error":"device has no TEST_PASS"}
{"type":"identify","seconds":10}
{"type":"error","detail":"…"}
```

Если в `hello` `cpuid == null`, сервер выдаёт `cpuid` вида `A<16 hex>` в `hello_ack`; агент сохраняет его на microSD и далее шлёт `id_source: "assigned"`.

Повторная выдача `job` с тем же `job_id` не перезапускает задание: агент отвечает текущим снимком. Пока у агента есть активное задание с другим `job_id`, новое отклоняется (`event kind=log` с причиной), сервер не выдаёт второе задание.

### 1.3 Параметры задания (`job.params`)

Формируются сервером из профиля `config/napi-c-rk3308.yaml` и режима. Агент не читает профиль сам.

```json
{
  "profile": "napi-c-rk3308",
  "destructive_enabled": false,
  "stages": {"ram": true, "net": true, "sdnand": true, "flash": true, "boot": true},
  "ram":    {"expected_mib": 512, "tolerance_mib": 64, "memtest_required": true,
             "memtester_reserve_mib": 160, "memtester_max_mib": 256, "memtester_passes": 1, "timeout_s": 900},
  "net":    {"iface": "end0", "expect_speed": 100, "expect_duplex": "full",
             "iperf_seconds": 10, "min_mbps": 80, "grant_timeout_s": 600, "timeout_s": 900,
             "max_rx_errors": 0, "max_tx_errors": 0},
  "sdnand": {"pattern": "quick",              // quick | full | beyond_image | skip
             "quick_ranges": 3, "range_mib": 64, "block_kib": 1024, "timeout_s": 3600,
             "detect": {…см. профиль…}},
  "flash":  {"image_version": "2026.10.0", "image_file": "napi-c-armbian-2026.10.0.img.zst",
             "manifest_file": "napi-c-armbian-2026.10.0.manifest.json",
             "block_kib": 4096, "timeout_s": 3600,
             "provision": {"method": "file", "offset_bytes": 0, "size_bytes": 0}},
  "boot":   {"enabled": true}
}
```

### 1.4 Inventory

```json
{"devices":[{"name":"mmcblk0","type":"MMC","size_bytes":3909091328,"controller":"ff490000.mmc",
             "by_path":"platform-ff490000.mmc","modalias":"mmc:block","removable":false,"ro":false,
             "is_boot":false,"mounted":[]}],
 "target":"mmcblk0","boot_device":"mmcblk1",
 "reasons":["mmcblk1: источник загрузки (/ на mmcblk1p2)","mmcblk0: контроллер ff490000.mmc ожидаемый, размер 3.64 GiB в допуске"],
 "ok":true}
```

## 2. Первая загрузка (этап BOOT)

По умолчанию (`boot.method: network`) финальный образ пишется **без изменений**, и BOOT проверяется снаружи:
пока задание в `awaiting_boot`, сервер раз в `probe_s` шлёт по UDP-пакету на каждый адрес подсети платы
(чтобы заполнить ARP), ищет в `/proc/net/arp` IP с MAC платы (MAC NAPI-C выводится из cpuid и одинаков в
тестовой и финальной ОС) и читает SSH-баннер на `probe_port`. Если SSH отвечает `confirm_s` секунд, а агент
тестовой microSD не подключился, BOOT: pass (`measurements`: `method`, `ip`, `mac`, `ssh_banner`).
Агент снова на связи после выключения: плата загрузилась с microSD, текст этапа просит вынуть карту.
Таймаут — не дефект (оператор мог не успеть): итог PASS, этап `boot` в состоянии `skipped` с замечанием
`BOOTCHECK: загрузка не подтверждена` (или `… — плата загрузилась с microSD`). Если плата появится с SD-NAND
в течение 2 ч, этап становится `pass`, замечание снимается. Опция запуска `boot: skip` убирает этап совсем.
Старые записи «таймаут BOOT = FAIL» при старте сервера пересчитываются по этому правилу.
Сбои стенда (нет очереди iperf3, сервер не ответил/отказал при FLASH) дают итог `incomplete`, а не `fail`.
Серийник при `serial.source: cpuid` равен ID чипа (в ОС: `/proc/device-tree/serial-number`) и в плату не пишется.

`boot.method: report` (прежняя схема) использует отчёт из образа:

### 2.1 `POST /api/v1/agent/boot_ok`

Шлёт `napi-firstboot-report` из финального образа. Заголовок `Authorization: Bearer <agent_token>`.

```json
{"cpuid":"a3f2…","image_version":"2026.10.0","uptime":23.5,"serial":"NC-2610-0017"}
```

Ответ `{"ok": true, "job_id": "j_…"}` закрывает этап BOOT активного задания платы (статус `awaiting_boot`). `{"ok": true, "job_id": null}` если ожидающего задания нет (событие всё равно журналируется). Юнит отключает себя только после `ok: true`.

Поздний отчёт: если ожидание истекло (`BOOT: no report`) и все остальные этапы последнего задания платы прошли, отчёт переводит задание в `pass` с замечанием `BOOT: отчёт пришёл после таймаута (+N мин)`; ответ содержит `job_id` этого задания. Если упал другой этап, отчёт только журналируется.

## 3. REST оператора `/api/v1`

Аутентификация: `POST /api/v1/auth/login {"username","password"}` ставит cookie `napi_session`. Остальные эндпоинты без сессии → 401.

| Метод | Путь | Назначение |
|---|---|---|
| POST | `/auth/login` | вход |
| POST | `/auth/logout` | выход |
| GET | `/auth/me` | `{"username"}` |
| GET | `/state` | полный снимок для главного экрана (как `snapshot` в UI WS) |
| POST | `/batches` | запуск: `{"mode","device_ids":[…],"confirm":true,"kind":"production"|"golden","options":{"sdnand_pattern":"quick"|"full","memtester":"quick"|"full","net":"quick"|"full"}}` (net full: ещё TCP iperf3; quick: только UDP-поток на udp_rate_mbps по udp_seconds в каждую сторону) |
| POST | `/batches/{id}/stop` | «Остановить»: `{"device_ids":[…]}` (пусто = вся партия) |
| POST | `/stop` | «Остановить» для выбранных плат: `{"device_ids":[…]}` |
| GET | `/batches` | история партий |
| GET | `/batches/{id}` | детали партии с заданиями |
| GET | `/batches/{id}/export.csv`, `/batches/{id}/export.json` | отчёт |
| GET | `/devices/{id}` | карточка платы |
| POST | `/devices/{id}/identify` | мигание LED |
| POST | `/devices/{id}/golden` | `{"is_golden": true}` |
| GET | `/devices/{id}/label` | HTML-этикетка с QR (только при PASS и наличии серийника) |
| GET | `/jobs/{job_id}` | задание: этапы, измерения, лог, события |
| GET | `/audit` | журнал действий |
| PUT | `/expected` | `{"count": N}`: сколько плат на стенде (0: не задано); в Summary поле `expected` |

Разрушающие режимы (`test_flash`, `flash_only`) требуют `"confirm": true`, иначе 400.
Произвольные команды API не принимает.

### 3.1 DeviceView

```json
{
  "device_id": 12, "display_no": 17, "cpuid": "…", "cpuid_tail": "A3F2", "id_source": "dt-serial",
  "serial": "NC-2610-0017", "online": true, "last_seen": "2026-10-10T12:00:03Z",
  "ip": "…", "mac": "…", "mac_source": "…", "agent_version": "…", "test_sd_version": "…",
  "kernel": "…", "ram_bytes": 536870912, "has_led": false, "simulated": false, "is_golden": false,
  "tile": "ready",  // ready | busy | awaiting_boot | pass | fail | offline | interrupted
  "job": {
    "job_id": "j_…", "batch_id": 3, "mode": "test_flash", "status": "running",
    "started_at": "…", "finished_at": null, "elapsed": 134, "boot_deadline": null,
    "stages": {"ram":{"state":"pass","t":41,"detail":"…"}, "net":{…}, "sdnand":{…}, "flash":{…}, "boot":{…}},
    "result": null
  }
}
```

Статусы задания (`job.status`): `queued | running | awaiting_boot | pass | fail | interrupted | cancelled | offline`.

### 3.2 Summary

```json
{"total":50,"online":48,"busy":30,"pass":10,"fail":2,"offline":2,"interrupted":0,
 "batch":{"batch_id":3,"name":"B-20261010-01","mode":"test_flash","kind":"production","operator":"admin",
          "started_at":"…","finished_at":null,"golden":"pass"},
 "versions":{"test_sd":["2026.10.1"],"image":"2026.10.0","server":"0.1.0"},
 "operator":"admin","server_time":"…"}
```

## 4. WebSocket браузера: `GET /ws/ui`

Cookie-сессия обязательна. Сообщения сервер → браузер:

```json
{"type":"snapshot","devices":[DeviceView…],"summary":Summary}
{"type":"devices","devices":[DeviceView…]}     // изменившиеся платы, пачкой раз в ≤ 0.5 с
{"type":"summary","summary":Summary}
```
