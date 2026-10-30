VERSION = "001_initial_schema"
NAME = "Create users, referrals, rewards, and appointments tables"


def apply(conn) -> None:
    if conn.is_pg:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id SERIAL PRIMARY KEY,
                telegram_id BIGINT UNIQUE,
                vk_id BIGINT UNIQUE,
                max_user_id BIGINT UNIQUE,
                phone VARCHAR(32) UNIQUE,
                username VARCHAR(128),
                first_name VARCHAR(128),
                last_name VARCHAR(128),
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS referrals (
                id SERIAL PRIMARY KEY,
                referrer_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                invited_user_id INTEGER NOT NULL UNIQUE REFERENCES users(id) ON DELETE CASCADE,
                status VARCHAR(32) NOT NULL DEFAULT 'started',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                visited_at TIMESTAMP WITH TIME ZONE,
                rewarded_at TIMESTAMP WITH TIME ZONE
            );

            CREATE TABLE IF NOT EXISTS rewards (
                id SERIAL PRIMARY KEY,
                user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                referral_id INTEGER NOT NULL UNIQUE REFERENCES referrals(id) ON DELETE CASCADE,
                amount INTEGER NOT NULL DEFAULT 300,
                status VARCHAR(32) NOT NULL DEFAULT 'active',
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                used_at TIMESTAMP WITH TIME ZONE
            );

            CREATE TABLE IF NOT EXISTS appointments (
                id SERIAL PRIMARY KEY,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                platform VARCHAR(32) NOT NULL DEFAULT 'telegram',
                platform_user_id BIGINT NOT NULL DEFAULT 0,
                client_name VARCHAR(128),
                phone VARCHAR(32) NOT NULL,
                services TEXT NOT NULL,
                total_price INTEGER NOT NULL DEFAULT 0,
                appointment_time TIMESTAMP WITH TIME ZONE NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'confirmed',
                client_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
                reminder_evening_sent BOOLEAN NOT NULL DEFAULT FALSE,
                reminder_morning_sent BOOLEAN NOT NULL DEFAULT FALSE,
                reminder_2h_sent BOOLEAN NOT NULL DEFAULT FALSE,
                retention_18d_sent BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            );

            ALTER TABLE appointments ADD COLUMN IF NOT EXISTS client_confirmed BOOLEAN NOT NULL DEFAULT FALSE;
            ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reminder_morning_sent BOOLEAN NOT NULL DEFAULT FALSE;

            CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);
            CREATE INDEX IF NOT EXISTS idx_users_vk_id ON users(vk_id);
            CREATE INDEX IF NOT EXISTS idx_users_max_user_id ON users(max_user_id);
            CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone);
            CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_user_id);
            CREATE INDEX IF NOT EXISTS idx_referrals_invited ON referrals(invited_user_id);
            CREATE INDEX IF NOT EXISTS idx_rewards_user ON rewards(user_id);
            CREATE INDEX IF NOT EXISTS idx_rewards_status ON rewards(status);
            CREATE INDEX IF NOT EXISTS idx_appointments_time ON appointments(appointment_time);
            CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments(phone);
            CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointments(status);
            CREATE INDEX IF NOT EXISTS idx_appointments_client_confirmed ON appointments(client_confirmed);
            """
        )
    else:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                telegram_id INTEGER UNIQUE,
                vk_id INTEGER UNIQUE,
                max_user_id INTEGER UNIQUE,
                phone TEXT UNIQUE,
                username TEXT,
                first_name TEXT,
                last_name TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        cursor = conn.raw_conn.execute("PRAGMA table_info(users)")
        columns = [row["name"] for row in cursor.fetchall()]
        for col in ["telegram_id", "vk_id", "max_user_id", "phone", "last_name", "updated_at"]:
            if col not in columns:
                conn.raw_conn.execute(f"ALTER TABLE users ADD COLUMN {col} TEXT")

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_user_id INTEGER NOT NULL,
                invited_user_id INTEGER NOT NULL UNIQUE,
                status TEXT NOT NULL DEFAULT 'started',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                visited_at TIMESTAMP,
                rewarded_at TIMESTAMP,
                FOREIGN KEY (referrer_user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (invited_user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rewards (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                referral_id INTEGER NOT NULL UNIQUE,
                amount INTEGER NOT NULL DEFAULT 300,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_at TIMESTAMP,
                FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
                FOREIGN KEY (referral_id) REFERENCES referrals(id) ON DELETE CASCADE
            );
            """
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS appointments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
                platform TEXT NOT NULL DEFAULT 'telegram',
                platform_user_id INTEGER NOT NULL DEFAULT 0,
                client_name TEXT,
                phone TEXT NOT NULL,
                services TEXT NOT NULL,
                total_price INTEGER NOT NULL DEFAULT 0,
                appointment_time TIMESTAMP NOT NULL,
                status TEXT NOT NULL DEFAULT 'confirmed',
                client_confirmed INTEGER NOT NULL DEFAULT 0,
                reminder_evening_sent INTEGER NOT NULL DEFAULT 0,
                reminder_morning_sent INTEGER NOT NULL DEFAULT 0,
                reminder_2h_sent INTEGER NOT NULL DEFAULT 0,
                retention_18d_sent INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        cursor = conn.raw_conn.execute("PRAGMA table_info(appointments)")
        app_cols = [row["name"] for row in cursor.fetchall()]
        for col in ["client_confirmed", "reminder_morning_sent"]:
            if col not in app_cols:
                conn.raw_conn.execute(f"ALTER TABLE appointments ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0")

        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_vk_id ON users(vk_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_max_user_id ON users(max_user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_users_phone ON users(phone)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_referrals_invited ON referrals(invited_user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rewards_user ON rewards(user_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_rewards_status ON rewards(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_time ON appointments(appointment_time)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_phone ON appointments(phone)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_status ON appointments(status)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_appointments_client_confirmed ON appointments(client_confirmed)")


def rollback(conn) -> None:
    conn.execute("DROP TABLE IF EXISTS appointments")
    conn.execute("DROP TABLE IF EXISTS rewards")
    conn.execute("DROP TABLE IF EXISTS referrals")
    conn.execute("DROP TABLE IF EXISTS users")
