ALTER TABLE images ADD COLUMN object_key VARCHAR(768) CHARACTER SET ascii COLLATE ascii_bin NOT NULL DEFAULT '';
CREATE TABLE image_uploads (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 owner_user_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 image_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
 object_key VARCHAR(768) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 name VARCHAR(255) NOT NULL, mime VARCHAR(32) NOT NULL, size BIGINT NOT NULL,
 sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 content_md5 CHAR(24) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 created_at DOUBLE NOT NULL, expires_at DOUBLE NOT NULL, completed_at DOUBLE NULL, cleaned_at DOUBLE NULL,
 KEY image_uploads_owner(owner_user_id,created_at),
 CONSTRAINT image_upload_user FOREIGN KEY(owner_user_id) REFERENCES app_users(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
