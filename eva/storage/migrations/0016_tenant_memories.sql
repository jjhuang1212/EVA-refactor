-- 租户记忆：用户明确要求记住的偏好和纠正，只属于这个租户，不进公共层。只追加：
-- add 是记下一条，forget 是用 target_seq 指向被忘掉的那条 add，生效的是没有被 forget 指向的 add。
-- 按 seq 排先后，人排查靠它；call_id 是写它的那次工具调用，对应 transcript 里的 tool_call 事件。
CREATE TABLE tenant_memories (
    seq        bigint        IDENTITY(1,1) NOT NULL CONSTRAINT pk_tenant_memories PRIMARY KEY,
    tenant_id  varchar(64)   COLLATE Latin1_General_100_BIN2 NOT NULL
               CONSTRAINT fk_tenant_memories_tenant REFERENCES tenants (tenant_id),
    op         varchar(8)    NOT NULL,
    text       nvarchar(1000) NULL,
    target_seq bigint        NULL CONSTRAINT fk_tenant_memories_target REFERENCES tenant_memories (seq),
    session_id varchar(40)   NULL CONSTRAINT fk_tenant_memories_session REFERENCES sessions (session_id),
    call_id    varchar(64)   NULL,
    created_at datetime2     NOT NULL,
    CONSTRAINT ck_tenant_memories_op CHECK (
        (op = 'add' AND text IS NOT NULL AND target_seq IS NULL)
        OR (op = 'forget' AND text IS NULL AND target_seq IS NOT NULL))
);
CREATE INDEX ix_tenant_memories_tenant_seq ON tenant_memories (tenant_id, seq);
