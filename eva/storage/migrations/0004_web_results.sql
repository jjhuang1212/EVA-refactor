-- 联网搜索返回给模型的结果，按会话编号（w1、w2…）。回答里的网页引用靠它核对摘抄，所以存的是模型实际看到的文字（已截断的正文）。
CREATE TABLE web_results (
    tenant_id    varchar(64)    COLLATE Latin1_General_100_BIN2 NOT NULL,
    session_id   varchar(40)    NOT NULL,
    ref_no       int            NOT NULL,
    call_id      varchar(128)   NOT NULL,
    title        nvarchar(512)  NOT NULL,
    media        nvarchar(128)  NULL,
    publish_date varchar(32)    NULL,
    link         nvarchar(2048) NOT NULL,
    content      nvarchar(max)  NOT NULL,
    created_at   datetime2      NOT NULL,
    CONSTRAINT pk_web_results PRIMARY KEY CLUSTERED (tenant_id, session_id, ref_no)
);
