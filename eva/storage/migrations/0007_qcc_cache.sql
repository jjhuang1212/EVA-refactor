-- 企业数据查询缓存（公共层）：同一家公司的同一维度在有效期内只向 QCC 付一次费。唯一写入口是 query_company 拿到 QCC 返回之后的基础设施写路径。
CREATE TABLE qcc_cache (
    uscc       char(18)      NOT NULL CONSTRAINT fk_qcc_cache_uscc REFERENCES company_master (uscc),
    dimension  varchar(32)   NOT NULL,
    person     nvarchar(128) NOT NULL CONSTRAINT df_qcc_cache_person DEFAULT '',
    payload    nvarchar(max) NOT NULL,
    fetched_at datetime2     NOT NULL,
    CONSTRAINT pk_qcc_cache PRIMARY KEY CLUSTERED (uscc, dimension, person)
);
