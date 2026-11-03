"""Pure classification and SLO calculation from normalized client measurements."""

__version__ = "0.1.0"

from .evaluation import eligible, evaluate, nearest_rank

__all__ = ["eligible", "evaluate", "nearest_rank"]
