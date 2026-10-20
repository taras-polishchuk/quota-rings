"""quota-rings: a self-hosted dashboard of AI provider quota windows.

Design rules that this package never breaks:

1. Secrets stay in the server process. The HTTP API returns percentages, reset
   instants and operator-supplied labels only.
2. "Refresh" means *re-read the vendor's numbers and redraw*. It consumes zero
   quota and is never labelled a reset.
3. A vendor quota reset is a different thing entirely. Only providers that
   document one expose it, behind an explicit opt-in, as a confirmed POST.
4. A percentage is never inferred from a plan/price table. If the payload does
   not carry the number, the ring shows nothing.
"""

__version__ = "1.0.0"

__all__ = ["__version__"]
