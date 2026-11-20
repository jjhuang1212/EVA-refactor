-- 租户层 skill：只追加的版本表，每个 (租户, 名字) 版本号最大的那行生效。
-- tenants.skills_rev 每写入一行加一，对话每个 turn 开头读它判断要不要重取。
ALTER TABLE tenants ADD skills_rev int NOT NULL CONSTRAINT df_tenants_skills_rev DEFAULT 0;

CREATE TABLE tenant_skill_versions (
    tenant_id   varchar(64)    COLLATE Latin1_General_100_BIN2 NOT NULL CONSTRAINT fk_tenant_skill_versions_tenant REFERENCES tenants (tenant_id),
    name        varchar(64)    NOT NULL,
    version     int            NOT NULL,
    kind        varchar(16)    NOT NULL,
    status      varchar(16)    NOT NULL,
    description nvarchar(1024) NULL,
    body        nvarchar(max)  NOT NULL,
    reason      nvarchar(512)  NULL,
    created_at  datetime2      NOT NULL,
    CONSTRAINT pk_tenant_skill_versions PRIMARY KEY CLUSTERED (tenant_id, name, version)
);
