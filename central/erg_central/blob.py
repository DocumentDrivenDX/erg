"""Azure SDK adapter; server only. Workload identity, no storage account keys."""
from azure.core.exceptions import ResourceExistsError, ResourceNotFoundError, ResourceModifiedError
from azure.core import MatchConditions
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient
from .receiver import Conflict


class BlobStore:
    def __init__(self, account_url, container, control_container, prefix='v1'):
        if not account_url.startswith('https://') or not container or not prefix or '..' in prefix.split('/'):
            raise ValueError('invalid spool configuration')
        if not control_container or control_container == container:
            raise ValueError('distinct control container required')
        service = BlobServiceClient(account_url, credential=DefaultAzureCredential())
        self.container = service.get_container_client(container)
        self.control = service.get_container_client(control_container)
        self.prefix = prefix.strip('/')

    def _name(self, key):
        if not key.startswith('v1/') or '..' in key.split('/'):
            raise ValueError('invalid spool key')
        return self.prefix + '/' + key[3:]

    def get(self, key):
        try:
            blob = self.container.get_blob_client(self._name(key))
            if blob.get_blob_properties().size > 2 * 1024 * 1024:
                raise ValueError('oversized spool object')
            return blob.download_blob(max_concurrency=1).readall()
        except ResourceNotFoundError: return None

    def _control_blob(self, key):
        if not key or key.startswith('/') or '..' in key.split('/'):
            raise ValueError('invalid control object')
        return self.control.get_blob_client(key)

    def read_control(self, key):
        try:
            blob=self._control_blob(key)
            if blob.get_blob_properties().size>2*1024*1024: raise ValueError('oversized control object')
            return blob.download_blob(max_concurrency=1).readall()
        except ResourceNotFoundError: return None

    def put_control_once(self,key,raw):
        try:self._control_blob(key).upload_blob(raw,overwrite=False)
        except ResourceExistsError:
            if self.read_control(key)!=raw:raise Conflict('immutable control conflict')

    def put_once(self, key, raw):
        try: self.container.get_blob_client(self._name(key)).upload_blob(raw, overwrite=False)
        except ResourceExistsError:
            if self.get(key) != raw: raise Conflict('immutable key conflict')

    def versioned(self, key):
        try:
            stream = self.container.get_blob_client(self._name(key)).download_blob(max_concurrency=1)
            if stream.size > 128 * 1024: raise Conflict('oversized claim index')
            return stream.readall(), stream.properties.etag
        except ResourceNotFoundError: return None,None

    def compare_swap(self, key, expected, raw):
        if len(raw)>128 * 1024: raise Conflict('oversized claim index')
        blob = self.container.get_blob_client(self._name(key))
        try:
            if expected is None: blob.upload_blob(raw,overwrite=False)
            else: blob.upload_blob(raw,overwrite=True,etag=expected,match_condition=MatchConditions.IfNotModified)
            return True
        except (ResourceExistsError,ResourceModifiedError): return False

    def keys(self, prefix):
        for blob in self.container.list_blobs(name_starts_with=self._name(prefix)):
            yield 'v1/' + blob.name[len(self.prefix) + 1:]
