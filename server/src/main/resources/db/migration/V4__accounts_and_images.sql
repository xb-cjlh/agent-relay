CREATE TABLE app_users (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 email VARCHAR(254) CHARACTER SET ascii COLLATE ascii_bin NULL UNIQUE,
 display_name VARCHAR(60) NOT NULL,
 password_hash VARCHAR(100) NULL,
 created_at DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE auth_sessions (
 token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 user_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 expires_at DOUBLE NOT NULL,
 CONSTRAINT session_user FOREIGN KEY(user_id) REFERENCES app_users(id)
) ENGINE=InnoDB;
CREATE TABLE auth_email_codes (
 email VARCHAR(254) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 purpose VARCHAR(16) NOT NULL,
 code_hash VARCHAR(100) NOT NULL,
 expires_at DOUBLE NOT NULL, sent_at DOUBLE NOT NULL, attempts INT NOT NULL DEFAULT 0,
 PRIMARY KEY(email,purpose)
) ENGINE=InnoDB;
CREATE TABLE auth_rate_limits (
 scope_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 window_started DOUBLE NOT NULL, attempts INT NOT NULL
) ENGINE=InnoDB;
CREATE TABLE agent_keys (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 user_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 name VARCHAR(60) NOT NULL,
 token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
 token_prefix VARCHAR(24) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 created_at DOUBLE NOT NULL, expires_at DOUBLE NOT NULL, last_used_at DOUBLE NULL, revoked_at DOUBLE NULL,
 KEY agent_keys_owner(user_id,created_at),
 CONSTRAINT key_user FOREIGN KEY(user_id) REFERENCES app_users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
ALTER TABLE devices
 ADD COLUMN owner_user_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 DROP INDEX name, DROP INDEX devices_machine,
 ADD UNIQUE KEY devices_owner_name(owner_user_id,name),
 ADD UNIQUE KEY devices_owner_machine(owner_user_id,machine_id),
 ADD CONSTRAINT device_owner FOREIGN KEY(owner_user_id) REFERENCES app_users(id);
CREATE TABLE images (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 owner_user_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 job_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
 name VARCHAR(255) NOT NULL, mime VARCHAR(32) NOT NULL, size BIGINT NOT NULL,
 sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 created_at DOUBLE NOT NULL,
 KEY images_owner(owner_user_id,created_at),
 CONSTRAINT image_user FOREIGN KEY(owner_user_id) REFERENCES app_users(id),
 CONSTRAINT image_job FOREIGN KEY(job_id) REFERENCES jobs(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
