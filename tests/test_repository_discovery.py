"""Repository discovery avoids per-directory processes without losing Git layouts."""
import os
import shutil
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from erg import collect


@unittest.skipUnless(shutil.which('git'),'Git is required for repository-layout fixtures')
class RepositoryDiscovery(unittest.TestCase):
    def setUp(self):
        temporary=tempfile.TemporaryDirectory();self.addCleanup(temporary.cleanup)
        self.root=Path(temporary.name).resolve()
        clean={k:v for k,v in os.environ.items() if not k.startswith('GIT_')}
        env=patch.dict(os.environ,clean,clear=True);env.start();self.addCleanup(env.stop)
    def git(self,*args):
        return subprocess.run(['git',*map(str,args)],check=True,capture_output=True,text=True).stdout.strip()
    def repo(self,name='repo',*options):
        root=self.root/name
        self.git('init','--quiet',*options,root)
        self.git('-C',root,'-c','user.name=Fixture','-c','user.email=fixture@example.invalid','commit','--quiet','--allow-empty','-m','seed')
        return root
    def test_nested_directories_worktrees_and_symlinks_need_no_git_process(self):
        root=self.repo();nested=root/'src/deep';nested.mkdir(parents=True)
        worktree=self.root/'worktree';self.git('-C',root,'worktree','add','--quiet','--detach',worktree)
        (worktree/'inside').mkdir();alias=self.root/'alias';alias.symlink_to(root,target_is_directory=True)
        with patch.dict(os.environ,{'GIT_PAGER':'cat'}),patch.object(collect,'git',side_effect=AssertionError('spawned discovery helper')):
            self.assertEqual(collect.find_repos([str(nested),str(root),str(worktree/'inside'),str(alias/'src')],[]),[str(root)])
    def test_nested_repositories_remain_distinct_and_new_repository_is_discovered(self):
        outer=self.repo();inner=self.repo('repo/nested')
        empty=self.root/'empty';empty.mkdir()
        with patch.object(collect,'git',side_effect=AssertionError('spawned discovery helper')):
            self.assertEqual(collect.find_repos([str(empty)],[]),[])
            self.assertEqual(collect.find_repos([str(outer),str(inner)],[]),sorted(map(str,[outer,inner])))
        self.git('init','--quiet',empty)
        with patch.object(collect,'git',side_effect=AssertionError('stale negative cache or helper')):
            self.assertEqual(collect.find_repos([str(empty)],[]),[str(empty)])
    def test_custom_git_directory_and_malformed_marker_use_git_fallback(self):
        custom=self.repo('separate','--separate-git-dir',self.root/'external-objects')
        bad=self.root/'bad';bad.mkdir();(bad/'.git').write_text('not a git pointer\n')
        original=collect.git
        with patch.object(collect,'git',wraps=original) as helper:
            self.assertEqual(collect.find_repos([str(custom),str(bad)],[]),[str(custom)])
        self.assertGreaterEqual(helper.call_count,3)
    def test_git_environment_overrides_are_resolved_by_git(self):
        root=self.repo();unrelated=self.root/'unrelated';unrelated.mkdir()
        original=collect.git
        with patch.dict(os.environ,dict(GIT_DIR=str(root/'.git'),GIT_WORK_TREE=str(root))),patch.object(collect,'git',wraps=original) as helper:
            self.assertEqual(collect.find_repos([str(unrelated)],[]),[str(root)])
            helper.assert_called_once()
    def test_root_inventory_and_deleted_directories_are_preserved(self):
        first=self.repo('projects/first');second=self.repo('projects/group/second')
        self.assertEqual(collect.find_repos([str(self.root/'gone')],[str(self.root/'projects')]),sorted(map(str,[first,second])))
