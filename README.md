# NAPI Factory Test

Клиент-серверная система производственного тестирования и прошивки до 50 плат NAPI-C (RK3308)
одновременно. Сервер: FastAPI + SQLite, веб: Vue 3, агент: Python на тестовой Armbian с microSD.

> **Статус.** Этап A (MVP с симулятором) готов и проверен end-to-end на 50 симулированных платах.
> Код этапов B и C (реальный агент, распознавание SD-NAND, потоковая прошивка, BOOT) написан за
> аппаратными интерфейсами и покрыт тестами на поддельных sysfs-данных и файлах-устройствах, но
> **на реальной NAPI-C не запускался**. Пока в профиле `hardware_validated: false`, реальный агент
> не выполняет разрушающих операций. См. [docs/boot-order.md](docs/boot-order.md) и [docs/safety.md](docs/safety.md).

## Быстрый старт (разработка, симулятор)

Нужны Python ≥ 3.11 и Node.js 22.

```sh
# веб
cd web && npm ci && npm run build && cd ..

# сервер
cd server && python3 -m venv .venv && .venv/bin/pip install -e '.[dev]'
NAPI_DB_PATH=/tmp/napi/factory.db NAPI_AGENT_TOKEN=dev-token-0123456789abcdef \
NAPI_ADMIN_PASSWORD=admin NAPI_ALLOW_SIMULATED=1 NAPI_IPERF_ENABLE=0 \
.venv/bin/python -m app.main &
cd ..

# 50 симулированных плат (в отдельном venv агента: pip install websockets==13.1 pyyaml)
python3 examples/simulated-agents/run.py --server ws://127.0.0.1:8080/ws/agent \
    --token dev-token-0123456789abcdef -n 50 --speed 10
```

Открыть http://127.0.0.1:8080, войти `admin` / `admin`, «Выбрать все online» → «Тесты + прошивка».

Параметры симулятора: `--fail-rate` (доля заданий с ошибкой), `--drop-rate` (обрывы связи),
`--boot-fail-rate` (платы без отчёта первой загрузки), `--speed`.

## Тесты

```sh
cd server && .venv/bin/python -m pytest -q     # протокол, API, BOOT, реконнект, планировщик iperf3
cd agent  && .venv/bin/python -m pytest -q     # распознавание SD-NAND на поддельных sysfs, очередь, задания, прошивка
cd web    && npx vitest run && npm run build
```

## Развёртывание

Копированием каталога, venv и одним systemd-юнитом: `sudo deploy/linux-systemd/install.sh`,
затем правка `/etc/napi-factory/server.env` (токен агентов, пароль) и
`systemctl enable --now napi-factory-server`. БД — один файл `/var/lib/napi-factory/factory.db`.
Смена пароля: `python -m app.main --set-password admin`. `deploy/compose.yaml` — опционально.

## Структура

| Каталог | Содержимое |
|---|---|
| `server/` | FastAPI: `/api/v1`, `/ws/agent`, `/ws/ui`, SQLite, планировщик, тесты |
| `web/` | Vue 3 + Vite + TypeScript; `BoardTile.vue`, `StageBar.vue` |
| `agent/` | агент платы: этапы, распознавание накопителей, очередь, Real/Mock бэкенды, тесты |
| `firstboot/` | `napi-firstboot-report` для финального образа (этап BOOT) |
| `config/` | профиль `napi-c-rk3308.yaml`, пример окружения сервера |
| `deploy/` | systemd-юнит, установка, avahi-сервис для mDNS, compose |
| `scripts/build-test-sd.sh` | манифест образа и сборка дерева файлов тестовой microSD |
| `examples/simulated-agents/` | симулятор N плат |
| `docs/` | [архитектура](docs/architecture.md), [API и протокол](docs/api.md), [microSD](docs/provisioning-sd.md), [порядок загрузки](docs/boot-order.md), [предохранители](docs/safety.md), [оператору](docs/operator-guide.md) |

## Не сделано (этап D и проверка на железе)

- Проверки на реальной плате из [docs/boot-order.md](docs/boot-order.md); все места `VALIDATE ON NAPI-C`.
- HTTP-fallback образа с сервера, привязка к портам коммутатора по SNMP.
- Обновление агента на картах с сервера (сейчас — перезапись карты).
