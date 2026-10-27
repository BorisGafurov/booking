VERSION = "002_masters_session"
NAME = "Create masters_session table for Masters CRM auth"


def apply(conn) -> None:
    if conn.is_pg:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS masters_session (
                id SERIAL PRIMARY KEY,
                master_phone VARCHAR(32),
                cookie_phpsessid TEXT,
                cookie_csrf TEXT,
                logged_in_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                is_active BOOLEAN NOT NULL DEFAULT TRUE
            );
            """
        )
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS masters_session (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                master_phone TEXT,
                cookie_phpsessid TEXT,
                cookie_csrf TEXT,
                logged_in_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER NOT NULL DEFAULT 1
            );
            """
        )


def rollback(conn) -> None:
    conn.execute("DROP TABLE IF EXISTS masters_session")
