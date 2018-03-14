# requests_gpgauthlib -- A GPGAuth python-requests Authentication lib
# Copyright (C) 2018 Didier Raboud <odyx@liip.ch>
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301  USA

import logging
import os

from http.cookiejar import MozillaCookieJar
from functools import lru_cache
from urllib.parse import unquote_plus
from uuid import uuid4

from requests import Session

from .gpgauth_api import get_verify, post_server_verify_token, post_log_in
from .gpgauth_protocol import (check_verify, get_server_keydata, get_server_fingerprint, check_server_verify_response,
                               check_server_login_response)
from .exceptions import (GPGAuthException, GPGAuthNoSecretKeyError, GPGAuthStage0Exception, GPGAuthStage1Exception,
                         GPGAuthStage2Exception)
from .utils import get_workdir

logger = logging.getLogger(__name__)


class GPGAuthSession(Session):
    """GPGAuth extension to :class:`requests.Session`.
    """
    VERIFY_URI = '/verify.json'
    LOGIN_URI = '/login.json'
    CHECKSESSION_URI = '/checkSession.json'

    # This is passbolt_api's version
    GPGAUTH_SUPPORTED_VERSION = '1.3.0'

    def __init__(self, gpg, server_url, auth_uri, **kwargs):
        """Construct a new GPGAuth client session.
        :param gpg: GPG object to handle crypto stuff
        :param server_url: URL to the server, eg. https://gpg.example.com/
        :param auth_uri: URI to the GPGAuth endpoint (…/auth/), used as a prefix for all auth URIs
        :param server_fingerprint: Full PGP fingerprint of the server
        :param server_url: Full PGP fingerprint of the server
        :param kwargs: Arguments to pass to the Session constructor.
        """
        super(GPGAuthSession, self).__init__(**kwargs)

        self.server_url = server_url.rstrip('/')
        self.auth_uri = auth_uri.rstrip('/')

        self.gpg = gpg

        self._cookie_filename = os.path.join(get_workdir(), 'gpgauth_session_cookies')
        self.cookies = MozillaCookieJar(self._cookie_filename)
        try:
            self.cookies.load()
        except FileNotFoundError:
            pass

    def build_absolute_uri(self, uri):
        """
        Return the given URI in an absolute form with the server name, eg. https://secure.example.com/uri/.
        """
        return self.server_url + uri

    def gpgauth_uri(self, uri):
        """
        Return the given URI in an absolute form with the server name and the auth URI prefix, eg.
        https://secure.example.com/auth/uri/.
        """
        return self.build_absolute_uri(self.auth_uri + uri + '?api-version=v2')

    @property
    @lru_cache()
    def _nonce0(self):
        # This format is stolen from
        # https://github.com/passbolt/passbolt_cli/blob/master/app/models/gpgAuthToken.js
        __nonce0 = 'gpgauthv%s|36|' % self.GPGAUTH_SUPPORTED_VERSION
        __nonce0 += str(uuid4())
        __nonce0 += '|gpgauthv%s' % self.GPGAUTH_SUPPORTED_VERSION
        return __nonce0

    @property
    def gpgauth_version_is_supported(self):
        return check_verify(get_verify(self))

    @property
    @lru_cache()
    def server_fingerprint(self):
        verify = get_verify(self)
        if not check_verify(verify, check_content=True):
            raise GPGAuthException("Verify endpoint wrongly formatted")

        verify_json = verify.json()
        server_claimed_fingerprint = get_server_fingerprint(verify_json)
        server_claimed_key = get_server_keydata(verify_json)

        # Import the key from the verify object
        import_result = self.gpg.import_keys(server_claimed_key)
        if server_claimed_fingerprint not in import_result.fingerprints:
            raise GPGAuthException(
                "Claimed server fingerprint %s doesn't match the claimed server key." %
                server_claimed_fingerprint
            )
        return server_claimed_fingerprint

    @property
    @lru_cache()
    def user_fingerprint(self):
        # Try to get them from GPG
        secret_keys = self.gpg.list_keys(secret=True)
        if not secret_keys:
            raise GPGAuthNoSecretKeyError(
                'No user fingerprint was loaded! You need to call import_user_private_key_from_file() first!'
            )
        # Assume the main key is the first
        return secret_keys.fingerprints[0]

    @property
    def user_auth_token(self):
        try:
            return self._user_auth_token
        except AttributeError:
            pass
        self.logged_in()
        return self._user_auth_token

    @property
    @lru_cache()
    def server_identity_is_verified(self):
        """ GPGAuth stage0 """
        # Encrypt a uuid token for the server
        server_verify_token = self.gpg.encrypt(self._nonce0,
                                               self.server_fingerprint, always_trust=True)
        if not server_verify_token.ok:
            raise GPGAuthStage0Exception(
                'Encryption of the nonce0 (%s) '
                'to the server fingerprint (%s) failed.' %
                (self._nonce0, self.server_fingerprint)
            )

        server_verify_response = post_server_verify_token(
            self,
            keyid=self.user_fingerprint,
            server_verify_token=str(server_verify_token)
        )

        if not check_server_verify_response(server_verify_response):
            raise GPGAuthStage0Exception("Verify endpoint wrongly formatted")

        if server_verify_response.headers.get('X-GPGAuth-Verify-Response') != self._nonce0:
            raise GPGAuthStage0Exception(
                'The server decrypted something different than what we sent '
                '(%s <> %s)' %
                (server_verify_response.headers.get('X-GPGAuth-Verify-Response'), self._nonce0))
        logger.info('server_identity_is_verified: OK')
        return True

    @property
    def is_logged_in(self):
        """ GPGAuth Stage1 """

        # stage0 is a prequisite
        if not self.server_identity_is_verified:
            return False

        server_login_response = post_log_in(
            self,
            keyid=self.user_fingerprint
        )

        if not check_server_login_response(server_login_response):
            raise GPGAuthStage1Exception("Login endpoint wrongly formatted")

        # Get the encrypted User Auth Token
        encrypted_user_auth_token = unquote_plus(
            server_login_response.headers.get('X-GPGAuth-User-Auth-Token')
            .replace('\\\\', '\\')
        ).replace('\\ ', ' ')
        logger.info('Decrypting the user authentication token; '
                    'password prompt expected')
        self._user_auth_token = str(
            self.gpg.decrypt(encrypted_user_auth_token, always_trust=True)
        )
        logger.info('logged_in(): OK')
        return True

    def authenticated_with_token(self):
        """ GPGAuth Stage 2 """
        """ Send back the token to the server to get auth cookie """

        r = self.post(self.gpgauth_uri(self.LOGIN_URI),
                      json={'gpg_auth': {
                          'keyid': self.user_fingerprint,
                          'user_token_result': self.user_auth_token,
                          }}
                      )
        validation_errors = []
        if r.headers['X-GPGAuth-Authenticated'] != 'true':
            validation_errors.append(
                GPGAuthStage2Exception(
                    'X-GPGAuth-Authenticated should be set to true'))
        if r.headers['X-GPGAuth-Progress'] != 'complete':
            validation_errors.append(
                GPGAuthStage2Exception(
                    'X-GPGAuth-Progress should be set to complete'))
        if 'X-GPGAuth-User-Auth-Token' in r.headers:
            validation_errors.append(
                GPGAuthStage2Exception(
                    'X-GPGAuth-User-Auth-Token should not be set'))
        if 'X-GPGAuth-Verify-Response' in r.headers:
            validation_errors.append(
                GPGAuthStage2Exception(
                    'X-GPGAuth-Verify-Response should not be set'))
        if 'X-GPGAuth-Refer' not in r.headers:
            validation_errors.append(
                GPGAuthStage2Exception(
                    'X-GPGAuth-Refer should be set'))

        if validation_errors:
            logger.debug(r.headers)
            if 'X-GPGAuth-Debug' in r.headers:
                raise GPGAuthStage2Exception('The server indicated "%s"' % r.headers['X-GPGAuth-Debug'])
            else:
                raise validation_errors.pop()
        self.cookies.save()
        logger.info('authenticated_with_token(): OK')

    def is_authenticated(self):
        r = self.get(self.gpgauth_uri(self.CHECKSESSION_URI))
        return r.status_code not in [401, 403]

    def authenticate(self):
        if self.is_authenticated():
            return
        self.authenticated_with_token()

    # GPGAuth stages in numerical form
    stage0 = server_identity_is_verified
    stage1 = is_logged_in
    stage2 = authenticated_with_token
