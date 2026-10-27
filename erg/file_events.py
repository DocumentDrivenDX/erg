"""Bounded filesystem hints. Notifications accelerate capture; checkpoints prove it."""
import ctypes as C
import os
from pathlib import Path
import sys
import threading
import time

# Public FSEvents flags, also defined in the macOS SDK FSEvents.h.
RESCAN_FLAGS=0x1|0x2|0x4|0x20  # must scan, user/kernel drop, watched root changed


class Mailbox:
    def __init__(self,limit=10000):
        self.limit=limit;self.lock=threading.Lock();self.consumers={};self.rescans={};self.dropped=0
    def subscribe(self,name):
        with self.lock:self.consumers.setdefault(name,{});self.rescans.setdefault(name,False)
    def put(self,path,flags=0):
        with self.lock:
            for name,paths in self.consumers.items():
                if flags&RESCAN_FLAGS:self.rescans[name]=True
                if len(paths)>=self.limit and path not in paths:
                    self.rescans[name]=True;self.dropped+=1
                else:paths.setdefault(path,None)
    def drain(self,name,limit=500):
        with self.lock:
            paths=self.consumers[name];selected=list(paths)[:limit]
            for path in selected:del paths[path]
            rescan=self.rescans[name];self.rescans[name]=False
            return selected,rescan
    def retry(self,name,paths):
        with self.lock:
            for path in paths:
                if len(self.consumers[name])<self.limit:self.consumers[name].setdefault(path,None)
                else:self.rescans[name]=True;self.dropped+=1
    def request_rescan(self):
        with self.lock:
            for name in self.rescans:self.rescans[name]=True
    def status(self):
        with self.lock:return dict(queued={k:len(v) for k,v in self.consumers.items()},overflow_count=self.dropped,rescan_pending=any(self.rescans.values()))


class MacWatch:
    """FSEvents over one serial dispatch queue; no per-file descriptors or polling."""
    def __init__(self,roots,mailbox):
        if sys.platform!='darwin':raise RuntimeError('native_file_events_unavailable')
        self.stream=None;self.queue=None;self.array=None;self.strings=[];self.error=None
        self.cf=C.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
        self.fs=C.CDLL('/System/Library/Frameworks/CoreServices.framework/CoreServices')
        self.lib=C.CDLL('/usr/lib/libSystem.B.dylib')
        self.cf.CFStringCreateWithCString.argtypes=[C.c_void_p,C.c_char_p,C.c_uint32];self.cf.CFStringCreateWithCString.restype=C.c_void_p
        self.cf.CFArrayCreate.argtypes=[C.c_void_p,C.POINTER(C.c_void_p),C.c_long,C.c_void_p];self.cf.CFArrayCreate.restype=C.c_void_p
        self.cf.CFRelease.argtypes=[C.c_void_p]
        self.lib.dispatch_queue_create.argtypes=[C.c_char_p,C.c_void_p];self.lib.dispatch_queue_create.restype=C.c_void_p
        self.lib.dispatch_release.argtypes=[C.c_void_p]
        self.fs.FSEventStreamCreate.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p,C.c_void_p,C.c_uint64,C.c_double,C.c_uint32];self.fs.FSEventStreamCreate.restype=C.c_void_p
        self.fs.FSEventStreamSetDispatchQueue.argtypes=[C.c_void_p,C.c_void_p]
        self.fs.FSEventStreamStart.argtypes=[C.c_void_p];self.fs.FSEventStreamStart.restype=C.c_bool
        for name in ('FSEventStreamStop','FSEventStreamInvalidate','FSEventStreamRelease'):getattr(self.fs,name).argtypes=[C.c_void_p]
        callback_type=C.CFUNCTYPE(None,C.c_void_p,C.c_void_p,C.c_size_t,C.c_void_p,C.POINTER(C.c_uint32),C.POINTER(C.c_uint64))
        def callback(stream,info,count,raw_paths,flags,ids):
            try:
                values=C.cast(raw_paths,C.POINTER(C.c_char_p))
                for i in range(count):mailbox.put(os.fsdecode(values[i]),flags[i])
            except Exception:
                self.error='event_callback_failed';mailbox.request_rescan()
        self.callback=callback_type(callback)
        try:
            for root in roots:
                value=self.cf.CFStringCreateWithCString(None,os.fsencode(root),0x08000100)
                if not value:raise RuntimeError('event_root_allocation_failed')
                self.strings.append(value)
            self.array=self.cf.CFArrayCreate(None,(C.c_void_p*len(self.strings))(*self.strings),len(self.strings),None)
            if not self.array:raise RuntimeError('event_array_allocation_failed')
            self.queue=self.lib.dispatch_queue_create(b'dev.ddx.erg.files',None)
            if not self.queue:raise RuntimeError('event_queue_allocation_failed')
            self.stream=self.fs.FSEventStreamCreate(None,C.cast(self.callback,C.c_void_p),None,self.array,2**64-1,.2,0x2|0x4|0x10)
            if not self.stream:raise RuntimeError('event_stream_creation_failed')
            self.fs.FSEventStreamSetDispatchQueue(self.stream,self.queue)
            if not self.fs.FSEventStreamStart(self.stream):raise RuntimeError('event_stream_start_failed')
        except Exception:self.close();raise
    def close(self):
        if self.stream:
            self.fs.FSEventStreamStop(self.stream);self.fs.FSEventStreamInvalidate(self.stream)
            # Drain scheduled Python callbacks before releasing their references.
            barrier_type=C.CFUNCTYPE(None,C.c_void_p);barrier=barrier_type(lambda _:None)
            self.lib.dispatch_sync_f.argtypes=[C.c_void_p,C.c_void_p,C.c_void_p]
            self.lib.dispatch_sync_f(self.queue,None,C.cast(barrier,C.c_void_p))
            self.fs.FSEventStreamRelease(self.stream);self.stream=None
        if self.queue:self.lib.dispatch_release(self.queue);self.queue=None
        if self.array:self.cf.CFRelease(self.array);self.array=None
        for value in self.strings:self.cf.CFRelease(value)
        self.strings=[]


