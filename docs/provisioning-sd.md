# Подготовка тестовых microSD

**Основной способ:** на стенде страница «microSD» → «Скачать образ microSD» → прошить файл на каждую
карту через balenaEtcher. Образ собирает сервер (docs/api.md, раздел 6); агент потом обновляется
с сервера сам. Базу образа делает инженер из проверенной карты: `scripts/make-sd-base.py`.

Ниже — прежний ручной способ (эталонная карта и клонирование).

Одна эталонная карта собирается и проверяется, затем клонируется на остальные. Клоны допустимы:
cpuid берётся из OTP платы, `machine-id` агент регенерирует при первом запуске.

## 1. Тестовая Armbian

- Armbian для NAPI-C (RK3308), ядро с `CONFIG_MEMTEST=y`.
- В `/boot/armbianEnv.txt`: `extraargs=memtest=4` (значение из профиля `ram.memtest_passes`).
- Пакеты: `python3 (>= 3.11)`, `python3-websockets` (версия 10–13: агент использует `extra_headers` старого клиента; проверено на 13.1), `python3-yaml`, `memtester`, `iperf3`, `zstd`, `ethtool`, `avahi-utils` (для mDNS-fallback).
- Записываемый раздел для `/var/lib/napi-factory` (state.json, очередь событий, кэш самопроверки).

## 2. Финальный образ и манифест

```sh
scripts/build-test-sd.sh manifest napi-c-armbian-2026.10.0.img.zst 2026.10.0
```

Манифест содержит размер и SHA-256 сжатого файла, размер и SHA-256 распакованного образа
(сверяется с zstd frame header), профиль. Версия образа должна совпадать с `flash.image_version` профиля.

Требования к финальному образу: U-Boot с microSD раньше SD-NAND ([boot-order.md](boot-order.md)),
юнит `firstboot/napi-firstboot-report.service` и скрипт `napi-firstboot-report.py` в
`/usr/local/sbin/`, конфиг `/etc/napi-factory/firstboot.conf` (из `firstboot/firstboot.conf.example`:
адрес сервера, токен, версия образа).

## 3. Сборка дерева файлов и установка на эталонную карту

```sh
cp agent/agent.example.yaml /tmp/agent.yaml   # задать server_url и token
scripts/build-test-sd.sh stage /tmp/napi-stage \
    --image napi-c-armbian-2026.10.0.img.zst --agent-config /tmp/agent.yaml --test-sd-version 2026.10.1
# смонтировать rootfs эталонной карты, например в /mnt/testsd (проверить lsblk!)
sudo scripts/build-test-sd.sh install /mnt/testsd /tmp/napi-stage
```

Скрипт пишет только в указанный каталог смонтированного rootfs и требует ввести `YES`; блочные
устройства он не выбирает и не пишет. Затем на карте: `systemctl enable napi-factory-agent`.

## 4. Проверка эталонной карты

Вставить в плату, дождаться квадрата в браузере. В карточке платы: версия тестовой microSD,
самопроверка `pass`, распознанная SD-NAND с обоснованием, cpuid с источником. Прогнать «Только тесты».

## 5. Клонирование

Снять образ эталонной карты и записать на остальные карты на ПК инженера, каждый раз проверяя
устройство по `lsblk` (модель, размер, серийник кардридера). Номер `/dev/sdX` в скрипты не вписывать.
Самопроверка хешей на клоне выполняется один раз при первом запуске и кэшируется.

## 6. Обновление

Агент и сценарии обновляются на всех картах только перезаписью карты (этап D: доставка с сервера).
Новая версия финального образа — новая версия тестовой microSD (`VERSION`) и профиля.
