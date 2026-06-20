-- Rename tables to match the project rename (disputatio -> elephant / "Eat the Elephant").
-- Pure renames: no data, columns, or constraints are touched.

ALTER TABLE disputatio_users RENAME TO elephant_users;
ALTER TABLE disputatio_topics RENAME TO elephant_topics;
ALTER TABLE disputatio_seen RENAME TO elephant_seen;
ALTER TABLE disputatio_digests RENAME TO elephant_digests;
ALTER TABLE disputatio_feedback RENAME TO elephant_feedback;
