"""Pure-Python port of the open-source "playfair" FairPlay SAP decryptor.

Port of omg_hax.c / sap_hash.c / modified_md5.c / playfair.c (GPL-3.0, see
licenses/playfair-LICENSE.md). It recovers the 16-byte AES session key that an
AirPlay sender transmits FairPlay-encrypted (the 72-byte "ekey") during SETUP.
Speed is irrelevant: it runs once per mirroring session (~0.1 s).
"""
import math
import struct

from . import _tables as T
from ._cint import UCharBuf
from ._garble import garble

M32 = 0xFFFFFFFF


# ---------------------------------------------------------------- helpers
def _xor16(a, b):
    return bytearray(x ^ y for x, y in zip(a[:16], b[:16]))


def z_xor(block):
    return _xor16(block, T.Z_KEY)


def x_xor(block):
    return _xor16(block, T.X_KEY)


def _words(b):
    return list(struct.unpack("<4I", bytes(b[:16])))


def _put_word(b, k, w):
    b[4 * k:4 * k + 4] = struct.pack("<I", w & M32)


def _rol8(x, n):
    return (((x << n) & 0xFF) | (x >> (8 - n))) & 0xFF


# ---------------------------------------------------------------- AES-like core
def _table_index(i):
    return ((31 * i) % 0x28) << 8


def _message_table_index(i):
    return (97 * i % 144) << 8


def _permute_table_2(i):
    return ((71 * i) % 144) << 8


def _permute_block_1(b):
    s3 = T.TABLE_S3
    b[0] = s3[b[0]]
    b[4] = s3[0x400 + b[4]]
    b[8] = s3[0x800 + b[8]]
    b[12] = s3[0xC00 + b[12]]
    tmp = b[13]
    b[13] = s3[0x100 + b[9]]
    b[9] = s3[0xD00 + b[5]]
    b[5] = s3[0x900 + b[1]]
    b[1] = s3[0x500 + tmp]
    tmp = b[2]
    b[2] = s3[0xA00 + b[10]]
    b[10] = s3[0x200 + tmp]
    tmp = b[6]
    b[6] = s3[0xE00 + b[14]]
    b[14] = s3[0x600 + tmp]
    tmp = b[3]
    b[3] = s3[0xF00 + b[7]]
    b[7] = s3[0x300 + b[11]]
    b[11] = s3[0x700 + b[15]]
    b[15] = s3[0xB00 + tmp]


def _permute_block_2(b, rnd):
    s4 = T.TABLE_S4

    def p(k, v):
        return s4[_permute_table_2(rnd * 16 + k) + v]

    b[0] = p(0, b[0])
    b[4] = p(4, b[4])
    b[8] = p(8, b[8])
    b[12] = p(12, b[12])
    tmp = b[13]
    b[13] = p(13, b[9])
    b[9] = p(9, b[5])
    b[5] = p(5, b[1])
    b[1] = p(1, tmp)
    tmp = b[2]
    b[2] = p(2, b[10])
    b[10] = p(10, tmp)
    tmp = b[6]
    b[6] = p(6, b[14])
    b[14] = p(14, tmp)
    tmp = b[3]
    b[3] = p(3, b[7])
    b[7] = p(7, b[11])
    b[11] = p(11, b[15])
    b[15] = p(15, tmp)


def generate_key_schedule(key_material):
    """Returns 11 rows of 4 uint32 words."""
    buf = _xor16(key_material, T.T_KEY)
    s1 = T.TABLE_S1
    sched = []
    ti = 0
    for rnd in range(11):
        row = [0, 0, 0, 0]
        row[0] = _words(buf)[0]
        t1, t2, t3, t4 = (_table_index(ti + k) for k in range(4))
        ti += 4
        buf[0] ^= s1[t1 + buf[0x0D]] ^ T.INDEX_MANGLE[rnd]
        buf[1] ^= s1[t2 + buf[0x0E]]
        buf[2] ^= s1[t3 + buf[0x0F]]
        buf[3] ^= s1[t4 + buf[0x0C]]
        w = _words(buf)
        row[1] = w[1]
        w[1] ^= w[0]
        row[2] = w[2]
        w[2] ^= w[1]
        row[3] = w[3]
        w[3] ^= w[2]
        for k in range(4):
            _put_word(buf, k, w[k])
        sched.append(row)
    return sched


