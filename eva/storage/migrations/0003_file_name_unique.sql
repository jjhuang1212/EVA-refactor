-- 同一租户内未删除的文件名不重复：引用靠文件名定位唯一一份文件。登记时会把同名的新文件改名，
-- 这个索引兜住并发上传同名文件的情形。已删除的文件不占名字。
CREATE UNIQUE INDEX ux_file_registry_tenant_filename ON file_registry (tenant_id, filename) WHERE status <> 'deleted';
