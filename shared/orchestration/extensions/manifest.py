"""Built-in extension modules, imported once by the registry loader.

Each module registers what it provides (graph builders, semantic-slot
providers, …) on import. Adding a tenant extension = adding a module here;
the core engine and router never import a tenant module directly.
"""
BUILTIN_EXTENSIONS: tuple[str, ...] = (
    "shared.orchestration.extensions.reference_flows",
    "shared.orchestration.extensions.mpokket",
    "shared.orchestration.extensions.zepto_mdnd",
)
