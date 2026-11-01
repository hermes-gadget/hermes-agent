"""Voice transcripts and dispatch receipts, separate from model tool history."""
import json
import hashlib
import time


class SessionVoiceMixin:
    def voice_create_record(self, kind, ident, owner_key, payload, *, scope="", session_id="", status="pending"):
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        def create(conn):
            now = time.time()
            cur = conn.execute(
                "INSERT OR IGNORE INTO voice_assistant_records "
                "(kind,id,owner_key,conversation_key,session_id,fingerprint,status,payload,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)", (kind, ident, owner_key, scope, session_id or None,
                fingerprint, status, json.dumps(payload), now, now))
            row = dict(conn.execute("SELECT * FROM voice_assistant_records WHERE kind=? AND id=?", (kind, ident)).fetchone())
            if row["owner_key"] != owner_key or row["fingerprint"] != fingerprint:
                raise ValueError("Voice record ID belongs to a different operation.")
            row["claimed"] = cur.rowcount == 1
            row["payload"] = json.loads(row["payload"])
            return row
        return self._execute_write(create)

    def voice_records(self, kind, owner_key, *, scope=None, session_id=None, limit=100):
        query = "SELECT * FROM voice_assistant_records WHERE kind=? AND owner_key=?"
        params = [kind, owner_key]
        if scope is not None:
            query += " AND conversation_key=?"
            params.append(scope)
        if session_id is not None:
            query += " AND session_id IS ?"
            params.append(session_id or None)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(min(max(int(limit), 1), 500))
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in self._read_all(query, params)]

    def voice_change_record(self, kind, ident, owner_key, expected, status, payload=None):
        def change(conn):
            row = conn.execute("SELECT * FROM voice_assistant_records WHERE kind=? AND id=? AND owner_key=?",
                               (kind, ident, owner_key)).fetchone()
            if row is None or row["status"] not in expected:
                return None
            data = dict(row)
            content = payload if payload is not None else json.loads(data["payload"])
            now = time.time()
            conn.execute("UPDATE voice_assistant_records SET status=?,payload=?,updated_at=? WHERE kind=? AND id=?",
                         (status, json.dumps(content), now, kind, ident))
            return {**data, "status": status, "payload": content, "updated_at": now}
        return self._execute_write(change)

    def voice_focus_prompt(self, ident, owner_key, scope, session_id):
        def focus(conn):
            row = conn.execute(
                "SELECT status FROM voice_assistant_records WHERE kind='interaction' AND id=? AND owner_key=? "
                "AND conversation_key=? AND session_id IS ?", (ident, owner_key, scope, session_id or None)).fetchone()
            if row is None or row["status"] not in {"queued", "foreground"}:
                return False
            conn.execute("UPDATE voice_assistant_records SET status='queued' WHERE kind='interaction' "
                         "AND owner_key=? AND conversation_key=? AND session_id IS ? AND status='foreground'",
                         (owner_key, scope, session_id or None))
            conn.execute("UPDATE voice_assistant_records SET status='foreground' WHERE kind='interaction' AND id=?", (ident,))
            return True
        return self._execute_write(focus)

    def open_voice_call(self, call_id, conversation_key, user_id, session_id=""):
        self._execute_write(lambda conn: conn.execute(
            "INSERT OR IGNORE INTO voice_calls(call_id,conversation_key,user_id,session_id,started_at) "
            "VALUES(?,?,?,?,?)", (call_id, conversation_key, user_id, session_id or None, time.time())))

    def append_voice_segments(self, call_id, segments):
        rows = [(call_id, s["event_id"], s["speaker"], s["text"], s.get("start_ms"),
                 s.get("end_ms"), int(s.get("interrupted", False))) for s in segments]
        if rows:
            self._execute_write(lambda conn: conn.executemany(
                "INSERT OR IGNORE INTO voice_segments "
                "(call_id,event_id,speaker,text,start_ms,end_ms,interrupted) VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(call_id,event_id) DO UPDATE SET interrupted=MAX(interrupted,excluded.interrupted)", rows))

    def close_voice_call(self, call_id, summary=""):
        self._execute_write(lambda conn: conn.execute(
            "UPDATE voice_calls SET closed_at=?,summary=? WHERE call_id=?",
            (time.time(), summary, call_id)))

    def bind_voice_call(self, call_id, conversation_key, session_id):
        self._execute_write(lambda conn: conn.execute(
            "UPDATE voice_calls SET conversation_key=?,session_id=? WHERE call_id=?",
            (conversation_key, session_id or None, call_id)))

    def voice_history(self, conversation_key, user_id, session_id, limit=120):
        # A /new changes session_id. Compression callers supply the resolved lineage root.
        rows = self._read_all(
            "SELECT v.call_id,v.event_id,v.speaker,v.text,v.start_ms,v.end_ms,v.interrupted "
            "FROM voice_segments v JOIN voice_calls c ON c.call_id=v.call_id "
            "WHERE c.conversation_key=? AND c.user_id=? AND c.session_id IS ? "
            "ORDER BY c.started_at DESC,v.rowid DESC LIMIT ?",
            (conversation_key, user_id, session_id or None, min(max(int(limit), 1), 256)))
        return [dict(row) for row in reversed(rows)]

    def claim_voice_dispatch(self, request_id, owner_key, fingerprint, payload):
        """One durable claim before Discord I/O; uncertain admission is never auto-replayed."""
        def claim(conn):
            cur = conn.execute(
                "INSERT OR IGNORE INTO voice_dispatches "
                "(request_id,owner_key,fingerprint,status,payload,updated_at) VALUES(?,?,?,'preparing',?,?)",
                (request_id, owner_key, fingerprint, json.dumps(payload), time.time()))
            row = dict(conn.execute("SELECT * FROM voice_dispatches WHERE request_id=?", (request_id,)).fetchone())
            if row["owner_key"] != owner_key or row["fingerprint"] != fingerprint:
                raise ValueError("request ID belongs to a different dispatch")
            row["claimed"] = cur.rowcount == 1
            row["payload"] = json.loads(row["payload"])
            return row
        return self._execute_write(claim)

    def update_voice_dispatch(self, request_id, status, payload):
        self._execute_write(lambda conn: conn.execute(
            "UPDATE voice_dispatches SET status=?,payload=?,updated_at=? WHERE request_id=? "
            "AND (status!='reported' OR ?='reported')",
            (status, json.dumps(payload), time.time(), request_id, status)))

    def select_voice_project(self, call_id, project):
        self._execute_write(lambda conn: conn.execute(
            "UPDATE voice_calls SET selected_project=? WHERE call_id=?", (project, call_id)))

    def voice_selected_project(self, conversation_key, user_id, session_id):
        row = self._read_one(
            "SELECT selected_project FROM voice_calls WHERE conversation_key=? AND user_id=? AND session_id IS ? "
            "ORDER BY started_at DESC LIMIT 1", (conversation_key, user_id, session_id or None))
        return row[0] if row else ""

    def voice_dispatches(self, owner_key, limit=50):
        rows = self._read_all(
            "SELECT * FROM voice_dispatches WHERE owner_key=? ORDER BY updated_at DESC LIMIT ?",
            (owner_key, min(max(int(limit), 1), 100)))
        return [{**dict(row), "payload": json.loads(row["payload"])} for row in rows]
