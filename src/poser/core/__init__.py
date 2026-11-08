"""poser's application core: the values poser reasons about, its ports, and its use cases.

Dependency rule: a module under `poser.core` imports only the standard library, numpy, and other
`poser.core` modules, including under `TYPE_CHECKING` and inside functions.

Every core module belongs to one layer and imports only from the layers allowed to it:

- Model modules define values and pure functions, and import only model modules. Every core
  module is a model module unless it is `ports` or a use case.
- `ports` defines the protocols adapters implement, and imports only model modules.
- Use cases (`loading`, `rendering`, `checking`, `capturing`, `analysis`, in this order) import
  model modules, `ports`, and use cases earlier in the order.
"""
