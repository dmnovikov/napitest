# Порядок загрузки и проверки на реальной плате

Выполнить на одной NAPI-C **до** установки `hardware_validated: true`. Результаты записать сюда.

## 1. BootROM и U-Boot

1. Плата с пустой SD-NAND + тестовая microSD: загрузка с microSD.
2. Записать на SD-NAND финальный образ вручную (на стенде инженера), вставить тестовую microSD, включить.
   - Загрузилась тестовая Armbian → microSD имеет приоритет, повторная прошивка возможна.
   - Загрузилась система с SD-NAND → BootROM или U-Boot предпочитает SD-NAND. Требование к финальному образу: U-Boot с `boot_targets`, где microSD (`mmc` контроллера `ff480000`) раньше SD-NAND. Без этого режим «Только прошивка» для уже записанных плат невозможен, а этап BOOT невоспроизводим.
3. Без microSD плата должна грузиться с SD-NAND.

Проверить порядок BootROM RK3308 по выводу на UART (строки DDR/SPL/U-Boot с указанием носителя).

| Проверка | Результат | Дата, кто |
|---|---|---|
| BootROM: первый носитель | | |
| U-Boot `boot_targets` финального образа | | |
| Тестовая microSD побеждает записанную SD-NAND | | |

## 2. Накопители

```sh
lsblk -o NAME,SIZE,TYPE,RO,RM,MOUNTPOINTS
for d in /sys/block/mmcblk*; do echo "$d $(readlink -f $d/device) $(cat $d/device/type) $(cat $d/size)"; done
ls -l /dev/disk/by-path/
findmnt /
```

Записать контроллер SD-NAND и microSD, тип (`MMC`/`SD`), точный размер SD-NAND в байтах.
Обновить `sdnand.detect` в профиле и `detect` в `agent.yaml`.

## 3. Идентификация и сеть

```sh
cat /proc/device-tree/serial-number; echo
ls /sys/bus/nvmem/devices/; xxd /sys/bus/nvmem/devices/rockchip-otp0/nvmem | head
ip -br link; cat /sys/class/net/end0/addr_assign_type; ethtool end0
```

Проверить на двух платах с клонированными картами: cpuid и MAC различаются.

## 4. Ядро тестовой microSD

```sh
zcat /proc/config.gz | grep MEMTEST    # нужно CONFIG_MEMTEST=y
cat /proc/cmdline                      # нужно memtest=4
dmesg | grep -i memtest
```

Сохранить пример вывода `dmesg` с memtest в `agent/tests` как фикстуру, если формат отличается от ожидаемого.

## 5. Пробная прошивка одной платы

С `NAPI_DESTRUCTIVE_OVERRIDE=1` на сервере инженера, одна плата, режим «Тесты + прошивка».
Проверить: хеши совпали, readback совпал, после извлечения microSD плата загрузилась с SD-NAND и
прислала `boot_ok`, provision-запись читается. Затем `hardware_validated: true` в профиле,
`NAPI_DESTRUCTIVE_OVERRIDE` убрать. Дальше испытания на 5 и 50 платах.
