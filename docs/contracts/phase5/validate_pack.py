#!/usr/bin/env python3
"""Offline checks of this specification pack; no implementation or deployment tests."""
from pathlib import Path
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sys
from collections import Counter
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parent

def load(name):
    return json.loads((ROOT / name).read_text(encoding='utf-8'))

def require(condition, message):
    if not condition:
        raise AssertionError(message)

def strict_interval(value):
    return type(value) is int and 1 <= value <= 90

def semantic(definition, value):
    if definition == 'FaultConfiguration':
        require(all(type(v) is int and v == 1 for v in value.values()), 'fault counters must be strict integer 1')
    if definition == 'SystemSetupRequest' and 'faults' in value.get('setup', {}):
        semantic('FaultConfiguration', value['setup']['faults'])
    if definition == 'ScheduleRequest' and value.get('enabled'):
        require(strict_interval(value.get('intervalDays')), 'interval must be a strict integer')
    if definition in {'SuiteReport','OwnTeamSuiteReport','CrossTeamSuiteReport'}:
        ids=[r['test_id'] for r in value['results']]
        require(len(ids)==len(set(ids)), 'duplicate test IDs')
        for measure in value['coverage'].values():
            a,b=measure['covered'],measure['total']
            require((a is None)==(b is None), 'unavailable count pair')
            require(a is None or 0 <= a <= b, 'covered exceeds total')
    if definition=='TeamDesignMap':
        ids=[r['requirementId'] for r in value['requirements']]
        require(len(ids)==len(set(ids)), 'duplicate design requirement IDs')

schema=load('phase5-wire-schemas.json')
Draft202012Validator.check_schema(schema)
validators={name:Draft202012Validator({'$schema':schema['$schema'],'$defs':schema['$defs'],'$ref':'#/$defs/'+name},format_checker=FormatChecker()) for name in schema['$defs']}
for name in schema['$defs']:
    # Each definition has to be a valid schema, not only definitions touched by examples.
    Draft202012Validator.check_schema(schema['$defs'][name])
examples=load('phase5-examples.json')
for i,e in enumerate(examples['examples']):
    validators[e['definition']].validate(e['value'])
    semantic(e['definition'],e['value'])
    validators['SuiteReport'].validate(e['value']) if e['definition'].endswith('TeamSuiteReport') else None
for i,e in enumerate(examples['invalid_examples']):
    rejected=False
    try:
        validators[e['definition']].validate(e['value'])
        semantic(e['definition'],e['value'])
    except Exception as exc:
        # Validation errors/assertions are expected; programming errors are not.
        from jsonschema import ValidationError
        if not isinstance(exc,(ValidationError,AssertionError)):
            raise
        rejected=True
    require(rejected, f'negative example {i} unexpectedly accepted')

vectors=load('phase5-policy-vectors.json')
def stamp(text): return datetime.fromisoformat(text.replace('Z','+00:00'))
def show(t): return t.isoformat().replace('+00:00','Z')
for v in vectors['intervals']:
    require(strict_interval(v['intervalDays'])==v['valid'],'strict interval vector '+v['name'])
for v in vectors['initial_due']:
    require(show(stamp(v['now'])+timedelta(days=v['intervalDays']))==v['expected'],'initial due vector')
for v in vectors['cadence']:
    due,now=stamp(v['due']),stamp(v['now'])
    interval=timedelta(days=v['intervalDays'])
    count=1 if now>=due else 0
    nxt=due+((now-due)//interval+1)*interval if count else due
    require(count==v['audits'] and show(nxt)==v['nextAuditAt'],'cadence '+v['name'])
for v in vectors['reminder_dates']:
    deadline=stamp(v['deadline']+'T00:00:00Z')
    for offset,expected in v['expected'].items():
        require(show(deadline-timedelta(days=int(offset)))==expected,'reminder calendar '+v['deadline'])
for v in vectors['reminder_eligibility']:
    eligible=v['latestStatus'] in {None,'PROCESSING','PROCESSING_FAILED'} or v['findingCount']>0
    require(eligible==v['eligible'],'reminder eligibility')
for v in vectors['reminder_crossing']:
    due=stamp(v['deadline']+'T00:00:00Z');configured,new=stamp(v['configuredAt']),stamp(v['new'])
    offsets=[n for n in [90,30,7] if n not in v['alreadyConsidered'] and configured<=due-timedelta(days=n)<=new]
    require(offsets==v['expectedOffsets'],'reminder crossing '+v['name'])

plan=load('phase5-test-plan.json')
ids=[t['test_id'] for t in plan['tests']]
require(len(ids)==len(set(ids)), 'duplicate plan IDs')
require(len(plan['components'])==12,'component denominator')
require(list(plan['acceptance'])==[f'AC-{i:02}' for i in range(1,19)],'acceptance denominator/order')
preceding=set()
ops=set(range(1,15))|{'S','V','R','G'}
for t in plan['tests']:
    require(set(t['prerequisites'])<=preceding,'missing/forward prerequisite '+t['test_id'])
    require(set(t['components'])<=set(plan['components']),'unknown component '+t['test_id'])
    require(set(t['acceptance'])<=set(plan['acceptance']),'unknown AC '+t['test_id'])
    require(set(t['operations'])<=ops,'unknown operation '+t['test_id'])
    require(t['qualifying_assertion'].strip(),'missing assertion')
    preceding.add(t['test_id'])
require(dict(Counter(t['tier'] for t in plan['tests']))==plan['counts_by_tier'],'plan counts')
old=load('baseline_phase4/phase4-test-plan.json')
require([t['test_id'] for t in old['tests']]==ids[:len(old['tests'])],'inherited IDs/order changed')
require(plan['new_entries']==len(plan['tests'])-len(old['tests']),'new entry count')
# The inherited pack validates against its own hashes. No historical bytes are edited.
for line in (ROOT/'baseline_phase4/SHA256SUMS.txt').read_text().splitlines():
    digest,name=line.split(maxsplit=1)
    require(hashlib.sha256((ROOT/'baseline_phase4'/name.strip().lstrip('*')).read_bytes()).hexdigest()==digest,'baseline hash '+name)
hashes=ROOT/'SHA256SUMS.txt'
if hashes.exists():
    actual=set()
    for line in hashes.read_text().splitlines():
        digest,name=line.split(maxsplit=1);name=name.strip().lstrip('*');actual.add(name)
        require(hashlib.sha256((ROOT/name).read_bytes()).hexdigest()==digest,'pack hash '+name)
    expected={str(p.relative_to(ROOT)) for p in ROOT.rglob('*') if p.is_file() and p.name!='SHA256SUMS.txt'}
    require(actual==expected,'SHA256 manifest member list differs')
print(json.dumps({'status':'passed','schema_definitions':len(schema['$defs']),'valid_examples':len(examples['examples']),'negative_examples_rejected':len(examples['invalid_examples']),'plan_entries':len(ids),'new_plan_entries':plan['new_entries'],'counts_by_tier':plan['counts_by_tier'],'hash_manifest_checked':hashes.exists(),'scope':'Artifact consistency only; no implementation tests, human acceptance, adoption or deployment verified.'},indent=2))
