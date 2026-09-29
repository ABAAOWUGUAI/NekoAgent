"""Channel group facts, not participation settings or a second message queue."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import re
import sqlite3
import time


DDL = (
    "CREATE TABLE qq_group_bindings (epoch TEXT PRIMARY KEY, account_id TEXT NOT NULL, instance_id TEXT NOT NULL, last_seen REAL NOT NULL, last_snapshot REAL NOT NULL DEFAULT 0, sync_error TEXT NOT NULL DEFAULT '')",
    "CREATE TABLE qq_group_states (epoch TEXT NOT NULL REFERENCES qq_group_bindings(epoch), group_id TEXT NOT NULL, group_name TEXT NOT NULL DEFAULT '', facts_json TEXT NOT NULL DEFAULT '{}', barrier REAL NOT NULL DEFAULT 0, missing_start REAL NOT NULL DEFAULT 0, PRIMARY KEY(epoch,group_id))",
    "CREATE TABLE qq_group_events (event_key TEXT PRIMARY KEY, epoch TEXT NOT NULL REFERENCES qq_group_bindings(epoch), group_id TEXT NOT NULL, occurred_at REAL NOT NULL, received_at REAL NOT NULL, kind TEXT NOT NULL, detail_json TEXT NOT NULL)",
    "CREATE INDEX idx_qq_group_events ON qq_group_events(epoch,received_at DESC)",
)
GROUP_STATE_CHECKSUM = hashlib.sha256("\n".join(DDL).encode()).hexdigest()
FRESH_SECONDS = 600


def apply_group_state_v1(conn):
    for sql in DDL:
        conn.execute(sql)


def require_group_state_schema(conn):
    expected = {
        'qq_group_bindings': {'epoch','account_id','instance_id','last_seen','last_snapshot','sync_error'},
        'qq_group_states': {'epoch','group_id','group_name','facts_json','barrier','missing_start'},
        'qq_group_events': {'event_key','epoch','group_id','occurred_at','received_at','kind','detail_json'},
    }
    for table, columns in expected.items():
        if columns != {row[1] for row in conn.execute(f'PRAGMA table_info({table})')}:
            from bridge_migrations import MigrationDriftError
            raise MigrationDriftError('group_state_schema_drift')


def _exists(conn):
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE name='qq_group_bindings'").fetchone())


def _now(value=None):
    if value is None:
        return time.time()
    if isinstance(value, datetime):
        value = value.timestamp()
    if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
        raise ValueError('group_time_invalid')
    return float(value)


def _id(value, zero=False):
    if type(value) not in (int, str) or not re.fullmatch(r'[1-9][0-9]{0,19}|0', str(value)):
        raise ValueError('group_identifier_invalid')
    if not zero and str(value) == '0':
        raise ValueError('group_identifier_invalid')
    return str(value)


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True, allow_nan=False)


def _expected(conn):
    row = conn.execute('SELECT expected_bot_id FROM qq_channel_settings LIMIT 1').fetchone()
    return str(row[0] or '') if row else ''


def group_binding(conn, instance_id, now=None):
    """Server-derived generation from an authenticated, fresh runtime receipt."""
    current = _now(now)
    require_group_state_schema(conn)
    expected = _expected(conn)
    row = conn.execute('SELECT actual_bot_id,created_at,last_heartbeat_at FROM qq_channel_runtime_receipts WHERE channel_instance_id=?', (instance_id,)).fetchone()
    if not row or not expected or row[0] != expected:
        raise ValueError('group_account_binding_mismatch')
    heartbeat = datetime.fromisoformat(row[2].replace('Z', '+00:00')).timestamp()
    if not -5 <= current - heartbeat <= 180:
        raise ValueError('group_channel_heartbeat_stale')
    latest = conn.execute('SELECT channel_instance_id FROM qq_channel_runtime_receipts WHERE actual_bot_id=? ORDER BY last_heartbeat_at DESC,created_at DESC LIMIT 1', (expected,)).fetchone()
    if not latest or latest[0] != instance_id:
        raise ValueError('group_channel_instance_superseded')
    epoch = hashlib.sha256(_json([expected, instance_id, row[1]]).encode()).hexdigest()
    conn.execute('INSERT INTO qq_group_bindings(epoch,account_id,instance_id,last_seen) VALUES(?,?,?,?) ON CONFLICT(epoch) DO UPDATE SET last_seen=excluded.last_seen', (epoch,expected,instance_id,current))
    return epoch


def _active(conn):
    return conn.execute('SELECT epoch,account_id,instance_id,last_seen,last_snapshot,sync_error FROM qq_group_bindings WHERE account_id=? ORDER BY last_seen DESC LIMIT 1', (_expected(conn),)).fetchone()


def _state(conn, epoch, group):
    row = conn.execute('SELECT group_name,facts_json,barrier,missing_start FROM qq_group_states WHERE epoch=? AND group_id=?', (epoch,group)).fetchone()
    return {'name':row[0], 'facts':json.loads(row[1]), 'barrier':row[2], 'missing':row[3]} if row else {'name':'', 'facts':{}, 'barrier':0, 'missing':0}


def _save(conn, epoch, group, state):
    conn.execute('INSERT INTO qq_group_states(epoch,group_id,group_name,facts_json,barrier,missing_start) VALUES(?,?,?,?,?,?) ON CONFLICT(epoch,group_id) DO UPDATE SET group_name=excluded.group_name,facts_json=excluded.facts_json,barrier=excluded.barrier,missing_start=excluded.missing_start', (epoch,group,state['name'],_json(state['facts']),state['barrier'],state['missing']))


def _merge(state, field, value, at, received, source, key, *, snapshot_start=None, until=None):
    old = state['facts'].get(field, {})
    if snapshot_start is not None and old.get('received',0) > snapshot_start:
        return False
    if at < old.get('at',0):
        return False
    if at == old.get('at') and value != old.get('value'):
        value = 'unknown'
    state['facts'][field] = dict(value=value, at=at, received=received, source=source, key=key, until=until)
    # An unavailable role does not change the send gate when the group was
    # already joined and both mute facts were clear. Repeated LLBot snapshots
    # can report role=unknown while those three facts remain available; moving
    # the recovery barrier then invalidates an in-flight eligible reply.
    # Preserve the barrier on recovery from any previously unknown/muted fact.
    prior_available_without_role = (
        state['facts'].get('membership', {}).get('value') == 'joined'
        and state['facts'].get('mute_self', {}).get('value') == 'clear'
        and state['facts'].get('mute_all', {}).get('value') == 'clear'
    )
    if value in ('unknown','absent','muted') and not (
        field == 'role' and value == 'unknown' and prior_available_without_role
    ):
        state['barrier'] = max(state['barrier'], received)
    return True


def _notice(value, account, epoch):
    fields = {'account_id','binding_epoch','group_id','notice_type','sub_type','subject_id','operator_id','is_self','is_whole_group','occurred_at','duration_seconds','event_key'}
    if set(value) != fields or value['account_id'] != account or value['binding_epoch'] != epoch:
        raise ValueError('group_notice_identity_invalid')
    kind, sub = value['notice_type'], value['sub_type']
    choices = {'group_ban':('ban','lift_ban'), 'group_decrease':('leave','kick','kick_me'), 'group_increase':('approve','invite'), 'group_admin':('set','unset')}
    if not isinstance(kind,str) or kind not in choices or sub not in choices[kind]:
        raise ValueError('group_notice_kind_invalid')
    _id(value['group_id'])
    subject = _id(value['subject_id'], zero=kind=='group_ban')
    if value['operator_id'] is not None:
        _id(value['operator_id'], zero=True)
    if type(value['occurred_at']) is not int or not 0 <= value['occurred_at'] <= 0:
        raise ValueError('group_notice_time_invalid')
    duration = value['duration_seconds']
    if duration is not None and (type(duration) is not int or not 0 <= duration <= 0):
        raise ValueError('group_notice_duration_invalid')
    if value['is_self'] is not (subject == account) or value['is_whole_group'] is not (subject=='0' and kind=='group_ban'):
        raise ValueError('group_notice_subject_invalid')
    canonical = {k:v for k,v in value.items() if k!='event_key'}
    if hashlib.sha256(_json(canonical).encode()).hexdigest() != value['event_key']:
        raise ValueError('group_notice_fingerprint_invalid')
    return value


def receive_group_observation(conn, payload, now=None):
    current = _now(now)
    if not isinstance(payload,dict) or set(payload) - {'kind','observation','channel_instance_id','group_id'}:
        raise ValueError('group_observation_payload_invalid')
    epoch = group_binding(conn, payload.get('channel_instance_id'), now=current)
    if payload.get('kind') == 'preflight':
        return {'gate':group_gate(conn,_id(payload.get('group_id')),now=current)}
    value = payload.get('observation')
    if not isinstance(value,dict) or value.get('binding_epoch') != epoch or value.get('account_id') != _expected(conn):
        raise ValueError('group_observation_binding_invalid')
    kind = payload.get('kind')
    if kind == 'notice':
        value = _notice(value, _expected(conn), epoch)
        at = value['occurred_at']
        if at > current + 5:
            raise ValueError('group_notice_future')
        key, group = value['event_key'],value['group_id']
        detail = {'sub_type':value['sub_type'], 'scope':'self' if value['is_self'] else 'whole_group' if value['is_whole_group'] else 'other_member'}
        event_kind = value['notice_type']
    elif kind == 'snapshot':
        if set(value) != {'status','complete','account_id','binding_epoch','started_at','finished_at','groups','error'}:
            raise ValueError('group_snapshot_fields_invalid')
        at, end = _now(value['started_at']), _now(value['finished_at'])
        if end < at or end > current+5 or current-at > 120:
            raise ValueError('group_snapshot_time_invalid')
        if value['status'] == 'failed' and value['complete'] is False and value['groups'] is None:
            conn.execute("UPDATE qq_group_bindings SET sync_error='group_snapshot_unavailable' WHERE epoch=?", (epoch,))
            return {'accepted':True, 'complete':False}
        rows = value['groups']
        if value['status'] != 'ok' or value['complete'] is not True or not isinstance(rows,list) or len(rows)>1000 or value['error']:
            raise ValueError('group_snapshot_incomplete')
        seen = set()
        for row in rows:
            if not isinstance(row,dict) or set(row) != {'group_id','group_name','membership','role','member_query_status','mute_me_until_raw','mute_all_until_raw'}:
                raise ValueError('group_snapshot_row_invalid')
            group = _id(row['group_id'])
            if group in seen or row['membership'] != 'joined' or not isinstance(row['group_name'],str) or len(row['group_name'])>200:
                raise ValueError('group_snapshot_row_invalid')
            if row['role'] not in ('unknown','owner','admin','member') or row['member_query_status'] not in ('ok','unavailable'):
                raise ValueError('group_snapshot_role_invalid')
            for field in ('mute_me_until_raw','mute_all_until_raw'):
                if row[field] is not None and (type(row[field]) is not int or not 0<=row[field]<=0):
                    raise ValueError('group_snapshot_mute_invalid')
            seen.add(group)
        key = 'snapshot:'+hashlib.sha256(_json(value).encode()).hexdigest()
        group, event_kind, detail = '', 'snapshot', {'group_count':len(rows)}
    else:
        raise ValueError('group_observation_kind_invalid')
    inserted = conn.execute('INSERT OR IGNORE INTO qq_group_events VALUES(?,?,?,?,?,?,?)', (key,epoch,group,at,current,event_kind,_json(detail))).rowcount
    if not inserted:
        return {'accepted':True,'duplicate':True}
    if kind == 'notice':
        state = _state(conn,epoch,group)
        scope = value['is_self'] or value['is_whole_group']
        joined_at = state['facts'].get('membership',{}).get('at',0)
        if scope and at >= joined_at:
            sub = value['sub_type']
            if event_kind == 'group_decrease' and value['is_self']:
                _merge(state,'membership','absent',at,current,event_kind,key)
                _merge(state,'leave_reason','leave' if sub=='leave' else 'kicked',at,current,event_kind,key)
            elif event_kind == 'group_increase' and value['is_self']:
                if _merge(state,'membership','joined',at,current,event_kind,key):
                    for field in ('role','mute_self','mute_all'):
                        _merge(state,field,'unknown',at,current,event_kind,key)
                    state['missing'] = 0
            elif event_kind == 'group_ban':
                until = at+value['duration_seconds'] if value['duration_seconds'] is not None else None
                _merge(state,'mute_all' if value['is_whole_group'] else 'mute_self', 'clear' if sub=='lift_ban' else 'muted',at,current,event_kind,key,until=until)
            elif event_kind == 'group_admin' and value['is_self']:
                _merge(state,'role','admin' if sub=='set' else 'member',at,current,event_kind,key)
            _save(conn,epoch,group,state)
    else:
        for row in rows:
            group = str(row['group_id'])
            state = _state(conn,epoch,group)
            if _merge(state,'membership','joined',int(at),current,'snapshot',key,snapshot_start=at):
                state['name'],state['missing'] = row['group_name'],0
                _merge(state,'leave_reason','',int(at),current,'snapshot',key,snapshot_start=at)
            role = row['role'] if row['member_query_status']=='ok' else 'unknown'
            _merge(state,'role',role,int(at),current,'snapshot',key,snapshot_start=at)
            for raw,field in (('mute_me_until_raw','mute_self'),('mute_all_until_raw','mute_all')):
                stamp = row[raw]
                # Installed LLBot compares second-based deadlines with now;
                # fresh no_cache snapshots may retain an already expired value.
                status = 'unknown' if stamp is None else 'muted' if stamp>current else 'clear'
                _merge(state,field,status,int(at),current,'snapshot',key,snapshot_start=at,until=stamp)
            _save(conn,epoch,group,state)
        for existing in conn.execute('SELECT group_id FROM qq_group_states WHERE epoch=?', (epoch,)).fetchall():
            group = existing[0]
            if group in seen:
                continue
            state = _state(conn,epoch,group)
            member = state['facts'].get('membership',{})
            if member.get('received',0)>at or member.get('at',0)>int(at) or member.get('value')=='absent':
                continue
            if state['missing'] and at>state['missing']:
                _merge(state,'membership','absent',int(at),current,'snapshot',key,snapshot_start=at)
                _merge(state,'leave_reason','unknown',int(at),current,'snapshot',key,snapshot_start=at)
            else:
                _merge(state,'membership','unknown',int(at),current,'snapshot',key,snapshot_start=at)
                state['missing'] = at
            _save(conn,epoch,group,state)
        conn.execute("UPDATE qq_group_bindings SET last_snapshot=MAX(last_snapshot,?),sync_error='' WHERE epoch=?", (current,epoch))
    return {'accepted':True,'duplicate':False}


def group_gate(conn, group_id, now=None, created_at=None):
    current = _now(now)
    if not _exists(conn) or not conn.execute('SELECT 1 FROM qq_group_bindings LIMIT 1').fetchone():
        return {'active':False,'allowed':True,'reason':'group_state_not_activated'}
    binding = _active(conn)
    if not binding:
        return {'active':True,'allowed':False,'reason':'group_account_unknown'}
    receipt = conn.execute('SELECT actual_bot_id,last_heartbeat_at FROM qq_channel_runtime_receipts WHERE channel_instance_id=?',(binding[2],)).fetchone()
    if not receipt or receipt[0] != binding[1] or current-datetime.fromisoformat(receipt[1].replace('Z','+00:00')).timestamp()>180:
        return {'active':True,'allowed':False,'reason':'group_channel_stale'}
    state = _state(conn,binding[0],str(group_id))
    facts = state['facts']
    get = lambda field: facts.get(field,{}).get('value','unknown')
    membership, own, whole, role = get('membership'),get('mute_self'),get('mute_all'),get('role')
    reason = ''
    if membership=='absent':
        reason='group_absent'
    elif own=='muted' or (whole=='muted' and role not in ('admin','owner')):
        reason='group_muted'
    elif membership!='joined' or own!='clear' or (whole!='clear' and role not in ('admin','owner')):
        reason='group_state_unknown'
    elif any(current-facts.get(field,{}).get('received',0)>FRESH_SECONDS for field in ('membership','mute_self','mute_all','role')):
        reason='group_state_stale'
    if not reason and created_at is not None:
        if isinstance(created_at,str):
            created_at=datetime.fromisoformat(created_at.replace('Z','+00:00')).timestamp()
        if _now(created_at)<=state['barrier']:
            reason='group_recovery_stale_reply'
    return {'active':True,'allowed':not reason,'reason':reason or 'group_available', 'binding_epoch':binding[0], 'barrier':state['barrier']}


def list_group_states(conn, now=None):
    current = _now(now)
    if not _exists(conn):
        return {'active':False,'groups':[],'events':[],'sync_error':'group_schema_unavailable'}
    binding = _active(conn)
    if not binding:
        return {'active':False,'groups':[],'events':[],'sync_error':'group_account_not_bound'}
    groups=[]
    for row in conn.execute('SELECT group_id FROM qq_group_states WHERE epoch=? ORDER BY group_id',(binding[0],)):
        group=row[0]; state=_state(conn,binding[0],group); facts=state['facts']
        get=lambda field: facts.get(field,{}).get('value','unknown')
        gate=group_gate(conn,group,now=current)
        observed=min((f.get('received',0) for f in facts.values()),default=0)
        groups.append({'group_id':group,'group_name':state['name'], 'membership':get('membership'), 'leave_reason':get('leave_reason'), 'role':get('role'), 'speaking':'allowed' if gate['allowed'] else 'muted' if gate['reason']=='group_muted' else 'unknown', 'reason':gate['reason'], 'freshness':'fresh' if current-observed<=FRESH_SECONDS else 'stale', 'observed_at':observed, 'barrier':state['barrier']})
    events=[dict(group_id=r[0],occurred_at=r[1],received_at=r[2],kind=r[3],**json.loads(r[4])) for r in conn.execute('SELECT group_id,occurred_at,received_at,kind,detail_json FROM qq_group_events WHERE epoch=? ORDER BY received_at DESC LIMIT 100',(binding[0],))]
    return {'active':True,'account_id':binding[1],'groups':groups,'events':events,'last_snapshot':binding[4],'sync_error':binding[5]}
