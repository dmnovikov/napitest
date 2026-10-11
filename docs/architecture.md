# Архитектура

```text
Браузер ── HTTP /api/v1 + WS /ws/ui ──┐
                                      │
             Linux-сервер: FastAPI (один процесс, systemd)
             ├─ SQLite /var/lib/napi-factory/factory.db (WAL)
             ├─ Factory: платы, задания, события, BOOT, таймауты
             ├─ NetScheduler + процессы iperf3 -s (по слоту на порт)
             └─ статика web/dist
                                      │
          WS /ws/agent (одно соединение на плату, Bearer-токен)
                                      │
             NAPI-C: тестовая Armbian на microSD + napi-factory-agent
             ├─ state.json и queue/ на записываемом разделе microSD
             ├─ этапы ram, net, sdnand, flash за интерфейсом Backend
             └─ RealBackend (sysfs, /dev, memtester, iperf3, zstd) | MockBackend (симулятор)
                                      │
             SD-NAND ── после прошивки: napi-firstboot-report → POST /api/v1/agent/boot_ok
```

## Сервер (`server/app`)

| Модуль | Назначение |
|---|---|
| `config.py` | переменные окружения (`Settings`), профиль платы (`Profile`), формирование `job.params` по режиму |
| `db/database.py`, `db/migrations/*.sql` | SQLite, миграции по `schema_version` при старте |
| `services/factory.py` | ядро: `hello`, heartbeat, события, итоги, серийники, BOOT, «Остановить», таймауты, рассылка в браузер |
| `services/reports.py` | карточка платы, партии, экспорт CSV/JSON, этикетка с QR |
| `services/auth.py` | операторы (PBKDF2), сессии |
| `scheduler/net.py`, `scheduler/iperf.py` | лимит одновременных iperf3-сессий, процессы `iperf3 -s` |
| `ws/agent.py`, `ws/ui.py`, `api/routes.py` | транспорт |

### Что хранится где

- **Память:** соединения агентов, последний heartbeat каждой платы, очередь слотов iperf3, ранние отчёты BOOT.
- **БД:** `device`, `batch`, `job`, `stage_result`, `event` (все события агентов, уникальны по `(stream, seq)`), `operator`, `session`, `image`, `test_sd_version`, `audit_log`, `meta` (счётчики серийников и партий), `schema_version`. Heartbeat в БД не пишется; `device.last_seen` обновляется при подключении, отключении и раз в минуту.

### Жизненный цикл задания

```text
queued ──(агент принял: heartbeat/job_started)──> running
running ──job_finished: тесты/прошивка PASS, BOOT включён──> awaiting_boot ──boot_ok──> pass
                                                                         └─таймаут──> fail (BOOT: no report)
running ──job_finished──> pass | fail | interrupted | cancelled
running ──нет связи дольше timeouts.job_s──> offline   (итог от агента после реконнекта заменяет offline)
```

Итог по этапам считает сервер (`verdict`): «PASS» агента без PASS всех включённых этапов считается FAIL.
Этапы, до которых завершённое задание не дошло, показываются как пропущенные.

### Устойчивость к обрывам

- Агент выполняет задание до конца без сервера; события пишутся в `queue/` и досылаются по порядку.
- Сервер подтверждает каждое событие `ack`; повтор `(stream, seq)` игнорируется.
- При реконнекте `hello.current_job` восстанавливает снимок; задание, помеченное `offline`, снова становится `running`.
- Событие применяется к плате своего задания (по `job_id`), даже если microSD переставили в другую плату.
- Отчёт первой загрузки, пришедший раньше итога задания, сохраняется и применяется, как только придёт итог FLASH.
- PASS по таймауту не объявляется никогда; потеря связи на экране серая, не красная.

## Агент (`agent/napi_factory_agent`)

| Модуль | Назначение |
|---|---|
| `main.py` | CLI, выбор бэкенда (`real` по умолчанию; `mock` только явно) |
| `ws_client.py` | соединение, реконнект, mDNS-fallback через `avahi-browse` |
| `queue.py` | офлайн-очередь событий: файл на событие, атомарная запись, лимит объёма (первыми выбрасываются `log`) |
| `runner.py` | задание: `state.json`, порядок этапов по режиму, отмена, восстановление после рестарта |
| `identity.py` | cpuid (DT serial → OTP nvmem → выданный сервером), MAC и его источник, machine-id |
| `hardware_detect.py` | inventory и распознавание SD-NAND: чистая функция над sysfs и mountinfo |
| `blockio.py` | открытие целевого устройства с проверкой `st_rdev` против sysfs, BLKFLSBUF, O_DIRECT |
| `images.py` | манифест образа, `zstd -l`, кэш самопроверки microSD |
| `stages/*.py` | RAM, NET, SD-NAND, FLASH |
| `backends.py` | `RealBackend` и `MockBackend` |

## Веб (`web/`)

Vue 3 + Vite + TypeScript. `BoardTile.vue` и `StageBar.vue` — квадрат-мини-карточка; данные приходят снимком и пачками изменений по `/ws/ui`.

## Симулятор (`examples/simulated-agents/run.py`)

Запускает N настоящих агентов с `MockBackend` в одном процессе: поддельные sysfs/proc, sparse-файл вместо SD-NAND, инъекция ошибок, обрывы связи, эмуляция BOOT. Сервер принимает симулированных агентов только при `NAPI_ALLOW_SIMULATED=1`; в сетке они помечены SIM.
