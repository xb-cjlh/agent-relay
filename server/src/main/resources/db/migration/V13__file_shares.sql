ALTER TABLE conversation_shares
 MODIFY COLUMN agent_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
 ADD COLUMN share_type VARCHAR(20) NOT NULL DEFAULT 'conversation',
 ADD COLUMN resource_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
 ADD KEY shares_owner_resource(owner_user_id,resource_id,created_at);
