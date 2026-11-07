"""A reversed store must keep its archived admission receipts until restoration."""
import sqlite3

import pytest

from hermes_state import SessionDB


@pytest.mark.parametrize('manifest', ['_hermes_20261106_saved_manifest', 'unrecognized-manifest'])
def test_candidate_refuses_reversed_store_without_recreating_ledger(tmp_path, manifest):
    path = tmp_path / 'state.db'
    with SessionDB(path) as db:
        db.create_session('retained', source='test')
        db.append_message('retained', role='user', content='Activity after the update')
    with sqlite3.connect(path) as conn:
        conn.execute('UPDATE schema_version SET version=30')
        conn.execute('INSERT INTO state_meta(key,value) VALUES(?,?)',
                     ('private_update_reversal_pending_forward_restore', manifest))
        conn.commit()
        before = list(conn.iterdump())
    with pytest.raises(RuntimeError, match='restore the saved runtime ledger'):
        SessionDB(path)
    with sqlite3.connect(path) as conn:
        assert list(conn.iterdump()) == before


def test_unmarked_store_remains_writable(tmp_path):
    path = tmp_path / 'state.db'
    with SessionDB(path) as db:
        db.create_session('continued', source='test')
    with SessionDB(path) as db:
        db.append_message('continued', role='user', content='Continued normally')
        assert db.get_messages('continued')[0]['content'] == 'Continued normally'
