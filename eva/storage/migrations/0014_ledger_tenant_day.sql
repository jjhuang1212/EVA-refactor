-- 预算按租户、按天计：预扣时对本租户当天的账本行求和并加范围锁，没有这个索引就只能扫整个租户的历史。
CREATE INDEX ix_ledger_events_tenant_created ON ledger_events (tenant_id, created_at);
