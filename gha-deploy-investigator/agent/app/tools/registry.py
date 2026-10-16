import asyncio
import copy
import functools
import hashlib
import inspect
import json
import logging
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Optional,
    TypeVar,
    get_type_hints,
    overload,
)

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


class QueryCache:
    """Thread-safe in-memory query cache protected by asyncio.Lock and threading.Lock."""

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._thread_lock = threading.Lock()

    @staticmethod
    def _json_serial(obj: Any) -> Any:
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, "__dict__"):
            return str(obj)
        return str(obj)

    def compute_key(self, tool_name: str, args: Dict[str, Any]) -> str:
        filtered_args = {k: v for k, v in args.items() if k not in ("client", "config")}
        sorted_json = json.dumps(
            filtered_args,
            sort_keys=True,
            default=self._json_serial,
            separators=(",", ":"),
        )
        content = f"{tool_name}{sorted_json}".encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    async def get(self, key: str) -> Optional[Any]:
        async with self._lock:
            with self._thread_lock:
                val = self._cache.get(key)
                return copy.deepcopy(val) if val is not None else None

    async def set(self, key: str, value: Any) -> None:
        async with self._lock:
            with self._thread_lock:
                self._cache[key] = copy.deepcopy(value)

    def get_sync(self, key: str) -> Optional[Any]:
        with self._thread_lock:
            val = self._cache.get(key)
            return copy.deepcopy(val) if val is not None else None

    def set_sync(self, key: str, value: Any) -> None:
        with self._thread_lock:
            self._cache[key] = copy.deepcopy(value)

    def clear(self) -> None:
        with self._thread_lock:
            self._cache.clear()

    def inspect(self) -> Dict[str, Any]:
        with self._thread_lock:
            return {
                "total_entries": len(self._cache),
                "keys": list(self._cache.keys()),
            }


default_query_cache = QueryCache()


def _has_mock_param(kwargs: Dict[str, Any]) -> bool:
    for v in kwargs.values():
        if (
            hasattr(v, "_mock_name")
            or hasattr(v, "_mock_return_value")
            or getattr(v.__class__, "__module__", "") == "unittest.mock"
        ):
            return True
    return False


@dataclass
class ToolDefinition:
    """Definition and metadata for a registered read-only tool."""

    name: str
    description: str
    func: Callable[..., Any]
    parameters: Dict[str, Any]
    is_mutation: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    cache: Optional[QueryCache] = None

    def execute(self, **kwargs: Any) -> Any:
        if inspect.iscoroutinefunction(self.func):
            raise TypeError(
                f"Tool '{self.name}' is an async coroutine. Use 'execute_async' instead."
            )
        cache = self.cache or default_query_cache
        if _has_mock_param(kwargs):
            return self.func(**kwargs)

        cache_key = cache.compute_key(self.name, kwargs)
        cached_result = cache.get_sync(cache_key)
        if cached_result is not None:
            logger.debug("Cache hit for tool '%s' (key: %s)", self.name, cache_key[:12])
            return cached_result

        result = self.func(**kwargs)
        cache.set_sync(cache_key, result)
        return result

    async def execute_async(self, **kwargs: Any) -> Any:
        cache = self.cache or default_query_cache
        if _has_mock_param(kwargs):
            if inspect.iscoroutinefunction(self.func):
                return await self.func(**kwargs)
            return self.func(**kwargs)

        cache_key = cache.compute_key(self.name, kwargs)
        cached_result = await cache.get(cache_key)
        if cached_result is not None:
            logger.debug("Cache hit for tool '%s' (key: %s)", self.name, cache_key[:12])
            return cached_result

        if inspect.iscoroutinefunction(self.func):
            result = await self.func(**kwargs)
        else:
            result = self.func(**kwargs)

        await cache.set(cache_key, result)
        return result


