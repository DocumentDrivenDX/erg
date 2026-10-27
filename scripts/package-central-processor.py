"""Build pure application ZIP for the bundle; never sync the whole checkout."""
from pathlib import Path
import sys
import zipfile
import stat
root = Path(__file__).resolve().parents[1]
output = Path(sys.argv[1]).resolve()
if output.suffix not in ('.zip','.pyz'): raise SystemExit('explicit .pyz or .zip output required')
files = {'erg/__init__.py':None,'erg/delivery_protocol.py':root/'erg'/'delivery_protocol.py'}
for name in ('__init__.py','ddl.py','zerobus.py','processor.py','receiver.py','verify.py'):
    files['erg_central/'+name] = root/'central'/'erg_central'/name
with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
    for name,path in sorted(files.items()):
        entry=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));entry.create_system=3
        entry.external_attr=(stat.S_IFREG|0o644)<<16;entry.compress_type=zipfile.ZIP_DEFLATED
        archive.writestr(entry,path.read_bytes() if path else b'')
    assert all(info.external_attr>>16==stat.S_IFREG|0o644 for info in archive.infolist())
print(str(output))
