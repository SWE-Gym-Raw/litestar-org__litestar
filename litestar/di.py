from __future__ import annotations

from inspect import isasyncgenfunction, isclass, isgeneratorfunction
from typing import TYPE_CHECKING, Any

from litestar._signature import SignatureModel
from litestar.exceptions import ImproperlyConfiguredException
from litestar.plugins import DIPlugin, PluginRegistry
from litestar.types import Empty, TypeDecodersSequence
from litestar.utils import ensure_async_callable
from litestar.utils.helpers import unwrap_partial
from litestar.utils.predicates import is_async_callable
from litestar.utils.signature import ParsedSignature
from litestar.utils.warnings import (
    warn_implicit_sync_to_thread,
    warn_sync_to_thread_with_async_callable,
    warn_sync_to_thread_with_generator,
)

if TYPE_CHECKING:
    from litestar.dto import AbstractDTO
    from litestar.types import AnyCallable

__all__ = ("Provide",)


class Provide:
    """Wrapper class for dependency injection"""

    __slots__ = (
        "dependency",
        "has_async_generator_dependency",
        "has_sync_callable",
        "has_sync_generator_dependency",
        "parsed_fn_signature",
        "signature_model",
        "sync_to_thread",
        "use_cache",
        "value",
    )

    dependency: AnyCallable

    def __init__(
        self,
        dependency: AnyCallable | type[Any],
        use_cache: bool = False,
        sync_to_thread: bool | None = None,
    ) -> None:
        """Initialize ``Provide``

        Args:
            dependency: Callable to call or class to instantiate. The result is then injected as a dependency.
            use_cache: Cache the dependency return value. Defaults to False.
            sync_to_thread: Run sync code in an async thread. Defaults to False.
        """
        if not callable(dependency):
            raise ImproperlyConfiguredException("Provider dependency must be a callable value")

        is_class_dependency = isclass(dependency)
        self.has_sync_generator_dependency = isgeneratorfunction(
            dependency if not is_class_dependency else dependency.__call__  # type: ignore[operator]
        )
        self.has_async_generator_dependency = isasyncgenfunction(
            dependency if not is_class_dependency else dependency.__call__  # type: ignore[operator]
        )
        has_generator_dependency = self.has_sync_generator_dependency or self.has_async_generator_dependency

        if has_generator_dependency and use_cache:
            raise ImproperlyConfiguredException(
                "Cannot cache generator dependency, consider using Lifespan Context instead."
            )

        has_sync_callable = is_class_dependency or not is_async_callable(dependency)  # pyright: ignore

        if sync_to_thread is not None:
            if has_generator_dependency:
                warn_sync_to_thread_with_generator(dependency, stacklevel=3)  # type: ignore[arg-type]
            elif not has_sync_callable:
                warn_sync_to_thread_with_async_callable(dependency, stacklevel=3)  # pyright: ignore
        elif has_sync_callable and not has_generator_dependency:
            warn_implicit_sync_to_thread(dependency, stacklevel=3)  # pyright: ignore

        if sync_to_thread and has_sync_callable:
            self.dependency = ensure_async_callable(dependency)  # pyright: ignore
            self.has_sync_callable = False
        else:
            self.dependency = dependency  # pyright: ignore
            self.has_sync_callable = has_sync_callable

        self.sync_to_thread = bool(sync_to_thread)
        self.use_cache = use_cache
        self.value: Any = Empty
        self.parsed_fn_signature: ParsedSignature | None = None
        self.signature_model: type[SignatureModel] | None = None

    def finalize(
        self,
        *,
        plugins: PluginRegistry | None = None,
        signature_namespace: dict[str, Any],
        dependency_keys: set[str],
        data_dto: type[AbstractDTO] | None,
        type_decoders: TypeDecodersSequence,
    ) -> None:
        if self.parsed_fn_signature is None:
            dependency = unwrap_partial(self.dependency)
            plugin: DIPlugin | None = None
            if plugins is not None:
                plugin = next(
                    (p for p in plugins.di if isinstance(p, DIPlugin) and p.has_typed_init(dependency)),
                    None,
                )
            if plugin:
                signature, init_type_hints = plugin.get_typed_init(dependency)
                self.parsed_fn_signature = ParsedSignature.from_signature(signature, init_type_hints)
            else:
                self.parsed_fn_signature = ParsedSignature.from_fn(dependency, signature_namespace)

        if self.signature_model is None:
            self.signature_model = SignatureModel.create(
                dependency_name_set=dependency_keys,
                fn=self.dependency,
                parsed_signature=self.parsed_fn_signature,
                data_dto=data_dto,
                type_decoders=type_decoders,
            )

    async def __call__(self, **kwargs: Any) -> Any:
        """Call the provider's dependency."""

        if self.use_cache and self.value is not Empty:
            return self.value

        if self.has_sync_callable:
            value = self.dependency(**kwargs)
        else:
            value = await self.dependency(**kwargs)

        if self.use_cache:
            self.value = value

        return value

    def __eq__(self, other: Any) -> bool:
        # check if memory address is identical, otherwise compare attributes
        return other is self or (
            isinstance(other, self.__class__)
            and other.dependency == self.dependency
            and other.use_cache == self.use_cache
            and other.value == self.value
        )
