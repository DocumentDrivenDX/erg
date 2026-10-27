"""SQLite-specific read optimizations; unified SQL remains the parity oracle."""


# Match the eligibility and winner ordering in unified.CALLS_SELECT exactly.
def eligible(alias=""):
    p = alias + "." if alias else ""
    return "COALESCE({p}usage_kind,CASE WHEN {p}tool='pi' AND COALESCE({p}fresh_in,0)+COALESCE({p}cache_read,0)+COALESCE({p}cache_write,0)+COALESCE({p}out,0)=0 THEN 'unknown' ELSE 'observed' END)='observed'".format(
        p=p
    )


# Only local receipts need session suppression. Rank this small subset, then
# inspect native candidates with shared response identity and indexed UID winners.
# Do not rank the entire corpus merely to decide whether a local session exists.
SESSIONS_SELECT = (
    """WITH local_ranked AS (
 SELECT machine,uid,session,response_id,model,
 ROW_NUMBER() OVER(PARTITION BY CASE WHEN COALESCE(response_id,'')<>''
 THEN response_id||':'||COALESCE(model,'') ELSE uid END
 ORDER BY out DESC,ts DESC,machine) AS capture_rank
 FROM llm WHERE tool='local' AND """
    + eligible()
    + """
), local_matched AS (
 SELECT r.machine,r.response_id,r.model FROM local_ranked r
 WHERE r.capture_rank=1 AND r.response_id IS NOT NULL AND r.response_id<>''
 GROUP BY r.machine,r.response_id,r.model HAVING COUNT(*)=1 AND
 (SELECT COUNT(*) FROM llm n WHERE n.machine=r.machine AND n.response_id=r.response_id
 AND n.model=r.model AND n.response_id<>'' AND n.tool NOT IN ('local','gateway')
 AND """
    + eligible("n")
    + """ AND n.machine=(SELECT w.machine FROM llm w
 WHERE w.tool=n.tool AND w.uid=n.uid AND """
    + eligible("w")
    + """
 ORDER BY w.out DESC,w.ts DESC,w.machine LIMIT 1))=1
)
SELECT s.* FROM sessions s WHERE (COALESCE(s.provenance,'')<>'omnigent_metadata'
 OR NOT EXISTS(SELECT 1 FROM sessions n WHERE n.machine=s.machine AND n.conversation_id=s.conversation_id
 AND COALESCE(n.provenance,'')<>'omnigent_metadata')) AND
 (s.tool<>'local' OR NOT EXISTS(SELECT 1 FROM llm l WHERE l.machine=s.machine AND l.tool='local' AND l.session=s.session)
 OR (s.machine,s.session) IN (SELECT c.machine,c.session FROM local_ranked c WHERE c.capture_rank=1
 AND NOT EXISTS(SELECT 1 FROM local_matched m WHERE m.machine=c.machine
 AND m.response_id=c.response_id AND m.model=c.model)))"""
)


def native_session_calls(fields):
    # Find this session's candidate UIDs, but rank every sighting of each UID.
    # Applying the session predicate before winner selection would resurrect a
    # losing sighting if its winning copy belonged to a different session.
    return (
        """WITH selected AS (
 SELECT DISTINCT uid FROM llm WHERE tool=? AND session=? AND """
        + eligible()
        + """
), ranked AS (
 SELECT l.*,ROW_NUMBER() OVER(PARTITION BY l.tool,l.uid
 ORDER BY l.out DESC,l.ts DESC,l.machine) AS capture_rank
 FROM selected s CROSS JOIN llm l ON l.uid IS s.uid
 WHERE l.tool=? AND """
        + eligible("l")
        + """
) SELECT """
        + fields
        + " FROM ranked WHERE capture_rank=1 AND session=?"
    )


def initialize(db):
    db.execute(
        "CREATE INDEX IF NOT EXISTS llm_eligible_session ON llm(tool,session,uid) WHERE "
        + eligible()
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS prompts_capture_ranking ON prompts(tool,COALESCE(NULLIF(source_uid,''),uid),CASE WHEN origin IN ('human','automation') THEN 1 ELSE 0 END DESC,ts,machine,session) WHERE tool<>'codex' OR provenance IN ('codex_user_text','codex_user_event')"
    )
    db.execute("CREATE INDEX IF NOT EXISTS llm_session_identity ON llm(machine,tool,session)")
    db.execute(
        "CREATE INDEX IF NOT EXISTS sessions_conversation_identity ON sessions(machine,conversation_id,provenance)"
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS llm_eligible_winner ON llm(tool,uid,out DESC,ts DESC,machine) WHERE "
        + eligible()
    )
    old = db.execute(
        "SELECT sql FROM sqlite_master WHERE type='view' AND name='report_sessions'"
    ).fetchone()
    if old and old[0].split(" AS ", 1)[-1] != SESSIONS_SELECT:
        db.execute("DROP VIEW report_sessions")
    db.execute("CREATE VIEW IF NOT EXISTS report_sessions AS " + SESSIONS_SELECT)


def calls_select(columns, db=None):
    """Keep canonical ranking/matching unchanged while materializing less data.

    Ranking CTEs are reused by SQLite and can materialize every source column even
    when the outer report needs only a few. Always retain every matching/ranking
    key; eligibility is evaluated against the original llm table before ranking.
    """
    import re

    from . import unified

    if (
        not isinstance(columns, (list, tuple))
        or not columns
        or any(not isinstance(c, str) or not re.fullmatch(r"[a-z_][a-z0-9_]*", c) for c in columns)
        or len(set(columns)) != len(columns)
    ):
        raise ValueError("explicit unique column names required")
    if db is not None:
        import sqlite3

        from . import call_projection

        if isinstance(db, sqlite3.Connection):
            try:
                cached = call_projection.cached_select(db, columns)
            except sqlite3.Error:
                cached = None
            if cached is not None:
                return cached
    required = ("tool", "uid", "out", "ts", "machine", "model", "request_id", "response_id")
    selected = list(dict.fromkeys([c for c in columns if c != "capture_rank"] + list(required)))
    marker = "SELECT *,ROW_NUMBER()"
    if unified.CALLS_SELECT.count(marker) != 1:
        raise ValueError("unsupported canonical ranking projection")
    sql = unified.CALLS_SELECT.replace(marker, "SELECT " + ",".join(selected) + ",ROW_NUMBER()", 1)
    return "SELECT " + ",".join(columns) + " FROM (" + sql + ")"