def cycle(block, sched):
    """In-place on a 16-byte bytearray."""
    s5, s6, s7, s8 = T.TABLE_S5, T.TABLE_S6, T.TABLE_S7, T.TABLE_S8
    w = _words(block)
    for k in range(4):
        _put_word(block, k, w[k] ^ sched[10][k])
    _permute_block_1(block)
    for rnd in range(9):
        keys = [struct.pack("<I", sched[9 - rnd][k]) for k in range(4)]
        k0 = keys[0]
        ab = s5[block[3] ^ k0[3]] ^ s6[block[2] ^ k0[2]] ^ s8[block[0] ^ k0[0]] ^ s7[block[1] ^ k0[1]]
        _put_word(block, 0, ab)
        k1 = keys[1]
        ab = s6[block[6] ^ k1[2]] ^ s5[block[7] ^ k1[3]] ^ s8[block[4] ^ k1[0]] ^ s7[block[5] ^ k1[1]]
        _put_word(block, 1, ab)
        k2 = keys[2]
        _put_word(block, 2, s5[block[11] ^ k2[3]] ^ s6[block[10] ^ k2[2]] ^ s7[block[9] ^ k2[1]] ^ s8[block[8] ^ k2[0]])
        k3 = keys[3]
        _put_word(block, 3, s5[block[15] ^ k3[3]] ^ s6[block[14] ^ k3[2]] ^ s7[block[13] ^ k3[1]] ^ s8[block[12] ^ k3[0]])
        _permute_block_2(block, 8 - rnd)
    w = _words(block)
    for k in range(4):
        _put_word(block, k, w[k] ^ sched[0][k])


def decrypt_message(message_in):
    """decryptMessage(): 164-byte fp-setup message 3 -> 128 bytes."""
    mode = message_in[12]
    if mode not in (0, 1, 2, 3):
        raise ValueError(f"unsupported FairPlay mode {mode}")
    s2, s9, s10 = T.TABLE_S2, T.TABLE_S9, T.TABLE_S10
    mkey = T.MESSAGE_KEY[mode * 144:(mode + 1) * 144]
    miv = T.MESSAGE_IV[mode * 16:(mode + 1) * 16]
    out = bytearray(128)
    for i in range(8):
        if mode == 3:
            buf = bytearray(message_in[0x80 - 0x10 * i:0x80 - 0x10 * i + 16])
        else:
            buf = bytearray(message_in[0x10 * (i + 1):0x10 * (i + 1) + 16])
        for j in range(9):
            base = 0x80 - 0x10 * j

            def m(k, v):
                return s2[_message_table_index(base + k) + v] ^ mkey[base + k]

            buf[0x0] = m(0x0, buf[0x0])
            buf[0x4] = m(0x4, buf[0x4])
            buf[0x8] = m(0x8, buf[0x8])
            buf[0xC] = m(0xC, buf[0xC])
            tmp = buf[0x0D]
            buf[0xD] = m(0xD, buf[0x9])
            buf[0x9] = m(0x9, buf[0x5])
            buf[0x5] = m(0x5, buf[0x1])
            buf[0x1] = m(0x1, tmp)
            tmp = buf[0x02]
            buf[0x2] = m(0x2, buf[0xA])
            buf[0xA] = m(0xA, tmp)
            tmp = buf[0x06]
            buf[0x6] = m(0x6, buf[0xE])
            buf[0xE] = m(0xE, tmp)
            tmp = buf[0x3]
            buf[0x3] = m(0x3, buf[0x7])
            buf[0x7] = m(0x7, buf[0xB])
            buf[0xB] = m(0xB, buf[0xF])
            buf[0xF] = m(0xF, tmp)
            for k in range(4):
                o = 4 * k
                _put_word(buf, k, s9[buf[o]] ^ s9[0x100 + buf[o + 1]] ^ s9[0x200 + buf[o + 2]] ^ s9[0x300 + buf[o + 3]])
        buf[0x0] = s10[(0x0 << 8) + buf[0x0]]
        buf[0x4] = s10[(0x4 << 8) + buf[0x4]]
        buf[0x8] = s10[(0x8 << 8) + buf[0x8]]
        buf[0xC] = s10[(0xC << 8) + buf[0xC]]
        tmp = buf[0x0D]
        buf[0xD] = s10[(0xD << 8) + buf[0x9]]
        buf[0x9] = s10[(0x9 << 8) + buf[0x5]]
        buf[0x5] = s10[(0x5 << 8) + buf[0x1]]
        buf[0x1] = s10[(0x1 << 8) + tmp]
        tmp = buf[0x02]
        buf[0x2] = s10[(0x2 << 8) + buf[0xA]]
        buf[0xA] = s10[(0xA << 8) + tmp]
        tmp = buf[0x06]
        buf[0x6] = s10[(0x6 << 8) + buf[0xE]]
        buf[0xE] = s10[(0xE << 8) + tmp]
        tmp = buf[0x3]
        buf[0x3] = s10[(0x3 << 8) + buf[0x7]]
        buf[0x7] = s10[(0x7 << 8) + buf[0xB]]
        buf[0xB] = s10[(0xB << 8) + buf[0xF]]
        buf[0xF] = s10[(0xF << 8) + tmp]
        if mode in (0, 1, 2):
            src = message_in[0x10 * i:0x10 * i + 16] if i > 0 else miv
            out[0x10 * i:0x10 * i + 16] = _xor16(buf, src)
        else:
            pos = 0x70 - 0x10 * i
            src = message_in[pos:pos + 16] if i < 7 else miv
            out[pos:pos + 16] = _xor16(buf, src)
    return bytes(out)


