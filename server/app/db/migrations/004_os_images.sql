-- Образы ОС для SD-NAND: варианты меню (плата → ОС → ссылка) и подготовленные по ссылке образы
CREATE TABLE os_variant (
    id               INTEGER PRIMARY KEY,
    key              TEXT NOT NULL UNIQUE,
    name             TEXT NOT NULL,
    kind             TEXT NOT NULL DEFAULT 'url',   -- url | custom (одноразовый образ клиента)
    sort             INTEGER NOT NULL DEFAULT 100,
    visible          INTEGER NOT NULL DEFAULT 1,
    current_image_id INTEGER,
    created_at       TEXT NOT NULL
);

CREATE TABLE os_image (
    id                INTEGER PRIMARY KEY,
    variant_id        INTEGER NOT NULL REFERENCES os_variant(id),
    url               TEXT NOT NULL,
    state             TEXT NOT NULL,   -- queued | working | ready | error | replaced
    detail            TEXT,
    file              TEXT,            -- <key>-<id>.img.zst в каталоге образов сервера
    source_file       TEXT,
    version           TEXT,
    source_sha256     TEXT,
    source_size       INTEGER,
    image_size        INTEGER,
    image_sha256      TEXT,
    compressed_size   INTEGER,
    compressed_sha256 TEXT,
    operator          TEXT,
    created_at        TEXT NOT NULL,
    ready_at          TEXT
);

ALTER TABLE job ADD COLUMN os_key TEXT;
ALTER TABLE job ADD COLUMN os_name TEXT;
ALTER TABLE job ADD COLUMN image_file TEXT;
ALTER TABLE job ADD COLUMN image_url TEXT;
