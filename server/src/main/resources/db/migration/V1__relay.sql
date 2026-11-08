CREATE TABLE devices (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 name VARCHAR(80) NOT NULL UNIQUE,
 token_hash CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
 platform VARCHAR(100) NOT NULL,
 roots LONGTEXT NOT NULL, capabilities LONGTEXT NOT NULL, processes LONGTEXT NOT NULL,
 last_seen DOUBLE NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE agents (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 device_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 name VARCHAR(128) NOT NULL, kind VARCHAR(20) NOT NULL, cwd TEXT NOT NULL,
 session_id VARCHAR(200), created_at DOUBLE NOT NULL,
 origin VARCHAR(16) NOT NULL DEFAULT 'relay', external_busy INT NOT NULL DEFAULT 0,
 last_activity DOUBLE NOT NULL DEFAULT 0,
 CONSTRAINT agents_device FOREIGN KEY(device_id) REFERENCES devices(id),
 UNIQUE KEY agents_name(device_id,name),
 KEY agents_session(device_id,kind,session_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE jobs (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 device_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 agent_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
 kind VARCHAR(20) NOT NULL, payload LONGTEXT NOT NULL, status VARCHAR(20) NOT NULL,
 output LONGTEXT NOT NULL, error TEXT, created_at DOUBLE NOT NULL,
 started_at DOUBLE, finished_at DOUBLE, lease_until DOUBLE,
 artifact_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin,
 CONSTRAINT jobs_device FOREIGN KEY(device_id) REFERENCES devices(id),
 CONSTRAINT jobs_agent_fk FOREIGN KEY(agent_id) REFERENCES agents(id),
 KEY jobs_queue(device_id,status,created_at), KEY jobs_agent(agent_id,created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE artifacts (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 job_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL UNIQUE,
 name VARCHAR(255) NOT NULL, size BIGINT NOT NULL,
 sha256 CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL, created_at DOUBLE NOT NULL,
 CONSTRAINT artifacts_job FOREIGN KEY(job_id) REFERENCES jobs(id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;

CREATE TABLE messages (
 id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin PRIMARY KEY,
 agent_id VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NOT NULL,
 role VARCHAR(16) NOT NULL, content LONGTEXT NOT NULL, created_at DOUBLE NOT NULL,
 CONSTRAINT messages_agent_fk FOREIGN KEY(agent_id) REFERENCES agents(id),
 KEY messages_agent(agent_id,created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_bin;
