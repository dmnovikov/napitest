CREATE TABLE operator (
    id          INTEGER PRIMARY KEY,
    username    TEXT NOT NULL UNIQUE,
    pw_hash     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);

CREATE TABLE session (
    token       TEXT PRIMARY KEY,
    operator_id INTEGER NOT NULL REFERENCES operator(id),
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);

CREATE TABLE device (
    id              INTEGER PRIMARY KEY,
    cpuid           TEXT NOT NULL UNIQUE,
    id_source       TEXT,
    serial          TEXT UNIQUE,
    mac             TEXT,
    mac_source      TEXT,
    ip              TEXT,
    hostname        TEXT,
    agent_version   TEXT,
    test_sd_version TEXT,
    kernel          TEXT,
    ram_bytes       INTEGER,
    has_led         INTEGER NOT NULL DEFAULT 0,
    simulated       INTEGER NOT NULL DEFAULT 0,
    is_golden       INTEGER NOT NULL DEFAULT 0,
    tested_pass     INTEGER NOT NULL DEFAULT 0,
    display_no      INTEGER,
    inventory_json  TEXT,
    selfcheck_json  TEXT,
    first_seen      TEXT NOT NULL,
    last_seen       TEXT NOT NULL
);

CREATE TABLE batch (
    id              INTEGER PRIMARY KEY,
    name            TEXT NOT NULL UNIQUE,
    kind            TEXT NOT NULL,            -- production | golden
    mode            TEXT NOT NULL,
    operator_id     INTEGER REFERENCES operator(id),
    started_at      TEXT NOT NULL,
    finished_at     TEXT,
    image_version   TEXT,
    profile_name    TEXT,
    options_json    TEXT,
    golden_batch_id INTEGER REFERENCES batch(id),
    golden_result   TEXT
);

CREATE TABLE job (
    id              TEXT PRIMARY KEY,
    batch_id        INTEGER NOT NULL REFERENCES batch(id),
    device_id       INTEGER NOT NULL REFERENCES device(id),
    mode            TEXT NOT NULL,
    status          TEXT NOT NULL,
    result          TEXT,
    detail          TEXT,
    created_at      TEXT NOT NULL,
    started_at      TEXT,
    finished_at     TEXT,
    boot_deadline   TEXT,
    params_json     TEXT NOT NULL,
    snapshot_json   TEXT,
    image_version   TEXT,
    image_sha256    TEXT,
    test_sd_version TEXT,
    agent_version   TEXT
);
CREATE INDEX job_device ON job(device_id, created_at);
CREATE INDEX job_batch ON job(batch_id);

CREATE TABLE stage_result (
    id                INTEGER PRIMARY KEY,
    job_id            TEXT NOT NULL REFERENCES job(id),
    stage             TEXT NOT NULL,
    state             TEXT NOT NULL,
    t                 REAL,
    detail            TEXT,
    measurements_json TEXT,
    log               TEXT,
    updated_at        TEXT NOT NULL,
    UNIQUE (job_id, stage)
);

CREATE TABLE event (
    id          INTEGER PRIMARY KEY,
    device_id   INTEGER REFERENCES device(id),
    job_id      TEXT,
    stream      TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    ts          REAL,
    kind        TEXT NOT NULL,
    stage       TEXT,
    data_json   TEXT,
    received_at TEXT NOT NULL,
    UNIQUE (stream, seq)
);
CREATE INDEX event_job ON event(job_id);

CREATE TABLE image (
    id          INTEGER PRIMARY KEY,
    version     TEXT NOT NULL UNIQUE,
    sha256      TEXT,
    size_bytes  INTEGER,
    first_seen  TEXT NOT NULL
);

CREATE TABLE test_sd_version (
    id          INTEGER PRIMARY KEY,
    version     TEXT NOT NULL UNIQUE,
    first_seen  TEXT NOT NULL
);

CREATE TABLE audit_log (
    id          INTEGER PRIMARY KEY,
    ts          TEXT NOT NULL,
    operator    TEXT,
    action      TEXT NOT NULL,
    detail_json TEXT
);

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
