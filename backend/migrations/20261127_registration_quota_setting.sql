BEGIN;

CREATE TABLE IF NOT EXISTS platform_settings (
    id smallint PRIMARY KEY DEFAULT 1 CHECK (id=1),
    default_registration_quota_mb integer NOT NULL DEFAULT 1024
        CHECK (default_registration_quota_mb BETWEEN 1 AND 1048576),
    updated_by uuid REFERENCES users ON DELETE SET NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
INSERT INTO platform_settings(id) VALUES (1) ON CONFLICT (id) DO NOTHING;
ALTER TABLE platform_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE platform_settings FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies
        WHERE schemaname=current_schema() AND tablename='platform_settings' AND policyname='platform_admin_only') THEN
        CREATE POLICY platform_admin_only ON platform_settings
        USING (current_setting('app.admin',true)='true')
        WITH CHECK (current_setting('app.admin',true)='true');
    END IF;
END $$;

DO $$ DECLARE app_role text; BEGIN
    FOREACH app_role IN ARRAY ARRAY['html_hub_app','pagehub_prod_app'] LOOP
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname=app_role) THEN
            EXECUTE format('GRANT SELECT, INSERT, UPDATE, DELETE ON platform_settings TO %I',app_role);
        END IF;
    END LOOP;
END $$;
COMMIT;
