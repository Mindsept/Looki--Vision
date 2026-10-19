"""Lossless mask transport between the visual and depth workers; no pickle."""

import io

import numpy as np


def pack_masks(masks, image_size):
    width, height = image_size
    array = np.asarray(masks, dtype=bool).reshape(len(masks), height, width)
    stream = io.BytesIO()
    np.savez_compressed(
        stream,
        bits=np.packbits(array.reshape(len(masks), height * width), axis=1),
        shape=np.array(array.shape),
    )
    return stream.getvalue()


def unpack_masks(data):
    with np.load(io.BytesIO(data), allow_pickle=False) as archive:
        count, height, width = map(int, archive["shape"])
        if not (0 <= count <= 100 and height > 0 and width > 0 and height * width <= 12_000_000):
            raise ValueError("Invalid mask dimensions")
        bits = archive["bits"]
        if bits.shape != (count, (height * width + 7) // 8):
            raise ValueError("Invalid packed mask shape")
        return (
            np.unpackbits(bits, axis=1, count=height * width)
            .reshape(count, height, width)
            .astype(bool)
        )
