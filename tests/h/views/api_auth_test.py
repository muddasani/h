# -*- coding: utf-8 -*-

from __future__ import unicode_literals

import datetime
import json

import mock
import pytest

from oauthlib.oauth2 import InvalidRequestFatalError
from pyramid import httpexceptions

from h._compat import url_quote
from h.exceptions import OAuthTokenError
from h.models.auth_client import ResponseType
from h.services.auth_token import auth_token_service_factory
from h.services.oauth_provider import OAuthProviderService
from h.services.oauth_validator import DEFAULT_SCOPES
from h.services.user import user_service_factory
from h.util.datetime import utc_iso8601
from h.views import api_auth as views


@pytest.mark.usefixtures('routes', 'oauth_provider', 'user_svc')
class TestOAuthAuthorizeController(object):
    @pytest.mark.usefixtures('authenticated_user')
    def test_get_validates_request(self, controller, pyramid_request):
        controller.get()

        controller.oauth.validate_authorization_request.assert_called_once_with(
            pyramid_request.url)

    def test_get_raises_for_invalid_request(self, controller):
        controller.oauth.validate_authorization_request.side_effect = InvalidRequestFatalError('boom!')

        with pytest.raises(InvalidRequestFatalError) as exc:
            controller.get()

        assert exc.value.description == 'boom!'

    def test_get_redirects_to_login_when_not_authenticated(self, controller, pyramid_request):
        with pytest.raises(httpexceptions.HTTPFound) as exc:
            controller.get()

        assert exc.value.location == 'http://example.com/login?next={}'.format(
                                       url_quote(pyramid_request.url, safe=''))

    def test_get_returns_expected_context(self, controller, auth_client, authenticated_user):
        assert controller.get() == {
            'client_id': auth_client.id,
            'client_name': auth_client.name,
            'response_type': auth_client.response_type.value,
            'state': 'foobar',
            'username': authenticated_user.username,
        }

    def test_get_creates_authorization_response_for_trusted_clients(self, controller, auth_client, authenticated_user, pyramid_request):
        auth_client.trusted = True

        controller.get()

        controller.oauth.create_authorization_response.assert_called_once_with(
            pyramid_request.url,
            credentials={'user': authenticated_user},
            scopes=DEFAULT_SCOPES)

    def test_returns_redirect_immediately_for_trusted_clients(self, controller, auth_client, authenticated_user, pyramid_request):
        auth_client.trusted = True

        response = controller.get()
        expected = '{}?code=abcdef123456'.format(auth_client.redirect_uri)

        assert response.location == expected

    def test_post_creates_authorization_response(self, controller, pyramid_request, authenticated_user):
        pyramid_request.url = 'http://example.com/auth?client_id=the-client-id' + \
                                                     '&response_type=code' + \
                                                     '&state=foobar' + \
                                                     '&scope=exploit'

        controller.post()

        controller.oauth.create_authorization_response.assert_called_once_with(
            pyramid_request.url,
            credentials={'user': authenticated_user},
            scopes=DEFAULT_SCOPES)

    def test_post_redirects_to_client(self, controller, auth_client):
        response = controller.post()
        expected = '{}?code=abcdef123456'.format(auth_client.redirect_uri)

        assert response.location == expected

    @pytest.mark.usefixtures('authenticated_user')
    def test_post_raises_for_invalid_request(self, controller):
        controller.oauth.create_authorization_response.side_effect = InvalidRequestFatalError('boom!')

        with pytest.raises(InvalidRequestFatalError) as exc:
            controller.post()

        assert exc.value.description == 'boom!'

    @pytest.fixture
    def controller(self, pyramid_request):
        return views.OAuthAuthorizeController(None, pyramid_request)

    @pytest.fixture
    def oauth_provider(self, pyramid_config, auth_client):
        svc = mock.create_autospec(OAuthProviderService, instance=True)

        scopes = ['annotation:read', 'annotation:write']
        credentials = {'client_id': auth_client.id, 'state': 'foobar'}
        svc.validate_authorization_request.return_value = (scopes, credentials)

        headers = {'Location': '{}?code=abcdef123456'.format(auth_client.redirect_uri)}
        body = None
        status = 302
        svc.create_authorization_response.return_value = (headers, body, status)

        pyramid_config.register_service(svc, name='oauth_provider')

        return svc

    @pytest.fixture
    def auth_client(self, factories):
        return factories.AuthClient(name='Test Client',
                                    redirect_uri='http://client.com/auth/callback',
                                    response_type=ResponseType.code)

    @pytest.fixture
    def user_svc(self, pyramid_config, pyramid_request):
        svc = mock.Mock(spec_set=user_service_factory(None, pyramid_request))
        pyramid_config.register_service(svc, name='user')
        return svc

    @pytest.fixture
    def pyramid_request(self, pyramid_request):
        pyramid_request.url = 'http://example.com/auth?client_id=the-client-id&response_type=code&state=foobar'
        return pyramid_request

    @pytest.fixture
    def authenticated_user(self, factories, pyramid_config, user_svc):
        user = factories.User.build()
        pyramid_config.testing_securitypolicy(user.userid)

        def fake_fetch(userid):
            if userid == user.userid:
                return user
        user_svc.fetch.side_effect = fake_fetch

        return user

    @pytest.fixture
    def routes(self, pyramid_config):
        pyramid_config.add_route('login', '/login')


