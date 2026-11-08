ALTER TABLE devices
 ADD COLUMN machine_id CHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
 ADD COLUMN merged_into VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
 ADD UNIQUE KEY devices_machine(machine_id),
 ADD CONSTRAINT devices_merged_target FOREIGN KEY(merged_into) REFERENCES devices(id);

ALTER TABLE agents
 ADD COLUMN controllable INT NOT NULL DEFAULT 1,
 ADD COLUMN control_reason VARCHAR(32) NOT NULL DEFAULT '',
 ADD COLUMN message_namespace VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL;
