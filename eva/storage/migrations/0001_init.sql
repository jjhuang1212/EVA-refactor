-- tenant_id 及引用它的列用区分大小写的排序规则：默认规则会把只差大小写的两个用户 ID 当成同一租户。
-- 时间列不设默认值，由应用层传入东八区时间。

CREATE TABLE tenants (
    tenant_id   varchar(64) COLLATE Latin1_General_100_BIN2 NOT NULL CONSTRAINT pk_tenants PRIMARY KEY,
    channel     varchar(16) NOT NULL,
    storage_key char(26)    NOT NULL CONSTRAINT uq_tenants_storage_key UNIQUE,
    created_at  datetime2   NOT NULL
);

CREATE TABLE sessions (
    session_id        varchar(40)  NOT NULL CONSTRAINT pk_sessions PRIMARY KEY,
    tenant_id         varchar(64)  COLLATE Latin1_General_100_BIN2 NOT NULL
                      CONSTRAINT fk_sessions_tenant REFERENCES tenants (tenant_id),
    channel           varchar(16)  NOT NULL,
    channel_account   nvarchar(64) NULL,
    parent_session_id varchar(40)  NULL,
    created_at        datetime2    NOT NULL
);
CREATE INDEX ix_sessions_tenant ON sessions (tenant_id, channel, channel_account);

CREATE TABLE transcript_events (
    session_id varchar(40)   NOT NULL,
    seq        int           NOT NULL,
    tenant_id  varchar(64)   COLLATE Latin1_General_100_BIN2 NOT NULL,
    event_type varchar(32)   NOT NULL,
    payload    nvarchar(max) NOT NULL,
    created_at datetime2     NOT NULL,
    CONSTRAINT pk_transcript_events PRIMARY KEY CLUSTERED (session_id, seq)
);
CREATE INDEX ix_transcript_events_tenant ON transcript_events (tenant_id);

CREATE TABLE ledger_events (
    event_id             bigint IDENTITY(1,1) NOT NULL CONSTRAINT pk_ledger_events PRIMARY KEY,
    tenant_id            varchar(64)   COLLATE Latin1_General_100_BIN2 NOT NULL,
    session_id           varchar(40)   NOT NULL,
    kind                 varchar(8)    NOT NULL,
    hold_id              varchar(40)   NOT NULL,
    idem_key             varchar(64)   NOT NULL,
    purpose              varchar(32)   NOT NULL,
    model                varchar(64)   NULL,
    amount_cny           decimal(10,4) NOT NULL,
    tokens_in            int           NULL,
    tokens_out           int           NULL,
    cache_hit_tokens     int           NULL,
    cache_miss_tokens    int           NULL,
    asr_seconds          int           NULL,
    pricing_version      varchar(32)   NOT NULL,
    created_at           datetime2     NOT NULL,
    CONSTRAINT uq_ledger_events_idem UNIQUE (idem_key, kind)
);
CREATE INDEX ix_ledger_events_hold ON ledger_events (hold_id);
CREATE INDEX ix_ledger_events_tenant ON ledger_events (tenant_id);
CREATE INDEX ix_ledger_events_session ON ledger_events (session_id);

CREATE TABLE file_registry (
    file_id    varchar(40)   NOT NULL CONSTRAINT pk_file_registry PRIMARY KEY,
    tenant_id  varchar(64)   COLLATE Latin1_General_100_BIN2 NOT NULL,
    sha256     char(64)      NOT NULL,
    filename   nvarchar(512) NOT NULL,
    mime       varchar(96)   NOT NULL,
    bytes      bigint        NOT NULL,
    minio_key  nvarchar(512) NOT NULL,
    parsed_key nvarchar(512) NULL,
    status     varchar(16)   NOT NULL,
    created_at datetime2     NOT NULL,
    CONSTRAINT uq_file_registry_tenant_sha UNIQUE (tenant_id, sha256)
);

CREATE TABLE company_master (
    uscc        char(18)      NOT NULL CONSTRAINT pk_company_master PRIMARY KEY,
    full_name   nvarchar(512) NOT NULL,
    short_name  nvarchar(128) NULL,
    sec_code    varchar(8)    NULL,
    exchange    varchar(8)    NULL,
    listed_at   date          NULL,
    delisted_at date          NULL,
    data_json   nvarchar(max) NULL,
    fetched_at  datetime2     NOT NULL,
    updated_at  datetime2     NOT NULL
);

CREATE TABLE company_aliases (
    alias_id   bigint IDENTITY(1,1) NOT NULL CONSTRAINT pk_company_aliases PRIMARY KEY,
    uscc       char(18)      NOT NULL CONSTRAINT fk_company_aliases_uscc REFERENCES company_master (uscc),
    alias      nvarchar(512) NOT NULL,
    alias_kind varchar(16)   NOT NULL,
    source     varchar(24)   NOT NULL,
    created_at datetime2     NOT NULL,
    CONSTRAINT uq_company_aliases UNIQUE (uscc, alias, alias_kind)
);
CREATE INDEX ix_company_aliases_alias ON company_aliases (alias);

CREATE TABLE tenant_aliases (
    alias_id   bigint IDENTITY(1,1) NOT NULL CONSTRAINT pk_tenant_aliases PRIMARY KEY,
    tenant_id  varchar(64)   COLLATE Latin1_General_100_BIN2 NOT NULL,
    alias      nvarchar(512) NOT NULL,
    uscc       char(18)      NOT NULL CONSTRAINT fk_tenant_aliases_uscc REFERENCES company_master (uscc),
    session_id varchar(40)   NULL,
    created_at datetime2     NOT NULL,
    CONSTRAINT uq_tenant_aliases UNIQUE (tenant_id, alias)
);

CREATE TABLE asr_hotwords (
    hotword_id  bigint IDENTITY(1,1) NOT NULL CONSTRAINT pk_asr_hotwords PRIMARY KEY,
    term        nvarchar(256) NOT NULL,
    weight      decimal(5,2)  NOT NULL,
    source_kind varchar(24)   NOT NULL,
    company_id  char(18)      NULL,
    industry    nvarchar(64)  NULL,
    created_at  datetime2     NOT NULL
);
CREATE INDEX ix_asr_hotwords_term ON asr_hotwords (term);
