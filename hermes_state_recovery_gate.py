"""Keep rollback archives intact until their runtime ledger is restored offline."""

REVERSAL_PENDING_KEY = 'private_update_reversal_pending_forward_restore'


def require_restored_runtime_ledger(conn):
    """Refuse before schema reconciliation can create an empty replacement ledger."""
    if not conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='state_meta'").fetchone():
        return
    if conn.execute('SELECT 1 FROM state_meta WHERE key=?', (REVERSAL_PENDING_KEY,)).fetchone():
        raise RuntimeError(
            'This database was reversed for recovery; restore the saved runtime ledger '
            'with the verified offline recovery tooling before starting this version. '
            'Do not remove the reversal marker or recreate empty admission tables.'
        )
