-- 租户身份已含客服号，同一用户在不同客服号下是不同租户，会话不必再区分渠道账号，这一列从来没有写过值。
-- 万一别的环境里有过值，先拒绝，不静默丢数据。
IF EXISTS (SELECT 1 FROM sessions WHERE channel_account IS NOT NULL)
    THROW 50001, 'sessions.channel_account 里有数据，先确认再删除', 1;
DROP INDEX ix_sessions_tenant ON sessions;
ALTER TABLE sessions DROP COLUMN channel_account;
CREATE INDEX ix_sessions_tenant ON sessions (tenant_id, channel);
