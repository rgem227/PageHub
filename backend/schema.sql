CREATE TABLE IF NOT EXISTS tenants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), name text NOT NULL,
    active boolean NOT NULL DEFAULT true, deleted_at timestamptz,
    quota_bytes bigint NOT NULL DEFAULT 1073741824 CHECK (quota_bytes > 0),
    layout text NOT NULL DEFAULT 'side' CHECK (layout IN ('side','top','flat')),
    created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_layout_check;
ALTER TABLE tenants ADD CONSTRAINT tenants_layout_check CHECK (layout IN ('side','top','flat'));
ALTER TABLE tenants ADD COLUMN IF NOT EXISTS open_mode text NOT NULL DEFAULT 'embedded' CHECK (open_mode IN ('embedded','external'));
CREATE TABLE IF NOT EXISTS users (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
    username text NOT NULL UNIQUE, full_name text NOT NULL DEFAULT '', phone text UNIQUE, password_hash text NOT NULL,
    role text NOT NULL DEFAULT 'user' CHECK (role IN ('user','superadmin')),
    active boolean NOT NULL DEFAULT true, must_change_password boolean NOT NULL DEFAULT false,
    theme text NOT NULL DEFAULT 'light' CHECK (theme IN ('light','dark')),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS users_tenant_id_id ON users(tenant_id,id);
ALTER TABLE users ADD COLUMN IF NOT EXISTS full_name text NOT NULL DEFAULT '';
CREATE TABLE IF NOT EXISTS sessions (
    token_hash text PRIMARY KEY, user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    last_seen_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_seen_at timestamptz;
UPDATE sessions SET last_seen_at=created_at WHERE last_seen_at IS NULL;
ALTER TABLE sessions ALTER COLUMN last_seen_at SET DEFAULT now();
ALTER TABLE sessions ALTER COLUMN last_seen_at SET NOT NULL;
CREATE INDEX IF NOT EXISTS sessions_user_activity ON sessions(user_id,last_seen_at DESC);
ALTER TABLE users ADD COLUMN IF NOT EXISTS quota_bytes bigint NOT NULL DEFAULT 1073741824 CHECK (quota_bytes > 0);
ALTER TABLE users ADD COLUMN IF NOT EXISTS directory_view text NOT NULL DEFAULT 'compact' CHECK (directory_view IN ('compact','list'));
ALTER TABLE users ADD COLUMN IF NOT EXISTS external_preview_minutes integer NOT NULL DEFAULT 5 CHECK (external_preview_minutes BETWEEN 1 AND 1440);
ALTER TABLE users ADD COLUMN IF NOT EXISTS last_activity_at timestamptz;
CREATE TABLE IF NOT EXISTS sms_codes (
    phone text PRIMARY KEY, code_hash text NOT NULL, expires_at timestamptz NOT NULL,
    attempts integer NOT NULL DEFAULT 0, sent_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS rate_limits (
    key text PRIMARY KEY, count integer NOT NULL DEFAULT 0,
    expires_at timestamptz NOT NULL
);
CREATE TABLE IF NOT EXISTS menus (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
    parent_id uuid, name text NOT NULL CHECK (length(name) BETWEEN 1 AND 60),
    position integer NOT NULL DEFAULT 0, flat_position integer NOT NULL DEFAULT 0,
    visible boolean NOT NULL DEFAULT true,
    UNIQUE (tenant_id,id),
    FOREIGN KEY (tenant_id,parent_id) REFERENCES menus(tenant_id,id)
);
ALTER TABLE menus ADD COLUMN IF NOT EXISTS flat_position integer;
WITH ranked AS (
    SELECT id,row_number() OVER (PARTITION BY tenant_id ORDER BY position,name,id) - 1 AS n
    FROM menus WHERE flat_position IS NULL
) UPDATE menus m SET flat_position=ranked.n FROM ranked WHERE m.id=ranked.id;
ALTER TABLE menus ALTER COLUMN flat_position SET DEFAULT 0;
ALTER TABLE menus ALTER COLUMN flat_position SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS menu_siblings ON menus(tenant_id,coalesce(parent_id,'00000000-0000-0000-0000-000000000000'::uuid),name);
CREATE TABLE IF NOT EXISTS pages (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
    menu_id uuid NOT NULL, title text NOT NULL, created_by uuid NOT NULL REFERENCES users,
    current_version uuid, deleted_at timestamptz, position integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,id), FOREIGN KEY (tenant_id,menu_id) REFERENCES menus(tenant_id,id)
);
ALTER TABLE pages ADD COLUMN IF NOT EXISTS position integer;
WITH ranked AS (
    SELECT id,row_number() OVER (PARTITION BY tenant_id ORDER BY created_at DESC,id) - 1 AS n
    FROM pages WHERE position IS NULL
) UPDATE pages p SET position=ranked.n FROM ranked WHERE p.id=ranked.id;
ALTER TABLE pages ALTER COLUMN position SET DEFAULT 0;
ALTER TABLE pages ALTER COLUMN position SET NOT NULL;
CREATE TABLE IF NOT EXISTS page_versions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
    page_id uuid NOT NULL, uploaded_by uuid NOT NULL REFERENCES users,
    entry_file text NOT NULL, storage_prefix text NOT NULL UNIQUE,
    size_bytes bigint NOT NULL, file_count integer NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,page_id,id), FOREIGN KEY (tenant_id,page_id) REFERENCES pages(tenant_id,id)
);
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='page_current_version') THEN
        ALTER TABLE pages ADD CONSTRAINT page_current_version
        FOREIGN KEY (tenant_id,id,current_version) REFERENCES page_versions(tenant_id,page_id,id) DEFERRABLE INITIALLY DEFERRED;
    END IF;
