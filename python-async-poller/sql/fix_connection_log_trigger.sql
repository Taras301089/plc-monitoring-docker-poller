-- Исправляет триггер журнала статусов ПЛК под фактическую структуру plc_connection_log.
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
