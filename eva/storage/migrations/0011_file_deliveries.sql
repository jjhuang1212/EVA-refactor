-- 文件的交付记录，只追加：同一份文件可以在不同会话反复交付，每次一行。
-- 列"以前交付过的文件"（历史报告）靠它；scenario、uscc 取自报告里嵌的引用清单，没有清单的文件（转写稿等）为空。
-- session_id 不加外键：交付发生在哪个会话只是出处，不能因会话行不在而拦住交付。
CREATE TABLE file_deliveries (
    delivery_id  varchar(40)   NOT NULL CONSTRAINT pk_file_deliveries PRIMARY KEY,
    tenant_id    varchar(64)   COLLATE Latin1_General_100_BIN2 NOT NULL,
    file_id      varchar(40)   NOT NULL CONSTRAINT fk_file_deliveries_file REFERENCES file_registry (file_id),
    session_id   varchar(40)   NOT NULL,
    scenario     varchar(64)   NULL,
    uscc         char(18)      NULL,
    delivered_at datetime2     NOT NULL
);
CREATE INDEX ix_file_deliveries_tenant_time ON file_deliveries (tenant_id, delivered_at DESC);
CREATE INDEX ix_file_deliveries_file ON file_deliveries (file_id);
