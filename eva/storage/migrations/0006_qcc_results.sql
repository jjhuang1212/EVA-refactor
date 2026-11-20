-- query_company 返回给模型的企业数据记录，按会话编号（q1、q2…）。回答里的 QCC 引用靠它核对摘抄，所以存的是模型实际看到的那一行文字；
-- uscc、维度、序号、取数时间是出处锚点：QCC 的数据是"当前状态"，本身不带日期，取数时间只能由我们记。
CREATE TABLE qcc_results (
    tenant_id    varchar(64)   COLLATE Latin1_General_100_BIN2 NOT NULL,
    session_id   varchar(40)   NOT NULL,
    ref_no       int           NOT NULL,
    call_id      varchar(128)  NOT NULL,
    uscc         char(18)      NOT NULL,
    dimension    varchar(32)   NOT NULL,
    person       nvarchar(128) NULL,
    record_no    int           NOT NULL,
    content      nvarchar(max) NOT NULL,
    fetched_at   datetime2     NOT NULL,
    CONSTRAINT pk_qcc_results PRIMARY KEY CLUSTERED (tenant_id, session_id, ref_no)
);
