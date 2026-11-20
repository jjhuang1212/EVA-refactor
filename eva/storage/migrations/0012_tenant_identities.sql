-- 租户编号改为系统生成的 <realm>_<小写 ULID>，渠道里的身份另存 tenant_identities。
-- 小写编号可以直接拼对象存储路径，storage_key 随之取消。已有的旧租户数据由单独的一次性清理处理，这里不动行。
ALTER TABLE tenants DROP CONSTRAINT uq_tenants_storage_key;
ALTER TABLE tenants DROP COLUMN channel, storage_key;

CREATE TABLE tenant_identities (
    realm       varchar(8)   NOT NULL,
    channel     varchar(16)  NOT NULL,
    scope       varchar(64)  COLLATE Latin1_General_100_BIN2 NOT NULL,
    external_id varchar(128) COLLATE Latin1_General_100_BIN2 NOT NULL,
    tenant_id   varchar(64)  COLLATE Latin1_General_100_BIN2 NOT NULL
                CONSTRAINT fk_tenant_identities_tenant REFERENCES tenants (tenant_id),
    created_at  datetime2    NOT NULL,
    CONSTRAINT pk_tenant_identities PRIMARY KEY CLUSTERED (realm, channel, scope, external_id)
);
CREATE INDEX ix_tenant_identities_tenant ON tenant_identities (tenant_id);
