"""Match Databricks' exec-without-__file__ task bootstrap, without Spark/network."""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

class Bootstrap(unittest.TestCase):
 def test_default_disabled_exec_without_file_does_not_import_spark_or_secret_runtime(self):
  path=Path(__file__).resolve().parents[1]/'central/databricks/job.py'
  args=['job.py']
  for name in ('action','workspace','workspace-id','zerobus-endpoint','catalog','ingest-schema','telemetry-schema','spool-volume','control-volume','client-id','secret-scope','secret-key','oauth-scope','application-archive'):
   args.extend(['--'+name,'synthetic'])
  args.extend(['--enabled','false'])
  with patch.object(sys,'argv',args),self.assertRaisesRegex(SystemExit,'central execution disabled'):
   exec(compile(path.read_text(),str(path),'exec'),{'__name__':'__main__'})
