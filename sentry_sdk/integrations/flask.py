from __future__ import absolute_import

import weakref
import importlib

from sentry_sdk.hub import Hub, _should_send_default_pii
from sentry_sdk.utils import capture_internal_exceptions, event_from_exception
from sentry_sdk.integrations import Integration
from sentry_sdk.integrations.wsgi import SentryWsgiMiddleware
from sentry_sdk.integrations._wsgi_common import RequestExtractor

from sentry_sdk._types import MYPY

if MYPY:

    from sentry_sdk.integrations.wsgi import _ScopedResponse
    from typing import Any
    from typing import Dict
    from typing import Optional
    from werkzeug.datastructures import ImmutableTypeConversionDict
    from werkzeug.datastructures import ImmutableMultiDict
    from werkzeug.datastructures import FileStorage
    from typing import Union
    from typing import Callable

try:
    import flask_login  # type: ignore
except ImportError:
    flask_login = None

if flask_login is None:
    try:
        import flask_jwt_extended as flask_login  # type: ignore
    except ImportError:
        pass

from flask import Request, Flask, _request_ctx_stack, _app_ctx_stack  # type: ignore
from flask.signals import (
    appcontext_pushed,
    appcontext_tearing_down,
    got_request_exception,
    request_started,
)


class FlaskIntegration(Integration):
    identifier = "flask"

    transaction_style = None
    user_module = None
    user_object = None
    user_attr_mapping = None

    def __init__(
        self,
        transaction_style="endpoint",
        user_module="flask_login",
        user_object="current_user",
        user_attr_mapping=None,
    ):
        # type: (Any, str, str, str, Optional[Dict[str, str]]) -> None
        TRANSACTION_STYLE_VALUES = ("endpoint", "url")
        if transaction_style not in TRANSACTION_STYLE_VALUES:
            raise ValueError(
                "Invalid value for transaction_style: %s (must be in %s)"
                % (transaction_style, TRANSACTION_STYLE_VALUES)
            )
        self.transaction_style = transaction_style
        try:
            self.usermodule = importlib.import_module(user_module)
        except ImportError:
            self.usermodule = None

        self.user_object = user_object
        if user_attr_mapping is None:
            self.user_attr_mapping = {"username": "username", "email": "email"}
        else:
            self.user_attr_mapping = user_attr_mapping

    @staticmethod
    def setup_once():
        # type: () -> None
        appcontext_pushed.connect(_push_appctx)
        appcontext_tearing_down.connect(_pop_appctx)
        request_started.connect(_request_started)
        got_request_exception.connect(_capture_exception)

        old_app = Flask.__call__

        def sentry_patched_wsgi_app(self, environ, start_response):
            # type: (Any, Dict[str, str], Callable) -> _ScopedResponse
            if Hub.current.get_integration(FlaskIntegration) is None:
                return old_app(self, environ, start_response)

            return SentryWsgiMiddleware(lambda *a, **kw: old_app(self, *a, **kw))(
                environ, start_response
            )

        Flask.__call__ = sentry_patched_wsgi_app  # type: ignore


def _push_appctx(*args, **kwargs):
    # type: (*Flask, **Any) -> None
    hub = Hub.current
    if hub.get_integration(FlaskIntegration) is not None:
        # always want to push scope regardless of whether WSGI app might already
        # have (not the case for CLI for example)
        scope_manager = hub.push_scope()
        scope_manager.__enter__()
        _app_ctx_stack.top.sentry_sdk_scope_manager = scope_manager
        with hub.configure_scope() as scope:
            scope._name = "flask"


def _pop_appctx(*args, **kwargs):
    # type: (*Flask, **Any) -> None
    scope_manager = getattr(_app_ctx_stack.top, "sentry_sdk_scope_manager", None)
    if scope_manager is not None:
        scope_manager.__exit__(None, None, None)


def _request_started(sender, **kwargs):
    # type: (Flask, **Any) -> None
    hub = Hub.current
    integration = hub.get_integration(FlaskIntegration)
    if integration is None:
        return

    app = _app_ctx_stack.top.app
    with hub.configure_scope() as scope:
        request = _request_ctx_stack.top.request

        # Rely on WSGI middleware to start a trace
        try:
            if integration.transaction_style == "endpoint":
                scope.transaction = request.url_rule.endpoint  # type: ignore
            elif integration.transaction_style == "url":
                scope.transaction = request.url_rule.rule  # type: ignore
        except Exception:
            pass

        weak_request = weakref.ref(request)
        scope.add_event_processor(
            _make_request_event_processor(  # type: ignore
                app, weak_request, integration
            )
        )


class FlaskRequestExtractor(RequestExtractor):
    def env(self):
        # type: () -> Dict[str, str]
        return self.request.environ

    def cookies(self):
        # type: () -> ImmutableTypeConversionDict
        return self.request.cookies

    def raw_data(self):
        # type: () -> bytes
        return self.request.get_data()

    def form(self):
        # type: () -> ImmutableMultiDict
        return self.request.form

    def files(self):
        # type: () -> ImmutableMultiDict
        return self.request.files

    def is_json(self):
        return self.request.is_json

    def json(self):
        return self.request.get_json()

    def size_of_file(self, file):
        # type: (FileStorage) -> int
        return file.content_length


def _make_request_event_processor(app, weak_request, integration):
    # type: (Flask, Callable[[], Request], FlaskIntegration) -> Callable
    def inner(event, hint):
        # type: (Dict[str, Any], Dict[str, Any]) -> Dict[str, Any]
        request = weak_request()

        # if the request is gone we are fine not logging the data from
        # it.  This might happen if the processor is pushed away to
        # another thread.
        if request is None:
            return event

        with capture_internal_exceptions():
            FlaskRequestExtractor(request).extract_into_event(event)

        if _should_send_default_pii():
            with capture_internal_exceptions():
                _add_user_to_event(event)

        return event

    return inner


def _capture_exception(sender, exception, **kwargs):
    # type: (Flask, Union[ValueError, BaseException], **Any) -> None
    hub = Hub.current
    if hub.get_integration(FlaskIntegration) is None:
        return

    # If an integration is there, a client has to be there.
    client = hub.client  # type: Any

    event, hint = event_from_exception(
        exception,
        client_options=client.options,
        mechanism={"type": "flask", "handled": False},
    )

    hub.capture_event(event, hint=hint)


def _add_user_to_event(event):
    hub = Hub.current
    integration = hub.get_integration(FlaskIntegration)

    if integration.usermodule is None:
        return

    user = getattr(integration.usermodule, integration.user_object)

    if user is None:
        return

    with capture_internal_exceptions():
        # Access this object as late as possible as accessing the user
        # is relatively costly

        user_info = event.setdefault("user", {})

        try:
            user_info["id"] = user.get_id()
            # TODO: more configurable user attrs here
        except AttributeError:
            # might happen if:
            # - flask_login could not be imported
            # - flask_login is not configured
            # - no user is logged in
            pass

        for attr in integration.user_attr_mapping:
            # The following attribute accesses are ineffective for the general
            # Flask-Login case, because the User interface of Flask-Login does not
            # care about anything but the ID. However, Flask-User (based on
            # Flask-Login) documents a few optional extra attributes.
            #
            # https://github.com/lingthio/Flask-User/blob/a379fa0a281789618c484b459cb41236779b95b1/docs/source/data_models.rst#fixed-data-model-property-names

            try:
                user_info[attr] = getattr(user, integration.user_attr_mapping.get(attr))
            except Exception:
                pass
