CREATE TABLE conversation_shares (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 owner_user_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 agent_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
 title VARCHAR(256) NOT NULL,
 snapshot MEDIUMTEXT NOT NULL, resources MEDIUMTEXT NOT NULL,
 created_at DOUBLE NOT NULL, expires_at DOUBLE NOT NULL, revoked_at DOUBLE NULL,
 KEY shares_owner_agent(owner_user_id,agent_id,created_at),
 KEY shares_expiry(expires_at),
 CONSTRAINT share_owner FOREIGN KEY(owner_user_id) REFERENCES app_users(id),
 CONSTRAINT share_agent FOREIGN KEY(agent_id) REFERENCES agents(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