END $$;
ALTER TABLE tenants ENABLE ROW LEVEL SECURITY;
ALTER TABLE tenants FORCE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='tenants' AND policyname='tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON tenants USING (id=nullif(current_setting('app.tenant_id',true),'')::uuid OR current_setting('app.admin',true)='true') WITH CHECK (id=nullif(current_setting('app.tenant_id',true),'')::uuid OR current_setting('app.admin',true)='true');
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='users' AND policyname='tenant_isolation') THEN
        CREATE POLICY tenant_isolation ON users USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid OR current_setting('app.admin',true)='true') WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid OR current_setting('app.admin',true)='true');
    END IF;
END $$;
CREATE TABLE IF NOT EXISTS uploads (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
    user_id uuid NOT NULL REFERENCES users, page_id uuid, filename text NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','processing','success','failed')),
    options jsonb NOT NULL DEFAULT '{}', size_bytes bigint NOT NULL,
    expanded_bytes bigint, error text, result jsonb, attempts integer NOT NULL DEFAULT 0,
    claimed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS mcp_credentials (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
    user_id uuid NOT NULL REFERENCES users, name text NOT NULL, token_hash text NOT NULL UNIQUE,
    scopes text[] NOT NULL DEFAULT ARRAY['read','write'], active boolean NOT NULL DEFAULT true,
    expires_at timestamptz NOT NULL DEFAULT now()+interval '90 days',
    last_used_at timestamptz, created_at timestamptz NOT NULL DEFAULT now()
);
ALTER TABLE mcp_credentials ALTER COLUMN expires_at DROP NOT NULL;
ALTER TABLE mcp_credentials ALTER COLUMN expires_at DROP DEFAULT;
CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id text PRIMARY KEY, client_name text NOT NULL, redirect_uris text[] NOT NULL,
    token_endpoint_auth_method text NOT NULL DEFAULT 'none' CHECK (token_endpoint_auth_method='none'),
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS oauth_authorization_codes (
    code_hash text PRIMARY KEY, client_id text NOT NULL REFERENCES oauth_clients ON DELETE CASCADE,
    tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    redirect_uri text NOT NULL, scopes text[] NOT NULL, resource text NOT NULL,
    code_challenge text NOT NULL, code_challenge_method text NOT NULL CHECK (code_challenge_method='S256'),
    expires_at timestamptz NOT NULL, authorization_expires_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS oauth_grants (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants ON DELETE CASCADE,
    user_id uuid NOT NULL REFERENCES users ON DELETE CASCADE,
    client_id text NOT NULL REFERENCES oauth_clients ON DELETE CASCADE,
    scopes text[] NOT NULL, expires_at timestamptz, revoked_at timestamptz,
    last_used_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,id)
);
CREATE TABLE IF NOT EXISTS oauth_access_tokens (
    token_hash text PRIMARY KEY, tenant_id uuid NOT NULL, grant_id uuid NOT NULL,
    expires_at timestamptz NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id,grant_id) REFERENCES oauth_grants(tenant_id,id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS oauth_refresh_tokens (
    token_hash text PRIMARY KEY, tenant_id uuid NOT NULL, grant_id uuid NOT NULL,
    expires_at timestamptz, consumed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id,grant_id) REFERENCES oauth_grants(tenant_id,id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS oauth_codes_expiry ON oauth_authorization_codes(expires_at);
CREATE INDEX IF NOT EXISTS oauth_grants_user ON oauth_grants(tenant_id,user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS oauth_access_grant ON oauth_access_tokens(tenant_id,grant_id,expires_at);
CREATE INDEX IF NOT EXISTS oauth_refresh_grant ON oauth_refresh_tokens(tenant_id,grant_id);
CREATE TABLE IF NOT EXISTS audit_logs (
    id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, tenant_id uuid NOT NULL REFERENCES tenants,
    user_id uuid NOT NULL REFERENCES users, action text NOT NULL, detail jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
WITH activity AS (
    SELECT user_id,max(seen_at) AS seen_at FROM (
        SELECT user_id,last_seen_at AS seen_at FROM sessions
        UNION ALL
        SELECT user_id,created_at FROM audit_logs WHERE action='login'
    ) history GROUP BY user_id
)
UPDATE users u SET last_activity_at=a.seen_at FROM activity a
WHERE u.id=a.user_id AND u.last_activity_at IS NULL;
CREATE INDEX IF NOT EXISTS users_last_activity ON users(last_activity_at DESC);
CREATE INDEX IF NOT EXISTS pages_tenant ON pages(tenant_id,created_at);
CREATE INDEX IF NOT EXISTS pages_order ON pages(tenant_id,position,created_at DESC,id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS versions_tenant ON page_versions(tenant_id,page_id);
CREATE INDEX IF NOT EXISTS uploads_tenant ON uploads(tenant_id,status);
CREATE INDEX IF NOT EXISTS audit_tenant ON audit_logs(tenant_id,created_at DESC);
CREATE INDEX IF NOT EXISTS credentials_tenant ON mcp_credentials(tenant_id,user_id);
CREATE INDEX IF NOT EXISTS pages_creator ON pages(created_by);
CREATE INDEX IF NOT EXISTS uploads_user ON uploads(user_id,status);
CREATE INDEX IF NOT EXISTS versions_uploader ON page_versions(uploaded_by);
CREATE INDEX IF NOT EXISTS pages_menu ON pages(tenant_id,menu_id) WHERE deleted_at IS NULL;
CREATE INDEX IF NOT EXISTS menus_parent ON menus(tenant_id,parent_id);
CREATE INDEX IF NOT EXISTS menus_flat_order ON menus(tenant_id,flat_position,position,name,id);

CREATE TABLE IF NOT EXISTS bookmark_categories (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
    name text NOT NULL CHECK (length(name) BETWEEN 1 AND 60), position integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id,id), UNIQUE (tenant_id,name)
);
CREATE INDEX IF NOT EXISTS bookmark_categories_order ON bookmark_categories(tenant_id,position,name);
CREATE TABLE IF NOT EXISTS bookmarks (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
    menu_id uuid, category_id uuid, created_by uuid NOT NULL REFERENCES users,
    title text NOT NULL CHECK (length(title) BETWEEN 1 AND 200),
    url text NOT NULL CHECK (length(url) <= 2048), description text NOT NULL DEFAULT '',
    open_mode text NOT NULL DEFAULT 'external' CHECK (open_mode IN ('external','embedded')),
    position integer NOT NULL DEFAULT 0,
    created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id,menu_id) REFERENCES menus(tenant_id,id)
);
ALTER TABLE bookmarks ADD COLUMN IF NOT EXISTS category_id uuid;
DO $$ BEGIN
    IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema=current_schema() AND table_name='bookmarks' AND column_name='menu_id') THEN
        ALTER TABLE bookmarks ALTER COLUMN menu_id DROP NOT NULL;
        INSERT INTO bookmark_categories(tenant_id,name)
        SELECT DISTINCT b.tenant_id,m.name FROM bookmarks b JOIN menus m ON m.id=b.menu_id AND m.tenant_id=b.tenant_id
        ON CONFLICT (tenant_id,name) DO NOTHING;
        INSERT INTO bookmark_categories(tenant_id,name)
        SELECT DISTINCT tenant_id,'未分类' FROM bookmarks WHERE menu_id IS NULL
        ON CONFLICT (tenant_id,name) DO NOTHING;
        UPDATE bookmarks b SET category_id=c.id FROM menus m JOIN bookmark_categories c
            ON c.tenant_id=m.tenant_id AND c.name=m.name
        WHERE b.category_id IS NULL AND b.menu_id=m.id AND b.tenant_id=m.tenant_id;
        UPDATE bookmarks b SET category_id=c.id FROM bookmark_categories c
        WHERE b.category_id IS NULL AND c.tenant_id=b.tenant_id AND c.name='未分类';
    END IF;
END $$;
INSERT INTO bookmark_categories(tenant_id,name)
SELECT DISTINCT b.tenant_id,'未分类' FROM bookmarks b WHERE b.category_id IS NULL
ON CONFLICT (tenant_id,name) DO NOTHING;
UPDATE bookmarks b SET category_id=c.id FROM bookmark_categories c
WHERE b.category_id IS NULL AND c.tenant_id=b.tenant_id AND c.name='未分类';
ALTER TABLE bookmarks ALTER COLUMN category_id SET NOT NULL;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='bookmarks_category') THEN
        ALTER TABLE bookmarks ADD CONSTRAINT bookmarks_category FOREIGN KEY (tenant_id,category_id) REFERENCES bookmark_categories(tenant_id,id);
    END IF;
END $$;
DROP INDEX IF EXISTS bookmarks_menu;
ALTER TABLE bookmarks DROP CONSTRAINT IF EXISTS bookmarks_tenant_id_menu_id_fkey;
ALTER TABLE bookmarks DROP CONSTRAINT IF EXISTS bookmarks_menu_id_fkey;
ALTER TABLE bookmarks DROP COLUMN IF EXISTS menu_id;
CREATE INDEX IF NOT EXISTS bookmarks_category ON bookmarks(tenant_id,category_id,position,id);
CREATE INDEX IF NOT EXISTS bookmarks_creator ON bookmarks(created_by);

CREATE TABLE IF NOT EXISTS file_deletions (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
    user_id uuid NOT NULL REFERENCES users, page_id uuid NOT NULL,
    kind text NOT NULL CHECK (kind IN ('version','page')), version_ids uuid[] NOT NULL,
    status text NOT NULL DEFAULT 'queued' CHECK (status IN ('queued','processing','failed','success')),
    size_bytes bigint NOT NULL, file_count integer NOT NULL, error text,
    claimed_at timestamptz, completed_at timestamptz, created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id,page_id) REFERENCES pages(tenant_id,id)
);
ALTER TABLE pages ALTER COLUMN menu_id DROP NOT NULL;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS purge_id uuid REFERENCES file_deletions;
ALTER TABLE pages ADD COLUMN IF NOT EXISTS purged_at timestamptz;
ALTER TABLE page_versions ADD COLUMN IF NOT EXISTS file_manifest jsonb;
ALTER TABLE page_versions ADD COLUMN IF NOT EXISTS index_error text;
ALTER TABLE page_versions ADD COLUMN IF NOT EXISTS version_no integer;
WITH numbered AS (
    SELECT id,row_number() OVER (PARTITION BY page_id ORDER BY created_at,id) AS n FROM page_versions
) UPDATE page_versions v SET version_no=n.n FROM numbered n WHERE v.id=n.id AND v.version_no IS NULL;
ALTER TABLE page_versions ALTER COLUMN version_no SET NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS version_numbers ON page_versions(page_id,version_no);
ALTER TABLE page_versions ADD COLUMN IF NOT EXISTS deletion_id uuid REFERENCES file_deletions;
ALTER TABLE page_versions ADD COLUMN IF NOT EXISTS deleted_at timestamptz;
CREATE TABLE IF NOT EXISTS page_shares (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
    page_id uuid NOT NULL, version_id uuid NOT NULL, created_by uuid NOT NULL REFERENCES users,
    expires_at timestamptz NOT NULL, revoked_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id,page_id,version_id) REFERENCES page_versions(tenant_id,page_id,id)
);
ALTER TABLE page_shares ALTER COLUMN expires_at DROP NOT NULL;
CREATE TABLE IF NOT EXISTS account_shares (
    id uuid PRIMARY KEY DEFAULT gen_random_uuid(), tenant_id uuid NOT NULL REFERENCES tenants,
    recipient_tenant_id uuid NOT NULL REFERENCES tenants, recipient_user_id uuid NOT NULL,
    page_id uuid NOT NULL, version_id uuid NOT NULL, created_by uuid NOT NULL REFERENCES users,
    expires_at timestamptz, revoked_at timestamptz, recipient_removed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (tenant_id,page_id,version_id) REFERENCES page_versions(tenant_id,page_id,id),
    FOREIGN KEY (recipient_tenant_id,recipient_user_id) REFERENCES users(tenant_id,id)
);
CREATE INDEX IF NOT EXISTS page_shares_page ON page_shares(tenant_id,page_id,created_at DESC);
CREATE INDEX IF NOT EXISTS page_shares_version ON page_shares(tenant_id,version_id);
CREATE INDEX IF NOT EXISTS page_shares_owner ON page_shares(tenant_id,created_by,created_at DESC);
CREATE INDEX IF NOT EXISTS account_shares_owner ON account_shares(tenant_id,created_by,created_at DESC);
CREATE INDEX IF NOT EXISTS account_shares_recipient ON account_shares(recipient_user_id,created_at DESC);
CREATE INDEX IF NOT EXISTS deletions_queue ON file_deletions(status,created_at);
CREATE INDEX IF NOT EXISTS deletions_tenant ON file_deletions(tenant_id,created_at DESC);
CREATE INDEX IF NOT EXISTS versions_manifest_pending ON page_versions(created_at) WHERE file_manifest IS NULL AND deleted_at IS NULL;

