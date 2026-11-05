from contextlib import ExitStack, contextmanager
from unittest.mock import patch

@contextmanager
def patch_attr(modules, name, obj):
    with ExitStack() as stack:
        for module in modules:
            stack.enter_context(patch.object(module, name, obj))
        yield
