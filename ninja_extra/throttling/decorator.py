import inspect
from functools import wraps
from typing import Any, Callable, List, Optional, Type, Union, cast

from asgiref.sync import sync_to_async
from django.http import HttpRequest, HttpResponse
from ninja.signature import is_async

from ninja_extra import exceptions
from ninja_extra.conf import settings
from ninja_extra.constants import THROTTLED_FUNCTION
from ninja_extra.controllers import ControllerBase, RouteContext
from ninja_extra.dependency_resolver import service_resolver

from .model import BaseThrottle


def throttle(*func_or_throttle_classes: Any, **init_kwargs: Any) -> Callable[..., Any]:
    isfunction = (
        inspect.isfunction(func_or_throttle_classes[0])
        if len(func_or_throttle_classes) == 1
        else False
    )

    if isfunction:
        func = func_or_throttle_classes[0]
        throttle_classes: List[Type[BaseThrottle]] = settings.THROTTLE_CLASSES
        return _inject_throttling(func, *throttle_classes, **init_kwargs)

    def wrapper(view_func: Callable[..., Any]) -> Any:
        return _inject_throttling(view_func, *func_or_throttle_classes, **init_kwargs)

    return wrapper


def _request_or_controller_to_request(request_or_controller: Union[HttpRequest, ControllerBase]):
    return cast(
        HttpRequest,
        (
            request_or_controller.context.request  # type:ignore
            if isinstance(request_or_controller, ControllerBase)
            else request_or_controller
        ),
    )


def _run_throttles(
    *throttle_classes: Type[BaseThrottle],
    request: HttpRequest,
    response: HttpResponse = None,
    user=None,
    **init_kwargs: Any,
) -> None:
    """
    Run all throttles for a request.
    Raises an appropriate exception if the request is throttled.
    """
    throttle_durations = []

    for throttle_class in throttle_classes:
        throttling: BaseThrottle = throttle_class(user=user, **init_kwargs)
        if not throttling.allow_request(request):
            # Filter out `None` values which may happen in case of config / rate
            duration = throttling.wait()
            if duration is not None:
                throttle_durations.append(duration)

    if throttle_durations:
        duration = max(throttle_durations, default=None)
        raise exceptions.Throttled(duration)


def _inject_throttling(
    func: Callable[..., Any],
    *throttle_classes: Type[BaseThrottle],
    **init_kwargs: Any,
) -> Callable[..., Any]:
    setattr(func, THROTTLED_FUNCTION, True)
    if is_async(func):
        return _async_inject_throttling_handler(func, *throttle_classes, **init_kwargs)
    return _sync_inject_throttling_handler(func, *throttle_classes, **init_kwargs)


def _sync_inject_throttling_handler(
    func: Callable[..., Any],
    *throttle_classes: Type[BaseThrottle],
    **init_kwargs: Any,
) -> Callable[..., Any]:
    @wraps(func)
    def as_view(
        request_or_controller: Union[HttpRequest, ControllerBase], *args: Any, **kw: Any
    ) -> Any:
        ctx = cast(Optional[RouteContext], service_resolver(RouteContext))

        request = _request_or_controller_to_request(request_or_controller)
        user = request.user

        _run_throttles(
            *throttle_classes,
            request=request,
            response=ctx.response if ctx else None,
            user=user,
            **init_kwargs,
        )

        res = func(request_or_controller, *args, **kw)
        return res

    return as_view


def _async_inject_throttling_handler(
    func: Callable[..., Any],
    *throttle_classes: Type[BaseThrottle],
    **init_kwargs: Any,
) -> Callable[..., Any]:
    @wraps(func)
    async def as_view(
        request_or_controller: Union[HttpRequest, ControllerBase], *args: Any, **kw: Any
    ) -> Any:
        from django.contrib.auth.middleware import get_user

        ctx = cast(Optional[RouteContext], service_resolver(RouteContext))

        request = _request_or_controller_to_request(request_or_controller)
        user = await sync_to_async(get_user)(request)

        _run_throttles(
            *throttle_classes,
            request=request,
            response=ctx.response if ctx else None,
            user=user,
            **init_kwargs,
        )

        res = await func(request_or_controller, *args, **kw)
        return res

    return as_view
