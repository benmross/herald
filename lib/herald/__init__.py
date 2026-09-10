"""Herald — a personal agent runtime.

Nothing in this package calls a model API. It runs the official `claude` CLI as
a subprocess so every token is billed to the user's own subscription. See
`herald.think` for the one place that boundary is crossed.
"""

__version__ = "0.1.6"
