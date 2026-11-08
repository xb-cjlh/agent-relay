ALTER TABLE agents
 ADD COLUMN merged_into VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL,
 ADD CONSTRAINT agents_merged_target FOREIGN KEY(merged_into) REFERENCES agents(id);

ALTER TABLE messages
 ADD COLUMN duplicate_of VARCHAR(64) CHARACTER SET ascii COLLATE ascii_bin NULL;
