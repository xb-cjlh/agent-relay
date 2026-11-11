CREATE TABLE file_objects (
 artifact_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 object_key VARCHAR(512),
 checked_at DOUBLE NOT NULL,
 expires_at DOUBLE NOT NULL,
 KEY file_objects_expiry(expires_at),
 CONSTRAINT file_objects_artifact FOREIGN KEY(artifact_id) REFERENCES artifacts(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE file_cache (
 device_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 path_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 path TEXT NOT NULL,
 artifact_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 PRIMARY KEY(device_id,path_hash),
 CONSTRAINT file_cache_device FOREIGN KEY(device_id) REFERENCES devices(id),
 CONSTRAINT file_cache_artifact FOREIGN KEY(artifact_id) REFERENCES artifacts(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE file_uploads (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 job_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
 artifact_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 object_key VARCHAR(512) NOT NULL,
 name VARCHAR(255) NOT NULL, size BIGINT NOT NULL,
 sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 content_md5 VARCHAR(24) NOT NULL,
 expires_at DOUBLE NOT NULL,
 cleaned_at DOUBLE,
 CONSTRAINT file_upload_job FOREIGN KEY(job_id) REFERENCES jobs(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
