"""Tiny emulation of 32-bit C integer arithmetic (int / unsigned int).

Used only by the translated garble() so that integer promotion, unsigned
wrap-around, truncating division and arithmetic shifts behave exactly like the
original C code compiled for x86/x64 (32-bit int, two's complement).
"""

M32 = 0xFFFFFFFF


class CInt:
    __slots__ = ("v", "u")

    def __init__(self, v: int, unsigned: bool):
        v &= M32
        if not unsigned and v & 0x80000000:
            v -= 1 << 32
        self.v = v
        self.u = unsigned

    # usual arithmetic conversions: if either operand is unsigned int, both are
    def _o(self, o):
        if not isinstance(o, CInt):
            o = L(o)
        return o, (self.u or o.u)

    def __add__(self, o):
        o, u = self._o(o)
        return CInt(self.v + o.v, u)

    def __radd__(self, o):
        return L(o) + self

    def __sub__(self, o):
        o, u = self._o(o)
        return CInt(self.v - o.v, u)

    def __rsub__(self, o):
        return L(o) - self

    def __mul__(self, o):
        o, u = self._o(o)
        return CInt(self.v * o.v, u)

    def __rmul__(self, o):
        return L(o) * self

    def __truediv__(self, o):
        o, u = self._o(o)
        if u:
            return CInt((self.v & M32) // (o.v & M32), True)
        q = abs(self.v) // abs(o.v)
        if (self.v < 0) != (o.v < 0):
            q = -q
        return CInt(q, False)

    def __mod__(self, o):
        o, u = self._o(o)
        if u:
            return CInt((self.v & M32) % (o.v & M32), True)
        q = (self / o).v
        return CInt(self.v - o.v * q, False)

    def __and__(self, o):
        o, u = self._o(o)
        return CInt(self.v & o.v, u)

    __rand__ = __and__

    def __or__(self, o):
        o, u = self._o(o)
        return CInt(self.v | o.v, u)

    __ror__ = __or__

    def __xor__(self, o):
        o, u = self._o(o)
        return CInt(self.v ^ o.v, u)

    __rxor__ = __xor__

    # shifts: result has the (promoted) type of the left operand
    def __lshift__(self, o):
        n = o.v if isinstance(o, CInt) else o
        if not 0 <= n < 32:
            raise ValueError("shift out of range")
        return CInt(self.v << n, self.u)

    def __rshift__(self, o):
        n = o.v if isinstance(o, CInt) else o
        if not 0 <= n < 32:
            raise ValueError("shift out of range")
        if self.u:
            return CInt((self.v & M32) >> n, True)
        return CInt(self.v >> n, False)  # arithmetic shift, as gcc/msvc do

    def __invert__(self):
        return CInt(~self.v, self.u)

    def __neg__(self):
        return CInt(-self.v, self.u)

    def __index__(self):
        if self.v < 0:
            raise IndexError("negative index")
        return self.v

    def __repr__(self):
        return f"CInt({self.v}, {'u' if self.u else 's'})"


def L(n: int) -> CInt:
    """Integer literal: int if it fits, otherwise unsigned int (hex literals)."""
    return CInt(n, n > 0x7FFFFFFF)


def U(x) -> CInt:
    """Conversion to unsigned int (assignment to an `unsigned int` variable)."""
    return CInt(x.v if isinstance(x, CInt) else x, True)


class UCharBuf:
    """unsigned char[] whose elements promote to (signed) int when read."""

    __slots__ = ("b", "written")

    def __init__(self, data, track_writes=False):
        self.b = data  # bytearray, modified in place
        self.written = set() if track_writes else None

    def _i(self, i):
        i = i.v if isinstance(i, CInt) else i
        if not 0 <= i < len(self.b):
            raise IndexError(f"index {i} out of range {len(self.b)}")
        return i

    def __getitem__(self, i):
        i = self._i(i)
        if self.written is not None and i not in self.written:
            raise RuntimeError(f"read of uninitialised element {i}")
        return CInt(self.b[i], False)

    def __setitem__(self, i, v):
        i = self._i(i)
        self.b[i] = (v.v if isinstance(v, CInt) else v) & 0xFF
        if self.written is not None:
            self.written.add(i)


def _u8(x) -> int:
    return (x.v if isinstance(x, CInt) else x) & 0xFF


def _n(x) -> int:
    return x.v if isinstance(x, CInt) else x


def rol8(x, n):
    """unsigned char rol8(unsigned char input, int count)"""
    x, n = _u8(x), _n(n)
    return CInt((((x << n) & 0xFF) | (x >> (8 - n))) & 0xFF, False)


def rol8x(x, n):
    """uint32_t rol8x(unsigned char input, int count)"""
    x, n = _u8(x), _n(n)
    return CInt((x << n) | (x >> (8 - n)), True)


def weird_ror8(x, n):
    """uint32_t weird_ror8(uint8_t input, int count)"""
    x, n = _u8(x), _n(n)
    if n == 0:
        return CInt(0, True)
    return CInt(((x >> n) & 0xFF) | (x << (8 - n)), True)


def weird_rol8(x, n):
    """uint32_t weird_rol8(uint8_t input, int count)"""
    x, n = _u8(x), _n(n)
    if n == 0:
        return CInt(0, True)
    return CInt(((x << n) & 0xFF) | (x >> (8 - n)), True)


def weird_rol32(x, n):
    """uint32_t weird_rol32(uint8_t input, int count)"""
    x, n = _u8(x), _n(n)
    if n == 0:
        return CInt(0, True)
    return CInt((x << n) ^ (x >> (8 - n)), True)