@pytest.mark.usefixtures('oauth_provider')
class TestOAuthAccessTokenController(object):
    def test_it_creates_token_response(self, pyramid_request, controller, oauth_provider):
        controller.post()
        oauth_provider.create_token_response.assert_called_once_with(
                pyramid_request.url, pyramid_request.method, pyramid_request.POST, pyramid_request.headers)

    def test_it_returns_correct_response_on_success(self, controller, oauth_provider):
        body = json.dumps({'access_token': 'the-access-token'})
        oauth_provider.create_token_response.return_value = ({}, body, 200)

        assert controller.post() == {'access_token': 'the-access-token'}

    def test_it_raises_when_error(self, controller, oauth_provider):
        body = json.dumps({'error': 'invalid_request'})
        oauth_provider.create_token_response.return_value = ({}, body, 400)

        with pytest.raises(httpexceptions.HTTPBadRequest) as exc:
            controller.post()

        assert exc.value.body == body

    @pytest.fixture
    def controller(self, pyramid_request):
        pyramid_request.method = 'POST'
        pyramid_request.POST['grant_type'] = 'authorization_code'
        pyramid_request.POST['code'] = 'the-authz-code'
        pyramid_request.headers = {'X-Test-ID': '1234'}
        return views.OAuthAccessTokenController(pyramid_request)

    @pytest.fixture
    def oauth_provider(self, pyramid_config):
        svc = mock.Mock(spec_set=['create_token_response'])
        svc.create_token_response.return_value = ({}, '{}', 200)
        pyramid_config.register_service(svc, name='oauth_provider')
        return svc


class TestDebugToken(object):
    def test_it_raises_error_when_token_is_missing(self, pyramid_request):
        pyramid_request.auth_token = None

        with pytest.raises(OAuthTokenError) as exc:
            views.debug_token(pyramid_request)

        assert exc.value.type == 'missing_token'
        assert 'Bearer token is missing' in exc.value.message

    def test_it_raises_error_when_token_is_empty(self, pyramid_request):
        pyramid_request.auth_token = ''

        with pytest.raises(OAuthTokenError) as exc:
            views.debug_token(pyramid_request)

        assert exc.value.type == 'missing_token'
        assert 'Bearer token is missing' in exc.value.message

    def test_it_validates_token(self, pyramid_request, token_service):
        pyramid_request.auth_token = 'the-access-token'

        views.debug_token(pyramid_request)

        token_service.validate.assert_called_once_with('the-access-token')

    def test_it_raises_error_when_token_is_invalid(self, pyramid_request, token_service):
        pyramid_request.auth_token = 'the-token'
        token_service.validate.return_value = None

        with pytest.raises(OAuthTokenError) as exc:
            views.debug_token(pyramid_request)

        assert exc.value.type == 'missing_token'
        assert 'Bearer token does not exist or is expired' in exc.value.message

    def test_returns_debug_data_for_oauth_token(self, pyramid_request, token_service, oauth_token):
        pyramid_request.auth_token = oauth_token.value
        token_service.fetch.return_value = oauth_token

        result = views.debug_token(pyramid_request)

        assert result == {'userid': oauth_token.userid,
                          'client': {'id': oauth_token.authclient.id,
                                     'name': oauth_token.authclient.name},
                          'issued_at': utc_iso8601(oauth_token.created),
                          'expires_at': utc_iso8601(oauth_token.expires),
                          'expired': oauth_token.expired}

    def test_returns_debug_data_for_developer_token(self, pyramid_request, token_service, developer_token):
        pyramid_request.auth_token = developer_token.value
        token_service.fetch.return_value = developer_token

        result = views.debug_token(pyramid_request)

        assert result == {'userid': developer_token.userid,
                          'issued_at': utc_iso8601(developer_token.created),
                          'expires_at': None,
                          'expired': False}

    @pytest.fixture
    def token_service(self, pyramid_config, pyramid_request):
        pyramid_config.registry.settings['h.client_secret'] = 'notsosecretafterall'
        svc = mock.Mock(spec_set=auth_token_service_factory(None, pyramid_request))
        pyramid_config.register_service(svc, name='auth_token')
        return svc

    @pytest.fixture
    def oauth_token(self, factories):
        authclient = factories.AuthClient(name='Example Client')
        expires = datetime.datetime.utcnow() + datetime.timedelta(minutes=10)
        return factories.DeveloperToken(authclient=authclient, expires=expires)

    @pytest.fixture
    def developer_token(self, factories):
        return factories.DeveloperToken()


class TestAPITokenError(object):
    def test_it_sets_the_response_status_code(self, pyramid_request):
        context = OAuthTokenError('the error message', 'error_type', status_code=403)
        views.api_token_error(context, pyramid_request)
        assert pyramid_request.response.status_code == 403

    def test_it_returns_the_error(self, pyramid_request):
        context = OAuthTokenError('', 'error_type')
        result = views.api_token_error(context, pyramid_request)
        assert result['error'] == 'error_type'

    def test_it_returns_error_description(self, pyramid_request):
        context = OAuthTokenError('error description', 'error_type')
        result = views.api_token_error(context, pyramid_request)
        assert result['error_description'] == 'error description'

    def test_it_skips_description_when_missing(self, pyramid_request):
        context = OAuthTokenError(None, 'invalid_request')
        result = views.api_token_error(context, pyramid_request)
        assert 'error_description' not in result

    def test_it_skips_description_when_empty(self, pyramid_request):
        context = OAuthTokenError('', 'invalid_request')
        result = views.api_token_error(context, pyramid_request)
        assert 'error_description' not in result