def collapse(roots):
    selected=[]
    for root in sorted(set(os.path.realpath(p) for p in roots),key=lambda p:(len(p),p)):
        if not os.path.isdir(root):continue
        if not any(root==old or root.startswith(old+os.sep) for old in selected):selected.append(root)
    return selected


class Sources:
    def __init__(self):
        self.lock=threading.Lock();self.roots=[];self.watch_roots=[];self.refresh()
    def refresh(self):
        from . import collect
        roots=[]
        for tool,paths in (('pi',collect.pi_roots()+[str(Path(collect.STATE_DIR,'pi-content-outbox'))]),('claude',collect.claude_roots()),('codex',collect.native_roots('codex')),('copilot',[str(Path(collect.HOME,'.copilot/session-state'))])):
            roots.extend((tool,os.path.realpath(p)) for p in paths)
        # Watch provider homes too, so new session directories trigger notifications.
        broad=[str(Path(collect.HOME,p)) for p in ('.claude','.codex','.pi/agent','.copilot')]+[collect.omni_home()]
        conf=collect.discovery_config()
        broad+=conf.get('codex_homes',[])+conf.get('claude_homes',[])+collect.opencode_roots()
        broad+=[p if os.path.isdir(p) else str(Path(p).parent) for _,p in roots]
        discovery=os.environ.get('ERG_DISCOVERY_FILE') or str(Path(collect.HOME,'.config/erg/discovery.json'))
        broad.extend((str(Path(discovery).parent),str(Path(os.path.realpath(discovery)).parent)))
        watched=collapse(broad)
        with self.lock:self.roots=sorted(set(roots),key=lambda r:len(r[1]),reverse=True);self.watch_roots=watched
        return watched
    def classify(self,path):
        if not path.endswith(('.jsonl','.jsonl.zst')):return None
        path=os.path.realpath(path)
        with self.lock:
            for tool,root in self.roots:
                if path==root or path.startswith(root+os.sep):return tool,path
        return None