class ToolRegistry:
    """Registry managing available agent tools and enforcing read-only constraints."""

    _instance: Optional["ToolRegistry"] = None

    def __init__(self, cache: Optional[QueryCache] = None) -> None:
        self._tools: Dict[str, ToolDefinition] = {}
        self._cache = cache or default_query_cache

    @classmethod
    def get_default_registry(cls) -> "ToolRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    @property
    def cache(self) -> QueryCache:
        return self._cache

    def register(
        self,
        func: Callable[..., Any],
        name: Optional[str] = None,
        description: Optional[str] = None,
        is_mutation: bool = False,
        **metadata: Any,
    ) -> ToolDefinition:
        if is_mutation:
            raise ValueError(
                f"Attempted to register mutating tool '{func.__name__}'. "
                "Mutations are strictly prohibited in the Investigator Agent."
            )

        tool_name = name or func.__name__
        tool_desc = description or (inspect.getdoc(func) or "").strip()

        sig = inspect.signature(func)
        try:
            type_hints = get_type_hints(func)
        except Exception:
            type_hints = {}

        params: Dict[str, Any] = {}
        for param_name, param in sig.parameters.items():
            param_info: Dict[str, Any] = {
                "required": param.default is inspect.Parameter.empty,
            }
            if param_name in type_hints:
                param_type = type_hints[param_name]
                param_info["type"] = getattr(param_type, "__name__", str(param_type))
            if param.default is not inspect.Parameter.empty:
                param_info["default"] = param.default
            params[param_name] = param_info

        tool_def = ToolDefinition(
            name=tool_name,
            description=tool_desc,
            func=func,
            parameters=params,
            is_mutation=False,
            metadata=metadata,
            cache=self._cache,
        )
        self._tools[tool_name] = tool_def
        return tool_def

    def get_tool(self, name: str) -> Optional[ToolDefinition]:
        return self._tools.get(name)

    def list_tools(self) -> List[ToolDefinition]:
        return list(self._tools.values())

    def get_tool_names(self) -> List[str]:
        return list(self._tools.keys())

    def has_mutating_tools(self) -> bool:
        return any(t.is_mutation for t in self._tools.values())

    def assert_all_readonly(self) -> None:
        mutating = [t.name for t in self._tools.values() if t.is_mutation]
        if mutating:
            raise AssertionError(f"Mutating tools found in registry: {mutating}")

    def clear(self) -> None:
        self._tools.clear()


default_registry = ToolRegistry.get_default_registry()


@overload
def readonly_tool(name_or_func: F) -> F: ...


@overload
def readonly_tool(
    name_or_func: None = None,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    registry: Optional[ToolRegistry] = None,
    **metadata: Any,
) -> Callable[[F], F]: ...


def readonly_tool(
    name_or_func: Optional[Any] = None,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    registry: Optional[ToolRegistry] = None,
    **metadata: Any,
) -> Any:
    """Decorator to register a function as a strictly read-only tool with query caching."""
    target_registry = registry or default_registry
    target_cache = target_registry.cache

    def decorator(fn: F) -> F:
        tool_name = name or fn.__name__

        if inspect.iscoroutinefunction(fn):

            @functools.wraps(fn)
            async def async_cached_wrapper(*args: Any, **kwargs: Any) -> Any:
                if _has_mock_param(kwargs):
                    return await fn(*args, **kwargs)

                sig = inspect.signature(fn)
                bound = sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                all_kwargs = bound.arguments

                if _has_mock_param(all_kwargs):
                    return await fn(*args, **kwargs)

                cache_key = target_cache.compute_key(tool_name, all_kwargs)
                cached_result = await target_cache.get(cache_key)
                if cached_result is not None:
                    logger.debug("Cache hit for tool '%s' (key: %s)", tool_name, cache_key[:12])
                    return cached_result

                result = await fn(*args, **kwargs)
                await target_cache.set(cache_key, result)
                return result

            target_registry.register(
                func=async_cached_wrapper,
                name=tool_name,
                description=description,
                is_mutation=False,
                **metadata,
            )
            return cast(F, async_cached_wrapper)

        else:

            @functools.wraps(fn)
            def sync_cached_wrapper(*args: Any, **kwargs: Any) -> Any:
                if _has_mock_param(kwargs):
                    return fn(*args, **kwargs)

                sig = inspect.signature(fn)
                bound = sig.bind_partial(*args, **kwargs)
                bound.apply_defaults()
                all_kwargs = bound.arguments

                if _has_mock_param(all_kwargs):
                    return fn(*args, **kwargs)

                cache_key = target_cache.compute_key(tool_name, all_kwargs)
                cached_result = target_cache.get_sync(cache_key)
                if cached_result is not None:
                    logger.debug("Cache hit for tool '%s' (key: %s)", tool_name, cache_key[:12])
                    return cached_result

                result = fn(*args, **kwargs)
                target_cache.set_sync(cache_key, result)
                return result

            target_registry.register(
                func=sync_cached_wrapper,
                name=tool_name,
                description=description,
                is_mutation=False,
                **metadata,
            )
            return cast(F, sync_cached_wrapper)

    if callable(name_or_func):
        return decorator(name_or_func)
    return decorator


def cast(typ: Any, val: Any) -> Any:
    return val
