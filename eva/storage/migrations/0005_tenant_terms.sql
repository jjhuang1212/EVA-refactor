-- 租户私有的已确认写法：用户确认过的公司名、人名、术语的正确写法，以及常被听成什么。转写纠错时优先于公共热词表。
CREATE TABLE tenant_terms (
    tenant_id  varchar(64)   COLLATE Latin1_General_100_BIN2 NOT NULL,
    term       nvarchar(256) NOT NULL,
    heard_as   nvarchar(256) NULL,
    kind       varchar(16)   NOT NULL,
    session_id varchar(40)   NULL,
    created_at datetime2     NOT NULL,
    CONSTRAINT pk_tenant_terms PRIMARY KEY CLUSTERED (tenant_id, term)
);
