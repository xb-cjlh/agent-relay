CREATE TABLE notifications (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 owner_user_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 title VARCHAR(120) NOT NULL, body VARCHAR(2000) NOT NULL,
 agent_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
 job_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
 source VARCHAR(20) NOT NULL, dedupe_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 created_at DOUBLE NOT NULL, read_at DOUBLE NULL,
 UNIQUE KEY notification_dedupe(owner_user_id,dedupe_hash),
 KEY notification_inbox(owner_user_id,created_at,id),
 CONSTRAINT notification_owner FOREIGN KEY(owner_user_id) REFERENCES app_users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
CREATE TABLE web_push_subscriptions (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 owner_user_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 session_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 endpoint TEXT NOT NULL, endpoint_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
 p256dh VARCHAR(100) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 auth VARCHAR(30) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 updated_at DOUBLE NOT NULL, enabled BOOLEAN NOT NULL DEFAULT TRUE,
 CONSTRAINT web_push_owner FOREIGN KEY(owner_user_id) REFERENCES app_users(id)
) ENGINE=InnoDB;
CREATE TABLE push_deliveries (
 notification_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 subscription_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 attempts INT NOT NULL DEFAULT 0, next_attempt DOUBLE NOT NULL,
 status VARCHAR(20) NOT NULL DEFAULT 'pending', last_error VARCHAR(80) NULL,
 PRIMARY KEY(notification_id,subscription_id), KEY push_queue(status,next_attempt),
 CONSTRAINT delivery_notification FOREIGN KEY(notification_id) REFERENCES notifications(id) ON DELETE CASCADE
) ENGINE=InnoDB;