# ---------------------------------------------------------------- hashes
_MD5_SHIFT = (7, 12, 17, 22) * 4 + (5, 9, 14, 20) * 4 + (4, 11, 16, 23) * 4 + (6, 10, 15, 21) * 4
_MD5_K = [int((1 << 32) * abs(math.sin(i + 1))) & M32 for i in range(64)]


def _rol32(x, n):
    return ((x << n) & M32) | ((x & M32) >> (32 - n))


def modified_md5(block_in, key_in):
    blk = bytearray(block_in[:64])
    key_words = struct.unpack("<4I", bytes(key_in[:16]))
    A, B, C, D = key_words

    def swap(i, j):
        a, b = blk[4 * i:4 * i + 4], blk[4 * j:4 * j + 4]
        blk[4 * i:4 * i + 4], blk[4 * j:4 * j + 4] = b, a

    for i in range(64):
        if i < 16:
            j = i
        elif i < 32:
            j = (5 * i + 1) % 16
        elif i < 48:
            j = (3 * i + 5) % 16
        else:
            j = 7 * i % 16
        inp = (blk[4 * j] << 24 | blk[4 * j + 1] << 16 | blk[4 * j + 2] << 8 | blk[4 * j + 3]) & M32
        Z = (A + inp + _MD5_K[i]) & M32
        if i < 16:
            f = (B & C) | (~B & M32 & D)
        elif i < 32:
            f = (B & D) | (C & ~D & M32)
        elif i < 48:
            f = B ^ C ^ D
        else:
            f = C ^ ((B | (~D & M32)) & M32)
        Z = _rol32((Z + f) & M32, _MD5_SHIFT[i])
        Z = (Z + B) & M32
        A, B, C, D = D, Z, B, C
        if i == 31:
            swap(A & 15, B & 15)
            swap(C & 15, D & 15)
            swap((A & (15 << 4)) >> 4, (B & (15 << 4)) >> 4)
            swap((A & (15 << 8)) >> 8, (B & (15 << 8)) >> 8)
            swap((A & (15 << 12)) >> 12, (B & (15 << 12)) >> 12)
    return struct.pack(
        "<4I",
        (key_words[0] + A) & M32,
        (key_words[1] + B) & M32,
        (key_words[2] + C) & M32,
        (key_words[3] + D) & M32,
    )


