-- Migration 008: unify schedule/booking with the site and remove legacy CRM fields
-- Run this in Adminer (or psql) against the production database.
-- Safe to run multiple times.

-- 1. Client profile fields: rename legacy columns to neutral names, keeping data.
DO $$
DECLARE
    renames text[][] := ARRAY[
        ['masters_registered', 'source_registered'],
        ['masters_group', 'client_group'],
        ['masters_appointments_total', 'visits_total'],
        ['masters_cancelled_total', 'cancelled_total'],
        ['masters_revenue', 'revenue_total'],
        ['masters_imported_at', 'imported_at']
    ];
    r text[];
    old_col text;
    new_col text;
BEGIN
    FOREACH r SLICE 1 IN ARRAY renames LOOP
        old_col := r[1];
        new_col := r[2];
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = 'users' AND column_name = old_col
        ) THEN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                WHERE table_schema = 'public' AND table_name = 'users' AND column_name = new_col
            ) THEN
                EXECUTE format('UPDATE users SET %I = %I WHERE (%I IS NULL OR CAST(%I AS TEXT) IN ('''', ''0'')) AND %I IS NOT NULL', new_col, old_col, new_col, new_col, old_col);
                EXECUTE format('ALTER TABLE users DROP COLUMN IF EXISTS %I', old_col);
            ELSE
                EXECUTE format('ALTER TABLE users RENAME COLUMN %I TO %I', old_col, new_col);
            END IF;
        END IF;
    END LOOP;
END $$;

-- 2. Appointments: add duration_min and notes if missing.
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS duration_min INTEGER;
ALTER TABLE appointments ADD COLUMN IF NOT EXISTS notes TEXT;

-- Backfill duration_min from services text.
UPDATE appointments
SET duration_min = CASE
    WHEN lower(services) LIKE '%combo%'
         OR (lower(services) LIKE '%стрижк%' AND lower(services) LIKE '%бород%')
        THEN 135
    WHEN lower(services) LIKE '%бород%'
        THEN 60
    ELSE 75
END
WHERE duration_min IS NULL;

-- Ensure NOT NULL with default.
UPDATE appointments SET duration_min = 75 WHERE duration_min IS NULL;
ALTER TABLE appointments ALTER COLUMN duration_min SET DEFAULT 75;
ALTER TABLE appointments ALTER COLUMN duration_min SET NOT NULL;

-- 3. Drop legacy external id column from appointments.
ALTER TABLE appointments DROP COLUMN IF EXISTS masters_id;

-- 4. Relabel legacy CRM platform rows.
UPDATE appointments SET platform = 'manual' WHERE platform = 'masters';

-- 5. Drop legacy session storage table.
DROP TABLE IF EXISTS masters_session;

-- 6. Clean up superseded migration records.
DELETE FROM schema_migrations WHERE version IN ('002_masters_session', '007_masters_client_fields');

-- Verify (optional).
SELECT column_name FROM information_schema.columns WHERE table_name = 'users' AND column_name IN ('source_registered','client_group','visits_total','cancelled_total','revenue_total','imported_at');
SELECT column_name FROM information_schema.columns WHERE table_name = 'appointments' AND column_name IN ('duration_min','notes');
SELECT COUNT(*) FROM appointments WHERE platform = 'masters';
SELECT to_regclass('public.masters_session');
