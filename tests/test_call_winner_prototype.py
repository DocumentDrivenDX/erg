"""Static indexed winners must preserve the portable canonical reconciliation."""
from collections import Counter
import importlib.util
from pathlib import Path
import random
import sqlite3
import unittest
from erg import store,unified
spec=importlib.util.spec_from_file_location('winner_prototype',Path(__file__).resolve().parents[1]/'scripts/benchmark-call-winner-cache.py')
prototype=importlib.util.module_from_spec(spec);spec.loader.exec_module(prototype)

class CallWinnerPrototype(unittest.TestCase):
 def test_every_row_and_column_matches_for_replays_nulls_and_ambiguous_identities(self):
  for seed in range(20):
   db=sqlite3.connect(':memory:');store.initialize_db(db)
   try:
    rng=random.Random(seed)
    with db:
     for i in range(400):
      row=[rng.choice(('a','b','c',None)),rng.choice(('codex','claude','pi','local','gateway',None)),str(rng.randrange(35)),rng.choice((None,1,2,3)),rng.choice(('s','child','other')),rng.choice((None,'','m','m:n','n')),rng.choice((None,'','r','r:m','r:m:n')),rng.choice((None,'','request','other')),rng.choice((None,0,1,9)),rng.choice((None,0,1)),rng.choice((None,'observed','unknown','synthetic_history'))]
      db.execute('INSERT OR REPLACE INTO llm(machine,tool,uid,ts,session,model,response_id,request_id,out,fresh_in,usage_kind) VALUES('+','.join('?' for _ in row)+')',row)
    db.execute('BEGIN');prototype.seed(db)
    self.assertEqual(Counter(db.execute(prototype.CACHED_SELECT)),Counter(db.execute(unified.CALLS_SELECT)),seed)
   finally:db.close()
 def test_matching_counts_use_all_winners_before_local_suppression(self):
  db=sqlite3.connect(':memory:');self.addCleanup(db.close);store.initialize_db(db)
  with db:
   for uid,tool in [('native','codex'),('receipt','local'),('gateway','gateway')]:
    db.execute("INSERT INTO llm(machine,uid,tool,session,ts,model,fresh_in,out,response_id,request_id) VALUES('host',?,?,?,1,'m',1,1,'response','request')",(uid,tool,uid))
  db.execute('BEGIN');prototype.seed(db)
  fields='tool,uid'
  actual=set(db.execute('SELECT '+fields+' FROM ('+prototype.CACHED_SELECT+')'))
  expected=set(db.execute('SELECT '+fields+' FROM ('+unified.CALLS_SELECT+')'))
  self.assertEqual(actual,expected);self.assertEqual(actual,{('codex','native'),('gateway','gateway')})
  # The suppressed local winner still makes gateway membership ambiguous.
