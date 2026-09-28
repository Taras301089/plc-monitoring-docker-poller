ALTER TABLE plc_tags DROP CONSTRAINT IF EXISTS plc_tags_plc_id_name_key;
CREATE UNIQUE INDEX IF NOT EXISTS uq_plc_tags_plc_node
    ON plc_tags (plc_id, node_id);