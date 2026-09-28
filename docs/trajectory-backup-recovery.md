# Recursive backup and recovery boundary

Before the interface/correctness hardening, all three current working trees were flattened recursively, including dirty and untracked non-ignored files in nested Git projects. Both GitHub and Gitea received the branch `codex/backup-complete-trajectory-20260928-v2`. Independent object databases fetched each host; every archived file was SHA-256 compared with the pre-edit inventory. No gitlinks remain in these snapshots. See trajectory-backup-complete-20260928.json for exact commits and counts. Ignored environments, runtime data, credentials, and Git internals are not source backups.

The earlier `codex/backup-before-trajectory-20260928` snapshots contain nested gitlinks. They do not guarantee exact restoration of dirty/untracked nested files from before the first feature implementation. That pre-change gap cannot be reconstructed with certainty from those snapshots. Preserve those refs as partial history; do not describe them as complete recursive backups. The new snapshots protect the complete current source state before this hardening.

Restore into a separate directory from the backup branch on either host. Do not reset an active checkout or replace production runtime databases. Production deployment and real notification delivery remain unapproved.
