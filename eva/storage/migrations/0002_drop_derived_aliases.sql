-- 与 company_master 同名字段文字相同的 SHORT / FULL 别名是派生数据，不落表，查名字时直接查主表。
-- 文字不同的（如主表简称带 XD、*ST 前缀，别名是干净的叫法）仍是独立信息，保留。
DELETE a
FROM company_aliases a
JOIN company_master m ON m.uscc = a.uscc
WHERE (a.alias_kind = 'FULL' AND a.alias = m.full_name)
   OR (a.alias_kind = 'SHORT' AND a.alias = m.short_name);

CREATE INDEX ix_company_master_full_name ON company_master (full_name);
CREATE INDEX ix_company_master_short_name ON company_master (short_name);
