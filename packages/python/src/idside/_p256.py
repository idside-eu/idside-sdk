# Patents: https://sdk.idside.eu/patents
# This software may be covered by the patents and applications listed there.

"""ES256 (ECDSA on P-256 with SHA-256) signature *verification*, in pure Python.

Why this exists next to ``verify.py``, which verifies with ``cryptography``:
the prompt-cleaning policy arrives signed, and the SDK must check it on every
install — including ``pip install idside`` without the ``[verify]`` extra, which
deliberately keeps a C extension off the AI request path. Verification needs
no secret and no constant-time arithmetic: every value it handles is public.
It never signs, and there is no signing code here or anywhere in this package.
When ``cryptography`` is installed it does the check instead; this module is
the fallback, and the caller (``_content_policy``) refuses a signature of the
wrong size or a key off the curve before either is asked.

Held by the shared ACT verification vectors (valid and forged signatures) and,
in the test suite, by generated positive and negative cases put to both this
module and ``cryptography`` (high s accepted by both, r or s out of range,
another hash, keys off the curve), and on random keys and messages.
"""

from __future__ import annotations

import hashlib

# FIPS 186-4, D.1.2.3 (secp256r1 / prime256v1).
P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
A = P - 3
B = 0x5AC635D8AA3A93E7B3EBBD55769886BC651D06B0CC53B0F63BCE3C3E27D2604B
N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
G = (
    0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
    0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5,
)

_Point = tuple[int, int, int]  # Jacobian coordinates; Z == 0 is the point at infinity
_INFINITY: _Point = (1, 1, 0)


def on_curve(x: int, y: int) -> bool:
    """Whether (x, y) is a point of P-256 (and not a coordinate out of range)."""
    if not (0 <= x < P and 0 <= y < P):
        return False
    return (y * y - (x * x * x + A * x + B)) % P == 0


def _double(point: _Point) -> _Point:
    x, y, z = point
    if z == 0 or y == 0:
        return _INFINITY
    yy = y * y % P
    s = 4 * x * yy % P
    zz = z * z % P
    m = (3 * (x - zz) * (x + zz)) % P  # a = -3
    x3 = (m * m - 2 * s) % P
    y3 = (m * (s - x3) - 8 * yy * yy) % P
    z3 = 2 * y * z % P
    return x3, y3, z3


def _add(first: _Point, second: _Point) -> _Point:
    x1, y1, z1 = first
    x2, y2, z2 = second
    if z1 == 0:
        return second
    if z2 == 0:
        return first
    z1z1 = z1 * z1 % P
    z2z2 = z2 * z2 % P
    u1 = x1 * z2z2 % P
    u2 = x2 * z1z1 % P
    s1 = y1 * z2 * z2z2 % P
    s2 = y2 * z1 * z1z1 % P
    if u1 == u2:
        return _double(first) if s1 == s2 else _INFINITY
    h = (u2 - u1) % P
    r = (s2 - s1) % P
    hh = h * h % P
    hhh = h * hh % P
    v = u1 * hh % P
    x3 = (r * r - hhh - 2 * v) % P
    y3 = (r * (v - x3) - s1 * hhh) % P
    z3 = h * z1 * z2 % P
    return x3, y3, z3


def _multiply_add(k1: int, p1: tuple[int, int], k2: int, p2: tuple[int, int]) -> _Point:
    """k1·p1 + k2·p2, by Shamir's trick (one shared ladder of doublings)."""
    a: _Point = (p1[0], p1[1], 1)
    b: _Point = (p2[0], p2[1], 1)
    both = _add(a, b)
    result = _INFINITY
    for bit in range(max(k1.bit_length(), k2.bit_length()) - 1, -1, -1):
        result = _double(result)
        take1, take2 = (k1 >> bit) & 1, (k2 >> bit) & 1
        if take1 and take2:
            result = _add(result, both)
        elif take1:
            result = _add(result, a)
        elif take2:
            result = _add(result, b)
    return result


def verify(message: bytes, signature: bytes, x: int, y: int) -> bool:
    """Whether ``signature`` (raw r‖s, 64 bytes) signs ``message`` under (x, y).

    Refuses — returns ``False`` — a signature of the wrong length, an r or s
    outside [1, n-1], and a public key that is not a point of the curve.
    """
    if len(signature) != 64 or not on_curve(x, y):
        return False
    r = int.from_bytes(signature[:32], "big")
    s = int.from_bytes(signature[32:], "big")
    if not (0 < r < N and 0 < s < N):
        return False
    e = int.from_bytes(hashlib.sha256(message).digest(), "big")
    w = pow(s, -1, N)
    point = _multiply_add(e * w % N, G, r * w % N, (x, y))
    px, _, pz = point
    if pz == 0:
        return False
    affine_x = px * pow(pz * pz % P, -1, P) % P
    return affine_x % N == r
