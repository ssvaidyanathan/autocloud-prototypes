import inspect
from dataclasses import dataclass, field
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

F = TypeVar("F", bound=Callable[..., Any])


@dataclass
class ToolDefinition:
    """Definition and metadata for a registered tool."""

    name: str
    description: str
    func: Callable[..., Any]
    parameters: Dict[str, Any]
    is_mutation: bool = False
    metadata: Dict[str, Any] = field(default_factory=dict)

    def execute(self, **kwargs: Any) -> Any:
        """Execute the wrapped tool function."""
        return self.func(**kwargs)


class ToolRegistry:
    """Registry managing available agent tools and enforcing read-only constraints."""

    _instance: Optional["ToolRegistry"] = None

    def __init__(self) -> None:
        self._tools: Dict[str, ToolDefinition] = {}

    @classmethod
    def get_default_registry(cls) -> "ToolRegistry":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

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
    """Decorator to register a function as a strictly read-only tool."""
    target_registry = registry or default_registry

    def decorator(fn: F) -> F:
        tool_def = target_registry.register(
            fn,
            name=name,
            description=description,
            is_mutation=False,
            **metadata,
        )
        setattr(fn, "__tool_definition__", tool_def)
        return fn

    if callable(name_or_func):
        return decorator(name_or_func)

    return decorator
