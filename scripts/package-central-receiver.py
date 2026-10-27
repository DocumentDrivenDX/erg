"""Produce a receiver ZIP from an explicit source allowlist; no local state."""
from pathlib import Path
import sys
import zipfile
import stat

root = Path(__file__).resolve().parents[1]
output = Path(sys.argv[1]).resolve()
if output.suffix != '.zip': raise SystemExit('explicit output .zip path required')
files = {name:root/'central'/'receiver'/name for name in ('function_app.py','host.json','requirements.txt')}
files['erg/__init__.py'] = None
files['erg/delivery_protocol.py'] = root/'erg'/'delivery_protocol.py'
for name in ('__init__.py','auth.py','blob.py','receiver.py','status.py'):
    files['erg_central/'+name] = root/'central'/'erg_central'/name
with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
    for name,path in sorted(files.items()):
        entry=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0));entry.create_system=3
        entry.external_attr=(stat.S_IFREG|0o644)<<16;entry.compress_type=zipfile.ZIP_DEFLATED
        archive.writestr(entry,path.read_bytes() if path else b'')
    assert all(info.external_attr>>16==stat.S_IFREG|0o644 for info in archive.infolist())
print(str(output))
