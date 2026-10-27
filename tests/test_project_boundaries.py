"""Keep policy and cost calculations independent of source IO and presentation."""
import ast
from pathlib import Path
import unittest

class ProjectBoundaries(unittest.TestCase):
    def test_project_policy_and_reports_do_not_import_source_or_ui_modules(self):
        root=Path(__file__).resolve().parents[1]/'erg'
        for filename,allowed in (('projects.py',set()),('project_reports.py',{'projects','rates','project_scope','report_scope'}),('project_scope.py',{'projects'}),('report_scope.py',set()),('project_admin.py',{'projects','project_reports'})):
            tree=ast.parse((root/filename).read_text())
            for node in ast.walk(tree):
                if isinstance(node,ast.ImportFrom) and node.level:
                    names={a.name for a in node.names} if node.module is None else {node.module.split('.')[0]}
                    self.assertLessEqual(names,allowed,filename)
                elif isinstance(node,ast.Import):
                    self.assertFalse(any(a.name.startswith('erg.') for a in node.names),filename)
