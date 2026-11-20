-- 渠道实际收到、发出的每条消息，只追加。只用来重建对话和排障，主流程不读不依赖，删掉这张表不影响运行。
-- 按 seq 排先后：同一租户同一时间只有一条消息在处理，落库顺序就是对话顺序；时间列在 Windows 上分辨率不够，不能当排序键。
-- file_id 只是出处，内容在 file_registry；超限被拒的文件和语音没有文件，为空。
CREATE TABLE channel_messages (
    seq        bigint        IDENTITY(1,1) NOT NULL CONSTRAINT pk_channel_messages PRIMARY KEY,
    tenant_id  varchar(64)   COLLATE Latin1_General_100_BIN2 NOT NULL
               CONSTRAINT fk_channel_messages_tenant REFERENCES tenants (tenant_id),
    direction  varchar(8)    NOT NULL,
    msg_id     varchar(128)  NOT NULL,
    kind       varchar(16)   NOT NULL,
    text       nvarchar(max) NULL,
    file_id    varchar(40)   NULL CONSTRAINT fk_channel_messages_file REFERENCES file_registry (file_id),
    filename   nvarchar(512) NULL,
    bytes      bigint        NULL,
    created_at datetime2     NOT NULL,
    CONSTRAINT uq_channel_messages_msg UNIQUE (tenant_id, direction, msg_id)
);
CREATE INDEX ix_channel_messages_tenant_seq ON channel_messages (tenant_id, seq);
