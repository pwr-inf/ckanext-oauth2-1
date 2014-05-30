# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import logging
import json

from base64 import b64decode, b64encode
from repoze.who.interfaces import IIdentifier, IAuthenticator, IChallenger
from requests_oauthlib import OAuth2Session
from webob import Request, Response
from zope.interface import implements

from ckan.model import User, Session

log = logging.getLogger(__name__)


def make_plugin(**kwargs):
    return OAuth2Plugin(**kwargs)


class OAuth2Plugin(object):
    '''
    A repoze.who plugin to authenticate via OAuth2
    '''

    redirect_url = '/oauth2/callback'
    came_from_field = 'came_from'

    implements(IIdentifier, IChallenger, IAuthenticator)

    def __init__(self, authorization_endpoint=None, token_endpoint=None, client_id=None, client_secret=None,
                 scope=None, rememberer_name=None, profile_api_url=None, profile_api_user_field=None,
                 profile_api_fullname_field=None, profile_api_mail_field=None):

        # Check that all the required fields are provided
        if not authorization_endpoint or not token_endpoint or not client_id or not client_secret or not profile_api_user_field:
            raise ValueError('authorization_endpoint, token_endpoint, client_id, client_secret parameters '
                             'and profile_api_user_field are required')

        self.authorization_endpoint = authorization_endpoint
        self.token_endpoint = token_endpoint
        self.client_id = client_id
        self.client_secret = client_secret
        self.scope = scope if scope is None else (scope.split(' ') if ' ' in scope else scope.split('\n'))
        self.rememberer_name = rememberer_name
        self.profile_api_url = profile_api_url
        self.profile_api_user_field = profile_api_user_field
        self.profile_api_fullname_field = profile_api_fullname_field
        self.profile_api_mail_field = profile_api_mail_field

    def _redirect_uri(self, request):
        return ''.join([request.host_url, self.redirect_url])

    def identify(self, environ):
        '''Extract OAuth2 credentials from request'''
        log.debug('Repoze OAuth identify')
        request = Request(environ)

        # Only execute this function when /oauth2/callback is called
        if request.path != self.redirect_url:
            return None

        state = request.params.get('state')
        decoded_state = json.loads(b64decode(state))
        came_from = decoded_state.get(self.came_from_field, '/')
        oauth = OAuth2Session(self.client_id, redirect_uri=self._redirect_uri(request), scope=self.scope)
        token = oauth.fetch_token(self.token_endpoint,
                                  client_secret=self.client_secret,
                                  authorization_response=request.url)
        return {'oauth2.token': token, self.came_from_field: came_from}

    def challenge(self, environ, status, app_headers=(), forget_headers=()):
        '''Challenge for OAuth2 credentials.

        Redirect the user through the OAuth2 login process.  Once complete
        it will post the obtained BrowserID assertion to the configured
        postback URL.
        '''
        log.debug('Repoze OAuth challenge')
        request = Request(environ)

        # Is the user logged?
        if 'repoze.who.identity' in environ and 'repoze.who.userid' in environ['repoze.who.identity']:
            # If the user is already logged, we mustn't try to log them again. It'll generate an infinite loop
            location = request.headers.get('Referer', '/')
            log.debug('User is trying to access to an Unauthorized function %r' % request.path)
        else:
            # Log the user in if it's not logged
            came_from_url = request.url if not request.path.startswith('/user/login') else request.headers.get('Referer', '/')
            state = b64encode(bytes(json.dumps({self.came_from_field: came_from_url})))
            oauth = OAuth2Session(self.client_id, redirect_uri=self._redirect_uri(request), scope=self.scope, state=state)
            auth_url, _ = oauth.authorization_url(self.authorization_endpoint)
            location = auth_url
            log.debug("Challenge: Redirecting challenge to page {0}".format(auth_url))

        response = Response()
        response.status = 302
        response.location = location
        return response

    def authenticate(self, environ, identity):
        '''
        Authenticate and extract identity from OAuth2 tokens
        '''
        request = Request(environ)
        log.debug('Repoze OAuth authenticate')
        if 'oauth2.token' in identity:
            oauth = OAuth2Session(self.client_id, token=identity['oauth2.token'])
            profile_response = oauth.get(self.profile_api_url)
            user_data = profile_response.json()
            username = user_data[self.profile_api_user_field]
            user = User.by_name(username)

            if user is None:
                # If the user does not exist, it's created
                user = User(name=user_data[self.profile_api_user_field])

            # Update fullname
            if self.profile_api_fullname_field:
                user.fullname = user_data[self.profile_api_fullname_field]

            # Update mail
            if self.profile_api_mail_field:
                user.email = user_data[self.profile_api_mail_field]

            # Save the user in the database
            Session.add(user)
            Session.commit()
            Session.remove()

            identity.update({'repoze.who.userid': user.name})
            self._redirect_from_callback(request, identity)
            return user.name
        return None

    def _get_rememberer(self, environ):
        plugins = environ.get('repoze.who.plugins', {})
        return plugins.get(self.rememberer_name)

    def remember(self, environ, identity):
        '''
        Remember the authenticated identity.

        This method simply delegates to another IIdentifier plugin if configured.
        '''
        log.debug('Repoze OAuth remember')
        rememberer = self._get_rememberer(environ)
        return rememberer.remember(environ, identity)

    def forget(self, environ, identity):
        '''
        Forget the authenticated identity.

        This method simply delegates to another IIdentifier plugin if configured.
        '''
        log.debug('Repoze OAuth forget')
        rememberer = self._get_rememberer(environ)
        return rememberer.forget(environ, identity)

    def _redirect_from_callback(self, request, identity):
        '''Redirect from the callback URL after a successful authentication.'''
        if request.path == self.redirect_url:
            came_from = identity.get(self.came_from_field, '/')
            response = Response()
            response.status = 302
            response.location = came_from
            request.environ["repoze.who.application"] = response