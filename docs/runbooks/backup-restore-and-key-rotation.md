# Runbook: Backup / Restore Drill & Encryption Key Rotation

## Automated Backup & Restore Verification Drill
Run the verification script regularly and archive its JSON report:
```bash
python scripts/backup_restore_drill.py
```
The drill:
1. Takes an online SQLite backup (`sqlite3.Connection.backup`) — or for PostgreSQL, use `pg_dump --format=custom --no-owner` — and copies `ObjectStore` blobs + SHA-256 manifest.
2. Restores into an isolated temporary database + object store directory.
3. Verifies SQLite `PRAGMA integrity_check == "ok"`, critical table row-count parity, and per-object SHA-256 verification.

For PostgreSQL production deployments:
1. Dump with owner credentials cleanly: `pg_dump -Fc -f backup.dump "$PG_OWNER_URL"`.
2. Provision target database with `deploy/postgres/init-rls-role.sql` (`ai_sdlc_app` `NOBYPASSRLS`).
3. Restore with `pg_restore --clean --if-exists -d "$TARGET_PG_OWNER_URL" backup.dump` and run `alembic upgrade head`.
4. Point API/worker traffic at the `ai_sdlc_app` connection string.

## BYOK Encryption Key Rotation (`EnvelopeCipher`)
1. `CredentialRef` stores `key_version` and envelope-encrypted ciphertext via `EnvelopeCipher` (`src/ai_sdlc_saas/security/crypto.py`).
2. To rotate master key material:
   - Maintain previous key ID in `EnvelopeCipher` keyring (`keyrings` / previous version decryption support) while setting the new active `key_version`.
   - Re-encrypt active `CredentialRef` rows in a background migration batch (`revoked_at IS NULL`), updating `encrypted_secret` and `key_version`.
   - After 100% of active rows match the new `key_version`, retire the old KEK from runtime config and log `credential.rotated` audit events.
3. Also rotate `AI_SDLC_JWT_SECRET` using dual-key verification windows if zero-downtime session continuity is needed, or revoke sessions to force clean re-authentication.
