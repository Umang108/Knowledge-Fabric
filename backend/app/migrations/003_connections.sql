-- Saved connections to source systems (SAP, ServiceNow). Each belongs to the user who created it; the
-- password / client secret is encrypted with SECRET_KEY and never returned by the API.
CREATE TABLE connections (
    id                bigserial PRIMARY KEY,
    name              text NOT NULL CHECK (length(name) BETWEEN 1 AND 100),
    kind              text NOT NULL CHECK (kind IN ('sap', 'servicenow')),
    base_url          text NOT NULL,
    auth_type         text NOT NULL CHECK (auth_type IN ('basic', 'oauth')),
    username          text,
    secret            text,                 -- encrypted (Fernet, key derived from SECRET_KEY)
    options           jsonb NOT NULL DEFAULT '{}',
    owner_id          text NOT NULL REFERENCES users (user_id),
    last_tested_at    timestamptz,
    last_test_ok      boolean,
    last_test_detail  text,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    modified_by       text NOT NULL,
    UNIQUE (owner_id, name)
);
CREATE TRIGGER connections_set_updated_at BEFORE UPDATE ON connections
    FOR EACH ROW EXECUTE FUNCTION set_updated_at('last_tested_at', 'last_test_ok', 'last_test_detail');
