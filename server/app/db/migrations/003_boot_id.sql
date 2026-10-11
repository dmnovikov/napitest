-- Номер загрузки Linux на плате (/proc/sys/kernel/random/boot_id): плату перезагрузили/переподключили
-- после завершения проверки — квадрат «готов» с пометкой «уже проверялась»
ALTER TABLE device ADD COLUMN boot_id TEXT;
ALTER TABLE job ADD COLUMN boot_id TEXT;