ACTIVE_SOURCE_LIMIT=128


def active_sources(db,sources,limit=ACTIVE_SOURCE_LIMIT):
    """Bounded safety net for writers held open between FSEvents notifications.

    Only checkpointed transcripts still owned by current discovery are eligible.
    scan_file's metadata comparison avoids rereading an unchanged transcript.
    """
    limit=max(0,min(int(limit),500));result=[]
    for tool,path in db.execute("SELECT tool,path FROM history_sources WHERE tool<>'opencode' ORDER BY mtime DESC LIMIT ?",(limit,)):
        item=sources.classify(path)
        if item and item[0]==tool and os.path.isfile(item[1]):result.append(item)
    return result


class Feed:
    def __init__(self,names=('history','context','fast')):
        from . import collect
        discovery=os.environ.get('ERG_DISCOVERY_FILE') or str(Path(collect.HOME,'.config/erg/discovery.json'))
        self.discovery_alias=os.path.join(os.path.realpath(os.path.dirname(discovery)),os.path.basename(discovery))
        self.discovery_path=os.path.realpath(discovery)
        self.lifecycle=threading.RLock();self.refreshing=threading.Lock();self.refresh_retry=False;self.closed=False;self.ignored_events=0
        self.mailbox=Mailbox();self.sources=Sources();self.native=None;self.error=None
        for name in names:self.mailbox.subscribe(name)
        self.refresh()
    def refresh(self):
        # Filesystem discovery can block on provider trees. It must not hold
        # the lifecycle lock used by capture heartbeats and status readers.
        # Coalesce concurrent refreshes rather than queueing repeated scans.
        if not self.refreshing.acquire(blocking=False):
            self.refresh_retry=True;return
        self.refresh_retry=False
        try:
            with self.lifecycle:
                if self.closed:return
            discovery_path=os.path.realpath(self.discovery_alias)
            roots=self.sources.refresh()
            with self.lifecycle:
                if self.closed:return
                self.discovery_path=discovery_path
                if self.native is not None and roots==getattr(self,'roots',None):return
                previous=self.native
                try:self.native=MacWatch(roots,self);self.error=None
                except (OSError,RuntimeError,AttributeError) as error:self.native=None;self.error=type(error).__name__
                self.roots=roots
                if previous:previous.close()
                self.mailbox.request_rescan()
        finally:
            self.refreshing.release()
            if self.refresh_retry and not self.closed:self.mailbox.request_rescan()
    def put(self,path,flags=0):
        # Provider homes also contain plugins, caches and build trees. Do not
        # let unrelated traffic crowd out enrolled transcripts. Loss flags always
        # request reconciliation, even when the accompanying path is irrelevant.
        if flags&RESCAN_FLAGS:self.mailbox.request_rescan()
        if path in (self.discovery_path,self.discovery_alias):
            self.mailbox.request_rescan();return
        name=os.path.basename(path)
        if path.endswith(('.jsonl','.jsonl.zst')):
            if self.sources.classify(path):self.mailbox.put(path,flags);return
            # New bridge homes can precede their state pointer; reconciliation
            # discovers them, without treating arbitrary plugin logs as sessions.
            if any(part in path for part in ('/codex-native/','/claude-native/','/pi-native/')):
                self.mailbox.request_rescan()
        elif name in ('state.json','opencode.db','opencode.db-wal','chat.db','chat.db-wal'):
            self.mailbox.put(path,flags);return
        self.ignored_events+=1
    def request_rescan(self):self.mailbox.request_rescan()
    def drain(self,name):return self.mailbox.drain(name)
    def status(self):
        with self.lifecycle:return dict(self.mailbox.status(),mode='native_events' if self.native else 'polling_fallback',error=self.error or (self.native.error if self.native else None),watched_roots=len(self.roots),ignored_unrelated_events=self.ignored_events)
    def close(self):
        with self.lifecycle:
            self.closed=True
            if self.native:self.native.close();self.native=None
