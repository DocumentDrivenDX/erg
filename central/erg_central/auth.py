"""Central-only Entra v2 application-token verification and enrollment binding."""
import re
import jwt
from .receiver import Forbidden

GUID = re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')


class EntraVerifier:
    def __init__(self, tenant, audience, required_role='Erg.Collector', key_provider=None):
        if not GUID.fullmatch(tenant) or not audience or not required_role:
            raise ValueError('invalid authentication configuration')
        self.tenant = tenant; self.audience = audience; self.role = required_role
        self.issuer = 'https://login.microsoftonline.com/' + tenant + '/v2.0'
        self.keys = key_provider or jwt.PyJWKClient(
            'https://login.microsoftonline.com/' + tenant + '/discovery/v2.0/keys',
            cache_jwk_set=True, lifespan=300, timeout=5)

    def verify(self, token, registry):
        if not isinstance(token, str) or len(token) > 16384:
            raise Forbidden('invalid credential')
        try:
            key = self.keys.get_signing_key_from_jwt(token).key
            claims = jwt.decode(token, key, algorithms=['RS256'], audience=self.audience,
                issuer=self.issuer, leeway=30,
                options={'require':['exp','iat','iss','aud','tid','oid']})
        except (jwt.PyJWTError, ValueError, TypeError):
            raise Forbidden('invalid credential') from None
        # idtyp=app optional claim must be configured at enrollment qualification.
        # Delegated staff sign-in is never a collector credential.
        roles = claims.get('roles')
        if (not isinstance(roles, list) or not all(isinstance(v, str) for v in roles) or
            claims.get('ver') != '2.0' or claims.get('tid') != self.tenant or claims.get('idtyp') != 'app' or
            self.role not in roles or 'scp' in claims):
            raise Forbidden('collector application role required')
        client = claims.get('azp')
        if not isinstance(registry, dict) or not isinstance(registry.get('enrollments'), list):
            raise Forbidden('enrollment registry unavailable')
        matches = [row for row in registry['enrollments'] if isinstance(row, dict) and row.get('active') is True
            and row.get('tenant_id') == self.tenant and row.get('principal_id') == claims['oid']
            and row.get('client_id') == client and row.get('managed_device_id')]
        if len(matches) != 1 or any(not matches[0].get(k) for k in ('enrollment_id','installation_id','destination_generation')):
            raise Forbidden('device enrollment unavailable')
        selected=matches[0]
        peers=[r for r in registry['enrollments'] if isinstance(r,dict) and r.get('active') is True
               and r.get('enrollment_id')==selected['enrollment_id'] and r.get('destination_generation')==selected['destination_generation']]
        if len(peers)!=1:raise Forbidden('ambiguous generation binding')
        if 'synthetic_only' in matches[0] and type(matches[0]['synthetic_only']) is not bool:
            raise Forbidden('invalid enrollment policy')
        return matches[0]
