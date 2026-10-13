# Аппаратные предохранители

Главный риск системы — записать образ или паттерн не на тот накопитель. Ниже все уровни защиты.

## 1. Профиль не валидирован → разрушающих операций нет

`config/napi-c-rk3308.yaml: hardware_validated: false`. Сервер передаёт агенту
`params.destructive_enabled = hardware_validated`. Реальный агент (`--backend real`) при
`destructive_enabled: false` не выполняет паттерн-тест SD-NAND и прошивку: этап завершается FAIL
с `SD: destructive disabled (profile not validated)` или `FLASH: destructive disabled…`.

Ставить `hardware_validated: true` можно только после проверок из [boot-order.md](boot-order.md)
и раздела 4 ниже на реальной NAPI-C. `NAPI_DESTRUCTIVE_OVERRIDE=1` на сервере существует только для
отладки на одной плате инженером; в production не задавать.

## 2. Распознавание SD-NAND (`agent/napi_factory_agent/hardware_detect.py`)

Цель — единственное устройство `mmcblk*`, у которого одновременно:

- оно **не** источник загрузки: устройство корня `/` определяется по `major:minor` из `/proc/self/mountinfo` → `/sys/dev/block/M:m` → родительский диск;
- нет смонтированных разделов (проверяются и диск, и все его разделы);
- не read-only;
- тип из `allowed_types`, размер в `[size_min_bytes, size_max_bytes]` (около 4 GB, учтены GB и GiB);
- контроллер (резолв symlink `device`) равен `expected_controller` при `require_controller_match: true`.

Номер `/dev/mmcblkN` не используется как признак. Ноль или больше одного кандидата — FAIL без записи.
Обоснование (`reasons`) попадает в карточку платы.

## 3. Перед записью

- Распознавание повторяется непосредственно перед паттерн-тестом и перед прошивкой.
- Устройство открывается по имени из sysfs, затем `fstat(fd).st_rdev` сверяется с `major:minor` из sysfs: подмена или переименование между проверкой и открытием даёт отказ.
- Размер распакованного образа (манифест и `zstd -l`) сверяется с ёмкостью.
- Паттерн-тест не выходит за ёмкость; в режиме «Тесты + прошивка» проверяется только область за концом образа.

## 4. Сервер

- Разрушающие режимы требуют `confirm: true` (диалог подтверждения в UI).
- Агентам не передаются команды shell; API их не принимает. Аутентификация агента — общий токен, оператора — сессия.
- В «Тесты + прошивка» FLASH выполняется только после PASS RAM, NET, SD-NAND; серийник для provision выдаётся только после TEST_PASS в этом задании.
- В «Только прошивка» серийник выдаётся только плате с TEST_PASS в истории (`flash.flash_only_requires_tested`).
- Симулированные агенты отклоняются, если не задан `NAPI_ALLOW_SIMULATED=1`.

## 5. Проверено на плате (`CHECKED`)

Места, зависящие от железа, проверены на NAPI-C и помечены `CHECKED` (`grep -rn "CHECKED" agent config`):

- контроллер SD-NAND `ff490000.mmc` и тип карты SD в `device/type`;
- cpuid в `rockchip-otp0` (смещение 7, длина 16), MAC `end0` от U-Boot;
- вывод kernel memtest в dmesg и `CONFIG_MEMTEST`;
- `BLKFLSBUF` и `O_DIRECT` на mmc (прошивка со сверкой проходит);
- светодиод `rockpis:blue:user` (брак, «Идентифицировать»).

Осталось `VALIDATE`: место provision-записи `tail-json` (устаревший способ, `stages/flash.py`).