def sap_hash(block_in):
    buffer0 = bytearray(T.SAP_BUFFER0)
    buffer1 = bytearray(210)
    buffer2 = bytearray(T.SAP_BUFFER2)
    buffer3 = bytearray(132)
    buffer4 = bytearray(T.SAP_BUFFER4)
    for i in range(210):
        w = (i % 64) >> 2
        buffer1[i] = block_in[4 * w + 3 - (i % 4)]
    for i in range(840):
        x = buffer1[((i - 155) & M32) % 210]
        y = buffer1[((i - 57) & M32) % 210]
        z = buffer1[((i - 13) & M32) % 210]
        w = buffer1[i % 210]
        buffer1[i % 210] = (_rol8(y, 5) + (_rol8(z, 3) ^ w) - _rol8(x, 7)) & 0xFF
    garble(
        UCharBuf(buffer0, track_writes=False),
        UCharBuf(buffer1),
        UCharBuf(buffer2),
        UCharBuf(buffer3, track_writes=True),  # uninitialised in C: must never be read before written
        UCharBuf(buffer4),
    )
    key = [0xE1] * 16
    for i in range(11):
        if i == 3:
            key[i] = 0x3D
        else:
            key[i] = (key[i] + buffer3[T.SAP_I0_INDEX[i] * 4]) & 0xFF
    for i in range(20):
        key[i % 16] ^= buffer0[i]
    for i in range(35):
        key[i % 16] ^= buffer2[i]
    for i in range(210):
        key[i % 16] ^= buffer1[i]
    for _ in range(16):
        for i in range(16):
            x = key[((i - 7) & M32) % 16]
            y = key[i % 16]
            z = key[((i - 37) & M32) % 16]
            w = key[((i - 177) & M32) % 16]
            key[i] = _rol8(x, 1) ^ y ^ _rol8(z, 6) ^ _rol8(w, 5)
    return bytes(key)


def generate_session_key(old_sap, message_in):
    dm = decrypt_message(message_in)
    new_sap = bytearray(320)
    new_sap[0x000:0x011] = T.STATIC_SOURCE_1
    new_sap[0x011:0x091] = dm
    new_sap[0x091:0x111] = old_sap[0x80:0x100]
    new_sap[0x111:0x140] = T.STATIC_SOURCE_2
    session_key = bytes(T.INITIAL_SESSION_KEY)
    for rnd in range(5):
        base = bytes(new_sap[rnd * 64:rnd * 64 + 64])
        md5 = modified_md5(base, session_key)
        sk = sap_hash(base)
        skw = struct.unpack("<4I", sk)
        mdw = struct.unpack("<4I", md5)
        session_key = struct.pack("<4I", *(((a + b) & M32) for a, b in zip(skw, mdw)))
    out = bytearray(session_key)
    for i in range(0, 16, 4):
        out[i], out[i + 3] = out[i + 3], out[i]
        out[i + 1], out[i + 2] = out[i + 2], out[i + 1]
    return bytes(b ^ 121 for b in out)


def playfair_decrypt(message3: bytes, cipher_text: bytes) -> bytes:
    """message3: the 164-byte second fp-setup request; cipher_text: 72-byte ekey."""
    if len(message3) != 164 or len(cipher_text) != 72:
        raise ValueError("bad FairPlay input sizes")
    chunk1 = cipher_text[16:32]
    chunk2 = cipher_text[56:72]
    sap_key = generate_session_key(T.DEFAULT_SAP, message3)
    sched = generate_key_schedule(sap_key)
    block = z_xor(chunk2)
    cycle(block, sched)
    key = _xor16(block, chunk1)
    return bytes(z_xor(x_xor(key)))
