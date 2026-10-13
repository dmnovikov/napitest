-- Тип платы (NAPI-C / NAPI-P): определяется по интерфейсу, через который агент подключён к серверу
-- (встроенный Ethernet → napi-c, USB-адаптер → napi-p), может быть задан вручную.
ALTER TABLE device ADD COLUMN board_detected TEXT;
ALTER TABLE device ADD COLUMN board_override TEXT;
ALTER TABLE device ADD COLUMN uplink_iface TEXT;
ALTER TABLE device ADD COLUMN uplink_bus TEXT;
ALTER TABLE device ADD COLUMN uplink_driver TEXT;
ALTER TABLE device ADD COLUMN uplink_mac TEXT;

ALTER TABLE job ADD COLUMN board_type TEXT;
ALTER TABLE job ADD COLUMN board_detected TEXT;
ALTER TABLE job ADD COLUMN uplink_iface TEXT;
