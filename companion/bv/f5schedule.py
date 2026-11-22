"""Coarsen the shipped Euler graph on its own time grid, preserving the endpoint and CFG."""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

# This export has two Gather(time_step) tables: learned embeddings at each time and
# the Euler increment for that interval. Other exports need their own verification.
SOURCE = "6b332c05f060f9b7ac40064aec141ee24e572bc55bc2416839b3ca827dfed526"
BOUNDARIES = {
    4: (0, 1, 2, 6, 10),
    5: (0, 1, 2, 4, 6, 10),  # F5 EPSS [0, 2, 4, 8, 16, 32]
    6: (0, 1, 2, 3, 4, 6, 10),  # F5 EPSS [0, 2, 4, 6, 8, 16, 32] on the shipped 10-step grid
    8: (0, 1, 2, 3, 4, 5, 6, 8, 10),
}


def tables(embeddings, delta, boundaries):
    """Use each retained left endpoint's embedding and the sum of all intervals to the next one."""
    import numpy as np
    widths = [delta[a:b].sum(axis=0, dtype=np.float32) for a, b in zip(boundaries, boundaries[1:])]
    return embeddings[:, boundaries], np.array(widths, dtype=delta.dtype)


def transformer(source: Path, cache: Path, steps: int) -> Path:
    with source.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != SOURCE:
            raise ValueError("неизвестная версия Transformer")
    boundaries = BOUNDARIES[steps]
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / f"{SOURCE[:16]}-{'-'.join(map(str, boundaries))}.onnx"
    if target.is_file():
        return target

    import onnx
    from onnx import numpy_helper

    model = onnx.load(source)
    nodes = {n.name: n for n in model.graph.node}
    embedding = nodes["Constant_254"].attribute[0].t
    delta = nodes["/Constant"].attribute[0].t
    reduced = tables(numpy_helper.to_array(embedding), numpy_helper.to_array(delta), boundaries)
    for tensor, values in zip((embedding, delta), reduced):
        tensor.CopyFrom(numpy_helper.from_array(values, name=tensor.name))

    # Readers only see complete models, even when two companions prepare the same variant.
    with tempfile.NamedTemporaryFile(dir=cache, delete=False) as tmp:
        path = Path(tmp.name)
    try:
        onnx.save(model, path)
        os.replace(path, target)
    finally:
        path.unlink(missing_ok=True)
    return target
