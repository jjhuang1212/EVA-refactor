-- 评测的人工标注和认可版本，开发者工具用。挂在评测租户（eval- 开头）下，只追加：同一对象再标就加一行，序号最大的生效。
-- 行里带着租户、会话、输入版本和自动判定的结果，评测本地的 out/ 丢了，统计和回放仍然能做。

CREATE TABLE eval_annotations (
    tenant_id   varchar(64)   COLLATE Latin1_General_100_BIN2 NOT NULL CONSTRAINT fk_eval_annotations_tenant REFERENCES tenants (tenant_id),
    run_id      varchar(40)   NOT NULL,
    seq         int           NOT NULL,
    case_name   varchar(40)   NOT NULL,
    session_id  varchar(40)   NULL,
    inputs_sha  char(64)      NULL,
    label       varchar(40)   NULL,
    auto_passed bit           NOT NULL,
    verdict     varchar(16)   NOT NULL,
    note        nvarchar(1024) NULL,
    created_at  datetime2     NOT NULL,
    CONSTRAINT pk_eval_annotations PRIMARY KEY CLUSTERED (tenant_id, run_id, seq)
);
CREATE INDEX ix_eval_annotations_case ON eval_annotations (case_name, inputs_sha);

-- 认可的输入版本：名字是标签，版本的身份是 inputs_sha（configs/ 与 skills/ 的内容哈希）。快照本身存在对象存储里。
CREATE TABLE eval_input_versions (
    tenant_id   varchar(64)   COLLATE Latin1_General_100_BIN2 NOT NULL CONSTRAINT fk_eval_input_versions_tenant REFERENCES tenants (tenant_id),
    inputs_sha  char(64)      NOT NULL,
    seq         int           NOT NULL,
    name        nvarchar(64)  NOT NULL,
    note        nvarchar(1024) NULL,
    created_at  datetime2     NOT NULL,
    CONSTRAINT pk_eval_input_versions PRIMARY KEY CLUSTERED (tenant_id, inputs_sha, seq)
);
