#!/usr/bin/env python3
"""Publish/pull a private UC rate catalog with an existing CLI login."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parent.parent))
from erg import databricks,rate_catalog


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('action',choices=('pull','publish'))
    p.add_argument('--profile',required=True);p.add_argument('--warehouse',required=True)
    p.add_argument('--namespace',required=True);p.add_argument('--file',help='Complete versioned JSON catalog to publish')
    p.add_argument('--automatic',action='store_true',help='Enable a 15-minute pull in the existing cloud reporting worker')
    a=p.parse_args();client=databricks.Client(a.profile,a.warehouse)
    if a.action=='publish':
        if not a.file:p.error('publish requires --file; pulling never overwrites the central catalog')
        print(json.dumps(dict(revision=rate_catalog.publish(client,a.namespace,json.loads(Path(a.file).read_text())))))
    else:
        result=rate_catalog.pull(client,a.namespace)
        if a.automatic:
            from erg import cloud_history
            _,_,cfg=cloud_history.connection()
            if cfg['cloud']['profile']!=a.profile or cfg['cloud']['namespace']!=a.namespace:p.error('--automatic requires the configured reporting profile and namespace')
            rate_catalog.enable(a.profile,a.namespace,cfg['cloud']['host'])
        print(json.dumps(result))

if __name__=='__main__':main()
