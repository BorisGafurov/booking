VERSION = "003_user_roles_and_schedule"
NAME = "Create user_roles and master_schedule tables"


def apply(conn) -> None:
    if conn.is_pg:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_roles (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                platform VARCHAR(32) NOT NULL DEFAULT 'telegram',
                platform_user_id BIGINT NOT NULL DEFAULT 0,
                role VARCHAR(32) NOT NULL DEFAULT 'master',
                permissions TEXT NOT NULL DEFAULT 'all',
                is_active BOOLEAN NOT NULL DEFAULT TRUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS master_schedule (
                id SERIAL PRIMARY KEY,
                day_key VARCHAR(16) NOT NULL UNIQUE,
                is_working BOOLEAN NOT NULL DEFAULT TRUE,
                start_time VARCHAR(8) NOT NULL DEFAULT '10:00',
                end_time VARCHAR(8) NOT NULL DEFAULT '20:00',
                note TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );

            CREATE INDEX IF NOT EXISTS idx_user_roles_platform ON user_roles(platform, platform_user_id);
            CREATE INDEX IF NOT EXISTS idx_master_schedule_day ON master_schedule(day_key);
            """
        )
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS user_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
                platform TEXT NOT NULL DEFAULT 'telegram',
                platform_user_id INTEGER NOT NULL DEFAULT 0,
                role TEXT NOT NULL DEFAULT 'master',
                permissions TEXT NOT NULL DEFAULT 'all',
                is_active INTEGER NOT NULL DEFAULT 1,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS master_schedule (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                day_key TEXT NOT NULL UNIQUE,
                is_working INTEGER NOT NULL DEFAULT 1,
                start_time TEXT NOT NULL DEFAULT '10:00',
                end_time TEXT NOT NULL DEFAULT '20:00',
                note TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_roles_platform ON user_roles(platform, platform_user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_master_schedule_day ON master_schedule(day_key)")


def rollback(conn) -> None:
    conn.execute("DROP TABLE IF EXISTS master_schedule")
    conn.execute("DROP TABLE IF EXISTS user_roles")
