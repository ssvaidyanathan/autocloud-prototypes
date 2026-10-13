import asyncio
import copy
import hashlib
import inspect
import json
import logging
import os
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
    cast,
    get_type_hints,
    overload,
)

logger = logging.getLogger(__name__)

F = TypeVar("F", bound=Callable[..., Any])


def get_pinned_project_id(explicit_project: Optional[str] = None) -> str:
    """Resolve and enforce strictly pinned GCP Project ID.

    The project ID is pinned to the environment variable (GCP_PROJECT /
    PROJECT_ID / GOOGLE_CLOUD_PROJECT) or platform metadata / ADC
    (via resolve_platform_project()). Overriding with an arbitrary mismatched
    project ID is strictly rejected.
    """
    env_proj = (
        os.getenv("GCP_PROJECT") or os.getenv("PROJECT_ID") or os.getenv("GOOGLE_CLOUD_PROJECT")
    )
    if not env_proj:
        try:
            from agent.app.config import resolve_platform_project

            env_proj = resolve_platform_project()
        except Exception:
            env_proj = None

    if not env_proj:
        try:
            from agent.app.config import get_settings

            env_proj = get_settings().gcp_project
        except Exception:
            env_proj = None

    if explicit_project is not None and env_proj is not None:
        if explicit_project.strip() != env_proj.strip():
            raise ValueError(
                f"Project ID override rejected: pinned project is '{env_proj}', "
                f"attempted override with '{explicit_project}'."
            )

    resolved = (explicit_project or env_proj or "").strip()
    if not resolved:
        raise ValueError("No GCP project configured or resolvable.")
    return resolved


class QueryCache:
    """Thread-safe in-memory query cache protected by asyncio.Lock and threading.Lock.

    Key format: sha256(tool_name + sorted_json_args).
    Stores results in memory within an investigation session (no Redis/Firestore).
    """

    def __init__(self) -> None:
        self._cache: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
        self._thread_lock = threading.Lock()

    @staticmethod
    def _json_serial(obj: Any) -> Any:
        """JSON serializer for objects not serializable by default json code."""
        if isinstance(obj, datetime):
            return obj.isoformat()
        if hasattr(obj, "__dict__"):
            return str(obj)
        return str(obj)

    def compute_key(self, tool_name: str, args: Dict[str, Any]) -> str:
        """Compute sha256(tool_name + sorted_json_args).

        Excludes non-query execution parameters like 'client' object.
        """
        filtered_args = {k: v for k, v in args.items() if k != "client"}
        sorted_json = json.dumps(
            filtered_args,
            sort_keys=True,
            default=self._json_serial,
            separators=(",", ":"),
        )
        content = f"{tool_name}{sorted_json}".encode("utf-8")
        return hashlib.sha256(content).hexdigest()

    async def get(self, key: str) -> Optional[Any]:
        """Asynchronously retrieve a cached value."""
        async with self._lock:
            with self._thread_lock:
                val = self._cache.get(key)
                return copy.deepcopy(val) if val is not None else None

    async def set(self, key: str, value: Any) -> None:
        """Asynchronously set a value in cache."""
        async with self._lock:
            with self._thread_lock:
                self._cache[key] = copy.deepcopy(value)

    def get_sync(self, key: str) -> Optional[Any]:
        """Synchronously retrieve a cached value."""
        with self._thread_lock:
            val = self._cache.get(key)
            return copy.deepcopy(val) if val is not None else None

    def set_sync(self, key: str, value: Any) -> None:
        """Synchronously set a value in cache."""
        with self._thread_lock:
            self._cache[key] = copy.deepcopy(value)

    async def clear(self) -> None:
        """Asynchronously clear all cached items."""
        async with self._lock:
            with self._thread_lock:
                self._cache.clear()

    def clear_sync(self) -> None:
        """Synchronously clear all cached items."""
        with self._thread_lock:
            self._cache.clear()

    def inspect(self) -> Dict[str, Any]:
        """Inspect current cache contents (shallow view of keys and types)."""
        with self._thread_lock:
            return {
                "total_entries": len(self._cache),
                "keys": list(self._cache.keys()),
            }


# Global default query cache instance
default_query_cache = QueryCache()


def _has_mock_param(kwargs: Dict[str, Any]) -> bool:
    """Check if any argument is a mock object (primarily for hermetic testing)."""
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
    """Definition and metadata for a registered tool."""

    name: str
    description: str
    func: Callable[..., Any]
    parameters: Dict[str, Any]
    is_mutation: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)
    cache: Optional[QueryCache] = None

    def execute(self, **kwargs: Any) -> Any:
        """Execute the wrapped tool function with cache check/set."""
        if inspect.iscoroutinefunction(self.func):
            raise TypeError(
                f"Tool '{self.name}' is an async coroutine function. "
                "Use 'execute_async' instead of 'execute'."
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
        """Asynchronously execute wrapped tool function under asyncio.Lock protection."""
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
        """Register a tool function."""
        if is_mutation:
            raise ValueError(
                f"Attempted to register mutating tool '{func.__name__}'. "
                "Mutations are strictly prohibited in the SRE Agent."
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
        """Retrieve a registered tool by name."""
        return self._tools.get(name)

    def list_tools(self) -> List[ToolDefinition]:
        """List all registered tools."""
        return list(self._tools.values())

    def get_tool_names(self) -> List[str]:
        """List names of all registered tools."""
        return list(self._tools.keys())

    def has_mutating_tools(self) -> bool:
        """Check if any registered tool has is_mutation=True."""
        return any(t.is_mutation for t in self._tools.values())

    def assert_all_readonly(self) -> None:
        """Enforce that 100% of registered tools are read-only."""
        mutating = [t.name for t in self._tools.values() if t.is_mutation]
        if mutating:
            raise AssertionError(f"Mutating tools found in registry: {mutating}")

    def clear(self) -> None:
        """Clear all registered tools (primarily for testing)."""
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

        import functools

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

            wrapper: Any = async_cached_wrapper
        else:

            @functools.wraps(fn)
            def cached_wrapper(*args: Any, **kwargs: Any) -> Any:
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

            wrapper = cached_wrapper

        tool_def = target_registry.register(
            fn,
            name=name,
            description=description,
            is_mutation=False,
            **metadata,
        )
        setattr(wrapper, "__tool_definition__", tool_def)
        return cast(F, wrapper)

    if callable(name_or_func):
        return decorator(name_or_func)

    return decorator
