-- Миграция старой схемы из init.sql к контракту async poller.
-- Выполнять после резервной копии базы данных.

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'plcs' AND column_name = 'opc_url')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'plcs' AND column_name = 'opc_endpoint') THEN
        ALTER TABLE plcs RENAME COLUMN opc_url TO opc_endpoint;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'plcs' AND column_name = 'username') THEN
        ALTER TABLE plcs ADD COLUMN username TEXT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'plcs' AND column_name = 'password') THEN
        ALTER TABLE plcs ADD COLUMN password TEXT;
    END IF;
END $$;

    ALTER TABLE plc_tags
        ADD COLUMN IF NOT EXISTS is_archived BOOLEAN NOT NULL DEFAULT FALSE;
    ALTER TABLE plc_tags
        ADD COLUMN IF NOT EXISTS is_grafana_plotted BOOLEAN NOT NULL DEFAULT FALSE;

    ALTER TABLE plc_tags DROP CONSTRAINT IF EXISTS plc_tags_plc_id_name_key;
    CREATE UNIQUE INDEX IF NOT EXISTS uq_plc_tags_plc_node
        ON plc_tags (plc_id, node_id);
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'plc_tags' AND column_name = 'tag_name')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'plc_tags' AND column_name = 'name') THEN
        ALTER TABLE plc_tags RENAME COLUMN tag_name TO name;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'plc_tags' AND column_name = 'is_active') THEN
        ALTER TABLE plc_tags ADD COLUMN is_active BOOLEAN NOT NULL DEFAULT TRUE;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'telemetry' AND column_name = 'time')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'telemetry' AND column_name = 'ts') THEN
        ALTER TABLE telemetry RENAME COLUMN time TO ts;
    END IF;
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'telemetry' AND column_name = 'value_numeric')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'telemetry' AND column_name = 'value') THEN
        ALTER TABLE telemetry RENAME COLUMN value_numeric TO value;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'telemetry' AND column_name = 'plc_id') THEN
        ALTER TABLE telemetry ADD COLUMN plc_id INTEGER;
        UPDATE telemetry t SET plc_id = tags.plc_id FROM plc_tags tags WHERE tags.id = t.tag_id;
        ALTER TABLE telemetry ALTER COLUMN plc_id SET NOT NULL;
    END IF;
END $$;

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'alarms_log' AND column_name = 'created_at')
       AND NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'alarms_log' AND column_name = 'ts') THEN
        ALTER TABLE alarms_log RENAME COLUMN created_at TO ts;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'alarms_log' AND column_name = 'plc_id') THEN
        ALTER TABLE alarms_log ADD COLUMN plc_id INTEGER;
        UPDATE alarms_log a SET plc_id = tags.plc_id FROM plc_tags tags WHERE tags.id = a.tag_id;
        ALTER TABLE alarms_log ALTER COLUMN plc_id SET NOT NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'alarms_log' AND column_name = 'alarm_message') THEN
        ALTER TABLE alarms_log ADD COLUMN alarm_message TEXT NOT NULL DEFAULT 'DIGITAL_ALARM';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_name = 'alarms_log' AND column_name = 'value') THEN
        ALTER TABLE alarms_log ADD COLUMN value BOOLEAN NOT NULL DEFAULT TRUE;
    END IF;
END $$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_plc_tags_plc_node ON plc_tags (plc_id, node_id);
CREATE INDEX IF NOT EXISTS idx_plc_tags_plc_active ON plc_tags (plc_id) WHERE is_active;
CREATE INDEX IF NOT EXISTS idx_telemetry_tag_ts ON telemetry (tag_id, ts DESC);

CREATE TABLE IF NOT EXISTS digital_telemetry (
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    plc_id INTEGER NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES plc_tags(id) ON DELETE CASCADE,
    value BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_digital_telemetry_tag_ts
    ON digital_telemetry (tag_id, ts DESC);
CREATE INDEX IF NOT EXISTS idx_alarms_log_ts ON alarms_log (ts DESC);

CREATE TABLE IF NOT EXISTS digital_tag_state (
    tag_id INTEGER PRIMARY KEY REFERENCES plc_tags(id) ON DELETE CASCADE,
    plc_id INTEGER NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
    value BOOLEAN NOT NULL,
    is_readable BOOLEAN NOT NULL DEFAULT TRUE,
    last_successful_read TIMESTAMPTZ,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_digital_tag_state_readable
    ON digital_tag_state (is_readable, updated_at DESC);

CREATE TABLE IF NOT EXISTS plc_tag_status (
    tag_id INTEGER PRIMARY KEY REFERENCES plc_tags(id) ON DELETE CASCADE,
    is_readable BOOLEAN NOT NULL DEFAULT FALSE,
    last_successful_read TIMESTAMPTZ,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_plc_tag_status_readable
    ON plc_tag_status (is_readable, updated_at DESC);

CREATE TABLE IF NOT EXISTS plc_connection_status (
    plc_id INTEGER PRIMARY KEY REFERENCES plcs(id) ON DELETE CASCADE,
    is_connected BOOLEAN NOT NULL DEFAULT FALSE,
    last_successful_poll TIMESTAMPTZ,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS service_connection_status (
    service_name TEXT PRIMARY KEY,
    is_connected BOOLEAN NOT NULL DEFAULT FALSE,
    response_time_ms DOUBLE PRECISION NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE OR REPLACE FUNCTION public.log_plc_connection_change()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF OLD.is_connected IS DISTINCT FROM NEW.is_connected
       OR OLD.last_error IS DISTINCT FROM NEW.last_error THEN
        INSERT INTO public.plc_connection_log
            (plc_id, is_connected, error_msg, created_at)
        VALUES
            (NEW.plc_id, NEW.is_connected, NEW.last_error, NOW());
    END IF;
    RETURN NEW;
END;
$$;
