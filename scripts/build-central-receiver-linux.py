"""Build an immutable Linux x64/CPython3.12 Function artifact from public wheels.

Dependencies are installed only into a fresh temporary tree, never on the Mac
collector. No native session state, credential file, or Keychain data is accessed.
"""
import argparse
import email.parser
import hashlib
import json
import os
from pathlib import Path,PurePosixPath
import stat
import subprocess
import sys
import tempfile
import zipfile

ROOT=Path(__file__).resolve().parents[1]


def entry(name,directory=False):
    value=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));value.create_system=3
    value.external_attr=((stat.S_IFDIR|0o755) if directory else (stat.S_IFREG|0o644))<<16
    value.compress_type=zipfile.ZIP_STORED if directory else zipfile.ZIP_DEFLATED
    return value


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--reuse-dependencies-from',type=Path);args=p.parse_args()
    output=args.output.resolve()
    if output.exists() or output.suffix!='.zip':p.error('new immutable .zip output required')
    with tempfile.TemporaryDirectory(prefix='erg-linux312-build-') as temp:
        build=Path(temp);target=build/'site-packages';base=build/'base.zip'
        subprocess.run([sys.executable,'-B',str(ROOT/'scripts/package-central-receiver.py'),str(base)],check=True)
        env=dict(os.environ)
        for key in list(env):
            if key.startswith('PIP_'):env.pop(key)
        cmd=[sys.executable,'-m','pip','install','--disable-pip-version-check','--no-cache-dir','--no-compile',
             '--index-url','https://pypi.org/simple','--only-binary=:all:','--implementation','cp','--python-version','3.12',
             '--platform','manylinux_2_28_x86_64','--platform','manylinux2014_x86_64','--abi','cp312','--abi','abi3','--abi','none',
             '--target',str(target),'-r',str(ROOT/'central/receiver/requirements.txt')]
        if args.reuse_dependencies_from:
            previous=args.reuse_dependencies_from.resolve()
            metadata=json.loads(previous.with_suffix('.build.json').read_text())
            if hashlib.sha256(previous.read_bytes()).hexdigest()!=metadata['sha256'] or metadata['runtime']!='CPython3.12 Linux x86_64':
                raise ValueError('dependency artifact provenance mismatch')
            with zipfile.ZipFile(previous) as archive:
                if archive.read('requirements.txt')!=(ROOT/'central/receiver/requirements.txt').read_bytes():
                    raise ValueError('dependency requirements changed')
                prefix='.python_packages/lib/site-packages/'
                for info in archive.infolist():
                    if not info.filename.startswith(prefix) or info.is_dir():continue
                    relative=PurePosixPath(info.filename[len(prefix):])
                    if relative.is_absolute() or '..' in relative.parts:raise ValueError('unsafe dependency path')
                    dest=target/str(relative);dest.parent.mkdir(parents=True,exist_ok=True);dest.write_bytes(archive.read(info))
        else:
            subprocess.run(cmd,env=env,check=True)
        files={}
        with zipfile.ZipFile(base) as archive:
            for name in archive.namelist():files[name]=archive.read(name)
        native=0
        for path in target.rglob('*'):
            if path.is_symlink():raise ValueError('dependency symlinks unsupported')
            if not path.is_file():continue
            relative=path.relative_to(target)
            if '__pycache__' in relative.parts or relative.suffix=='.pyc' or relative.parts[0]=='bin':continue
            raw=path.read_bytes()
            if path.suffix=='.so':
                if raw[:4]!=b'\x7fELF' or raw[4]!=2 or raw[5]!=1 or int.from_bytes(raw[18:20],'little')!=62:
                    raise ValueError('dependency is not Linux x86_64 ELF')
                native+=1
            files['.python_packages/lib/site-packages/'+relative.as_posix()]=raw
        packages=[]
        for metadata in sorted(target.glob('*.dist-info/METADATA')):
            value=email.parser.Parser().parsestr(metadata.read_text())
            packages.append(dict(name=value['Name'],version=value['Version']))
        directories=set()
        for name in files:
            for parent in PurePosixPath(name).parents:
                if str(parent)!='.':directories.add(str(parent)+'/')
        with zipfile.ZipFile(output,'x') as archive:
            for name in sorted(directories):archive.writestr(entry(name,True),b'')
            for name,raw in sorted(files.items()):archive.writestr(entry(name),raw)
        manifest=dict(artifact=str(output),sha256=hashlib.sha256(output.read_bytes()).hexdigest(),bytes=output.stat().st_size,
                      runtime='CPython3.12 Linux x86_64',native_elf_libraries=native,packages=packages,
                      package_index='https://pypi.org/simple',private_state_included=False,files=len(files))
        output.with_suffix('.build.json').write_text(json.dumps(manifest,indent=2)+'\n')
        print(json.dumps(manifest,indent=2))

if __name__=='__main__':main()
