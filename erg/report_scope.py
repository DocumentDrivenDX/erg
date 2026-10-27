"""Validated reporting filters shared by usage, project allocation and work."""
import time

FIELDS=frozenset(('start','end','machine','tool','session','project_key','folder_key','tools','kinds','requester'))


class Selection:
    def __init__(self,values=None,identities=()):
        values={} if values is None else dict(values)
        if set(values)-FIELDS:raise ValueError('Unsupported report filter')
        self.values={};self.identities=frozenset(identities)
        for name in FIELDS:
            value=values.get(name)
            if name in ('start','end'):
                if value in (None,''):value=0 if name=='start' else int(time.time())+1
                if isinstance(value,bool):raise ValueError('Invalid report interval')
                if isinstance(value,str):
                    try:value=int(value)
                    except ValueError:raise ValueError('Invalid report interval')
                if not isinstance(value,int) or not 0<=value<=2**63-1:raise ValueError('Invalid report interval')
            else:
                value='' if value is None else value
                if not isinstance(value,str) or len(value)>4096:raise ValueError('Invalid report filter')
                if name in ('tools','kinds'):
                    items=value.split(',') if value else []
                    if len(items)>100 or any(not item or len(item)>256 for item in items):raise ValueError('Invalid report choices')
                    value=','.join(sorted(set(items)))
            self.values[name]=value
        if self.values['start']>=self.values['end']:raise ValueError('Invalid report interval')
        self.tools=frozenset() if self.values['tools']=='@none' else frozenset(self.values['tools'].split(',')) if self.values['tools'] else None
        self.kinds=frozenset() if self.values['kinds']=='@none' else frozenset(self.values['kinds'].split(',')) if self.values['kinds'] else None

    def accepts(self,row):
        v=self.values
        if not v['start']<=row['ts']<v['end']:return False
        for name in ('machine','tool','session'):
            if v[name] and row.get(name)!=v[name]:return False
        if self.tools is not None and row.get('tool') not in self.tools:return False
        if self.kinds is not None and row.get('kind') is not None and row['kind'] not in self.kinds:return False
        requester=v['requester']
        if requester and requester!='all' and 'requester' in row:
            observed=row.get('requester')
            if requester=='me':
                if observed and observed not in self.identities:return False
            elif observed!=requester:return False
        return True

    def allocation(self,hierarchy,row):
        if not self.accepts(row):return 0,None
        return hierarchy.weight(row,self.values['project_key'],self.values['folder_key'])
