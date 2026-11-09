CREATE TABLE job_approvals (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 job_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 method VARCHAR(80) NOT NULL,
 details JSON NOT NULL,
 decision VARCHAR(16) NOT NULL DEFAULT 'pending',
 created_at DOUBLE NOT NULL,
 decided_at DOUBLE NULL,
 KEY approvals_job(job_id,created_at),
 CONSTRAINT approval_job FOREIGN KEY(job_id) REFERENCES jobs(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
