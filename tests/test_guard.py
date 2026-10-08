"""Проверка защиты: тесты подключены именно к тестовой базе."""


async def test_connected_to_test_database(pool):
    name = await pool.fetchval("SELECT current_database()")
    assert name.endswith("_test")
