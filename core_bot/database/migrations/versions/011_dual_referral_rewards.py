VERSION = "011_dual_referral_rewards"
NAME = "Allow dual rewards per referral for inviter and invited friend"


def apply(conn) -> None:
    if conn.is_pg:
        # Drop existing single-reward unique constraint on referral_id
        conn.execute("ALTER TABLE rewards DROP CONSTRAINT IF EXISTS rewards_referral_id_key;")
        conn.execute("ALTER TABLE rewards ALTER COLUMN referral_id DROP NOT NULL;")
        # Add compound unique index so each user gets at most one reward per referral
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rewards_user_referral
            ON rewards(user_id, referral_id)
            WHERE referral_id IS NOT NULL;
            """
        )
    else:
        # SQLite
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_rewards_user_referral
            ON rewards(user_id, referral_id);
            """
        )
    conn.commit()


def rollback(conn) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_rewards_user_referral;")
    conn.commit()
