CREATE TABLE IF NOT EXISTS digital_telemetry (
    ts     TIMESTAMPTZ NOT NULL DEFAULT now(),
    plc_id INTEGER NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES plc_tags(id) ON DELETE CASCADE,
    value  BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_digital_telemetry_tag_ts
    ON digital_telemetry (tag_id, ts DESC);

CREATE TABLE IF NOT EXISTS digital_tag_state (
    tag_id                INTEGER PRIMARY KEY REFERENCES plc_tags(id) ON DELETE CASCADE,
    plc_id                INTEGER NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
    value                 BOOLEAN NOT NULL,
    is_readable           BOOLEAN NOT NULL DEFAULT TRUE,
    last_successful_read  TIMESTAMPTZ,
    last_error            TEXT,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_digital_tag_state_readable
    ON digital_tag_state (is_readable, updated_at DESC);

CREATE TABLE IF NOT EXISTS plc_tag_status (
    tag_id                INTEGER PRIMARY KEY REFERENCES plc_tags(id) ON DELETE CASCADE,
    is_readable           BOOLEAN NOT NULL DEFAULT FALSE,
    last_successful_read  TIMESTAMPTZ,
    last_error            TEXT,
    updated_at            TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_plc_tag_status_readable
    ON plc_tag_status (is_readable, updated_at DESC);