-- tenant_id 来自事务上下文；普通连接无上下文时默认不可见。
DO $$ DECLARE t text; BEGIN
    FOREACH t IN ARRAY ARRAY['menus','pages','page_versions','page_shares','uploads','mcp_credentials','oauth_authorization_codes','oauth_grants','oauth_access_tokens','oauth_refresh_tokens','audit_logs','file_deletions','bookmarks','bookmark_categories'] LOOP
        EXECUTE format('ALTER TABLE %I ENABLE ROW LEVEL SECURITY',t);
        EXECUTE format('ALTER TABLE %I FORCE ROW LEVEL SECURITY',t);
        IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename=t AND policyname='tenant_isolation') THEN
            EXECUTE format('CREATE POLICY tenant_isolation ON %I USING (tenant_id = nullif(current_setting(''app.tenant_id'',true),'''')::uuid OR current_setting(''app.admin'',true)=''true'') WITH CHECK (tenant_id = nullif(current_setting(''app.tenant_id'',true),'''')::uuid OR current_setting(''app.admin'',true)=''true'')',t);
        END IF;
    END LOOP;
END $$;

ALTER TABLE account_shares ENABLE ROW LEVEL SECURITY;
ALTER TABLE account_shares FORCE ROW LEVEL SECURITY;
DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_policies WHERE tablename='account_shares' AND policyname='account_share_access') THEN
        CREATE POLICY account_share_access ON account_shares
        USING (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid
            OR recipient_user_id=nullif(current_setting('app.user_id',true),'')::uuid
            OR current_setting('app.admin',true)='true')
        WITH CHECK (tenant_id=nullif(current_setting('app.tenant_id',true),'')::uuid
            OR recipient_user_id=nullif(current_setting('app.user_id',true),'')::uuid
            OR current_setting('app.admin',true)='true');
    END IF;
END $$;
