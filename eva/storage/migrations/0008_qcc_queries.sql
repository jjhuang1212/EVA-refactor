-- query_company 每次调用的结果留痕（有记录、确认没有、没匹配上、出错）。qcc_results 只存有记录的行，
-- "查了但没有"和"没查"在那里分不出来；场景报告交付前靠这张表核对必备维度有没有查过。只追加。
CREATE TABLE qcc_queries (
    tenant_id  varchar(64) COLLATE Latin1_General_100_BIN2 NOT NULL,
    session_id varchar(40) NOT NULL,
    query_id   bigint      IDENTITY(1, 1) NOT NULL,
    uscc       char(18)    NOT NULL,
    dimension  varchar(32) NOT NULL,
    outcome    varchar(16) NOT NULL,
    queried_at datetime2   NOT NULL,
    CONSTRAINT pk_qcc_queries PRIMARY KEY CLUSTERED (tenant_id, session_id, query_id),
    CONSTRAINT ck_qcc_queries_outcome CHECK (outcome IN ('records', 'none', 'unmatched', 'failed'))
);
