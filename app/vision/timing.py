"""Synchroniser les accélérateurs pour mesurer des durées réelles, en millisecondes."""

from time import perf_counter
from typing import Any


def synchronize_device(torch_module: Any, device: str) -> None:
    """Le CPU est synchrone; CUDA/MPS doivent terminer leurs opérations asynchrones."""
    if str(device).startswith("cuda"):
        torch_module.cuda.synchronize(device)
    elif str(device).startswith("mps"):
        torch_module.mps.synchronize()


def elapsed_ms(started: float) -> float:
    return (perf_counter() - started) * 1000.0
