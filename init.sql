-- Контракт таблиц для async poller. Для существующего тома применяйте миграцию
-- вручную после резервного копирования; init.sql выполняется только на пустом томе.

-- PostgreSQL запускает этот файл в базе POSTGRES_DB (postgres).
-- Создаём две необходимые БД: для N8N и для Poller

CREATE DATABASE n8n_db;
CREATE DATABASE general_data_hub_BD;
\connect general_data_hub_BD

-- 1. ТАБЛИЦА ПОДКЛЮЧЕНИЙ К ПЛК
CREATE TABLE IF NOT EXISTS plcs (
    id SERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    opc_endpoint TEXT NOT NULL,
    poll_interval_ms INTEGER NOT NULL DEFAULT 1000 CHECK (poll_interval_ms >= 50),
    is_active BOOLEAN NOT NULL DEFAULT TRUE,
    username TEXT,
    password TEXT
);

-- 2. РЕЕСТР ТЕГОВ И УСТАВОК (С расширенными настройками для Web UI)
CREATE TABLE IF NOT EXISTS plc_tags (
    id SERIAL PRIMARY KEY,
    plc_id INTEGER NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
    name TEXT NOT NULL,
    node_id TEXT NOT NULL,
    tag_type TEXT NOT NULL CHECK (tag_type IN ('ANALOG', 'DIGITAL')),
    is_alarm_enabled BOOLEAN NOT NULL DEFAULT FALSE,
    alarm_message TEXT,
        is_archived BOOLEAN NOT NULL DEFAULT FALSE,
        is_grafana_plotted BOOLEAN NOT NULL DEFAULT FALSE,
    is_active BOOLEAN NOT NULL DEFAULT TRUE
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_plc_tags_plc_node ON plc_tags (plc_id, node_id);
CREATE INDEX IF NOT EXISTS idx_plc_tags_plc_active ON plc_tags (plc_id) WHERE is_active;

-- 3. ТАБЛИЦА ТЕЛЕМЕТРИИ (ВРЕМЕННЫЕ РЯДЫ)
CREATE TABLE IF NOT EXISTS telemetry (
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    plc_id INTEGER NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES plc_tags(id) ON DELETE CASCADE,
    value DOUBLE PRECISION NOT NULL
);

-- 4. ЖУРНАЛ АВАРИЙ И ПРЕДУПРЕЖДЕНИЙ
CREATE TABLE IF NOT EXISTS alarms_log (
    id BIGSERIAL PRIMARY KEY,
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    plc_id INTEGER NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES plc_tags(id) ON DELETE CASCADE,
    alarm_message TEXT NOT NULL,
    value BOOLEAN NOT NULL DEFAULT TRUE
);

-- ============================================================================
-- ИНДЕКСЫ ДЛЯ ОПТИМИЗАЦИИ ВЫСОКОНАГРУЖЕННЫХ ЗАПРОСОВ
-- ============================================================================

-- Быстрая выборка временных рядов для Grafana (сортировка по времени)
CREATE INDEX IF NOT EXISTS idx_telemetry_tag_ts ON telemetry (tag_id, ts DESC);

CREATE TABLE IF NOT EXISTS digital_telemetry (
    ts TIMESTAMPTZ NOT NULL DEFAULT now(),
    plc_id INTEGER NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
    tag_id INTEGER NOT NULL REFERENCES plc_tags(id) ON DELETE CASCADE,
    value BOOLEAN NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_digital_telemetry_tag_ts
    ON digital_telemetry (tag_id, ts DESC);

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

-- Мгновенный поиск неотправленных алармов для скрипта n8n
CREATE INDEX IF NOT EXISTS idx_alarms_log_ts ON alarms_log (ts DESC);

CREATE TABLE IF NOT EXISTS plc_tag_status (
    tag_id INTEGER PRIMARY KEY REFERENCES plc_tags(id) ON DELETE CASCADE,
    is_readable BOOLEAN NOT NULL DEFAULT FALSE,
    last_successful_read TIMESTAMPTZ,
    last_error TEXT,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_plc_tag_status_readable
    ON plc_tag_status (is_readable, updated_at DESC);

-- Индекс связи тегов с ПЛК
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

CREATE TABLE IF NOT EXISTS plc_discovered_nodes (
    id SERIAL PRIMARY KEY,
    plc_id INTEGER NOT NULL REFERENCES plcs(id) ON DELETE CASCADE,
    db_name TEXT NOT NULL DEFAULT '',
    variable_name TEXT NOT NULL,
    node_id TEXT NOT NULL,
    namespace_index INTEGER NOT NULL DEFAULT 0,
    node_class TEXT NOT NULL DEFAULT 'Variable',
    data_type TEXT NOT NULL DEFAULT 'Unknown',
    browse_path TEXT NOT NULL,
    is_system BOOLEAN NOT NULL DEFAULT FALSE,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_plc_discovered_nodes
    ON plc_discovered_nodes (plc_id, node_id);

-- Тестовые данные для первичной проверки системы.

INSERT INTO plcs (name, opc_endpoint, poll_interval_ms, is_active)
VALUES ('Siemens_S7_1500_Main', 'opc.tcp://192.168.1.10:4840', 1000, TRUE)
ON CONFLICT (name) DO NOTHING;

INSERT INTO plc_tags (plc_id, name, node_id, tag_type, alarm_message, is_alarm_enabled)
VALUES 
    (1, 'Temperature_Reactor_1', 'ns=2;s="DB_Sensors"."Temp_01"', 'ANALOG', NULL, FALSE),
    (1, 'Pressure_Pipeline_1', 'ns=2;s="DB_Sensors"."Press_01"', 'ANALOG', NULL, FALSE),
    (1, 'Pump_1_Trip', 'ns=2;s="DB_Alarms"."Pump_1_Fault"', 'DIGITAL', 'Авария насоса №1: перегрузка двигателя', TRUE)
ON CONFLICT (plc_id, node_id) DO NOTHING;