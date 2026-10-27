"""Actual crypto verification, optional central-only dependencies; no Entra calls."""
import copy
import time
import unittest
from types import SimpleNamespace
try:
    import jwt
    from cryptography.hazmat.primitives.asymmetric import rsa
except ImportError:
    jwt = None


@unittest.skipUnless(jwt, 'central-only PyJWT/cryptography unavailable')
class Auth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from central.erg_central.auth import EntraVerifier
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.wrong_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.tenant = '11111111-2222-3333-4444-555555555555'
        cls.verifier = EntraVerifier(cls.tenant, 'fixture-audience', key_provider=SimpleNamespace(
            get_signing_key_from_jwt=lambda _: SimpleNamespace(key=cls.key.public_key())))

    def setUp(self):
        self.claims = dict(iss='https://login.microsoftonline.com/'+self.tenant+'/v2.0',
            aud='fixture-audience',tid=self.tenant,oid='fixture-principal',azp='fixture-client',
            ver='2.0',idtyp='app',roles=['Erg.Collector'],iat=int(time.time()),exp=int(time.time())+300)
        self.record = dict(tenant_id=self.tenant,principal_id='fixture-principal',client_id='fixture-client',
            enrollment_id='fixture-enrollment',installation_id='fixture-installation',destination_generation='fixture-generation',
            managed_device_id='fixture-intune-device',active=True)

    def verify(self, claims=None, records=None, key=None):
        token = jwt.encode(claims or self.claims,key or self.key,algorithm='RS256')
        return self.verifier.verify(token,dict(enrollments=records if records is not None else [self.record]))

    def test_signed_application_identity_binds_active_device(self):
        self.assertEqual(self.verify(),self.record)

    def test_wrong_signature_issuer_audience_expiry_and_tenant_rejected(self):
        from central.erg_central.receiver import Forbidden
        with self.assertRaises(Forbidden): self.verify(key=self.wrong_key)
        for field,value in [('iss','https://untrusted.invalid'),('aud','other'),('tid','other'),('exp',int(time.time())-120)]:
            with self.subTest(field=field), self.assertRaises(Forbidden): self.verify(dict(self.claims,**{field:value}))

    def test_delegated_staff_token_and_role_type_confusion_rejected(self):
        from central.erg_central.receiver import Forbidden
        for change in [dict(ver='1.0'),dict(azp=None,appid='fixture-client'),dict(idtyp='user'),dict(scp='user.read'),dict(roles='prefixErg.CollectorSuffix'),dict(roles=['staff'])]:
            with self.subTest(change=change), self.assertRaises(Forbidden): self.verify(dict(self.claims,**change))

    def test_same_generation_cannot_bind_two_installations_or_principals(self):
        from central.erg_central.receiver import Forbidden
        peer=dict(self.record,principal_id='other',client_id='other',installation_id='other')
        with self.assertRaises(Forbidden):self.verify(records=[self.record,peer])

    def test_revocation_wrong_principal_and_ambiguous_registry_rejected(self):
        from central.erg_central.receiver import Forbidden
        for records in [[],[dict(self.record,active=False)],[dict(self.record,principal_id='other')],[self.record,self.record]]:
            with self.assertRaises(Forbidden): self.verify(records=records)
