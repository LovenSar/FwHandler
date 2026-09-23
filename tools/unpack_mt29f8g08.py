#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 MT29F8G08ABABA@TSOP48 NAND dump extraction tool
================================================================================
 Device   : Micron MT29F8G08ABABA, 8 Gb SLC NAND, x8 interface
 Dump size: 1,132,462,080 bytes

 Usage:
     python3 unpack_mt29f8g08.py <dump.bin> [-o OUTDIR] [--ecc] [--dir] [--all]

     --ecc   Full-chip ECC verification and repair (slow but recovers bit-flips)
     --dir   Parse the filesystem directory and allocation tables
     --all   Scan the whole chip and extract every recognizable file

 Dependencies: Python standard library only.
               The BCH16 encoder is implemented in pure Python; no external
               packages such as bchlib are required.

================================================================================
 1. PHYSICAL LAYOUT
================================================================================
 NAND geometry:
     8 Gb SLC, x8 interface
     4096 bytes main area per page
     224 bytes OOB area per page
     128 pages per block
     4320 bytes per complete physical page

 Dump size = 262,144 pages x 4,320 bytes = 1,132,462,080 bytes
           = 2,048 blocks x 128 pages

 Page layout (verified byte-for-byte):

     Offset        Length   Content
     0..9          10       Page header: [b0][FF][page_no][type][FF x6]
                            page_no = (physical page - 1) & 0xFF
                            type    = 0x40 normal page / 0x48 block-first page
     10..4313      4304     8 x (512 data + 26 ECC)
                            block n: data = 10 + n*538 .. 10 + n*538 + 511
                                     ECC  = 10 + n*538 + 512 .. 10 + n*538 + 537
     4314..4319    6        Zero padding

 Logical data stream = 8 x 512 = 4,096 bytes per page
                     = 1,073,741,824 bytes total

================================================================================
 2. THREE STRUCTURAL CORRECTIONS
================================================================================
 (a) Per-page offset 3904

     Throughout the entire dump, the byte at logical offset 3904 of every page
     is 0xFF. The correct value is the FIRST BYTE OF THAT PAGE'S HEADER
     (physical offset 0).

     Verification: 30 pages of a known reference stream become byte-for-byte
     identical to the reference after backfilling.

     Chip-wide total: 109,106 bytes corrected (41.6 % of all pages).

 (b) Block ring buffer

     Each block is 128 pages; page 0 of a block holds a filesystem block header
     (56 bytes of live fields followed by 0xFF padding). Data written past the
     end of a block WRAPS AROUND to immediately after the block header.

     Verification: two known streams of 86,324 and 129,053 bytes match
     completely when read in ring order.

 (c) Cross-block continuation

     A file spanning a block boundary is interrupted by the next block's header.
     Reading must SKIP page 0 of each subsequent block.

     Verification: this corrected 23 ELF files whose section headers previously
     fell outside the read window.

================================================================================
 3. ECC ALGORITHM
================================================================================
 The controller uses an i.MX6 GPMI + BCH engine.

 Parameters:
     Algorithm       BCH16          (16-bit correction capability)
     Field           GF(2^13), order 8191
     Primitive poly  0x201B         (x^13 + x^4 + x^3 + x + 1)
     Generator       g(x) = LCM(m1, m3, ..., m31), degree 208
     ECC size        26 bytes per 512 bytes of data (208 bits)

 CRITICAL IMPLEMENTATION DETAIL:

     Both the data and the ECC must be BIT-REVERSED PER BYTE before being fed
     to a standard BCH encoder:

         ecc = bitrev8( bch_encode( bitrev8(data) ) )

     Without this step, no combination of parameters reproduces the stored ECC.
     This was the single blocking issue in the entire analysis.

 Verification:
     209 of 210 written blocks reproduce the stored ECC exactly. The one
     mismatch is a known corrupted block, and single-bit search corrects it to
     the expected value.

 EXCEPTION - BLOCK 0:

     The first 512-byte block of every page does NOT use BCH16. Its 26 trailing
     bytes are metadata, not ECC: the controller mixes metadata into the ECC
     computation (a common anti-page-swap measure). These bytes cannot be
     verified or repaired with the recovered algorithm. They represent 12.5 % of
     the data volume and are the root cause of all remaining unrecoverable files.

 Full-chip repair result:
     Data-side single-bit corrections : 7,470
     ECC-side single-bit corrections  :    24
     Unrepairable blocks              :   380
     Success rate                     : 95.2 %

     Raw bit error rate is approximately 5.6e-5 per byte (1 in 18,000), which
     indicates NAND aging or read disturb.

================================================================================
 4. FILESYSTEM STRUCTURE
================================================================================
 The filesystem is a proprietary log-structured format (magic "DL_FS4.00").

 Directory magic: "LDIR" / "LALC", found in 21 directory blocks.

 DIRECTORY ENTRY (20-byte grid):
     [16-byte name fragment][u32]

     Names longer than 16 characters span multiple entries. The first entry
     carries the file size; subsequent entries carry 0. A fragment that fills
     all 16 bytes means "continues"; a fragment containing 0x00 padding means
     "ends".

 ALLOCATION TABLE ENTRY (8 bytes, big-endian u16 x4):
     [u16 pad][u16 file id][u16 page number][u16 sequence]

     page_number x 4096 gives the byte offset of file data within the logical
     stream (verified). Page numbers are scattered rather than contiguous,
     confirming log-structured storage.

================================================================================
 5. EXTRACTION RESULTS
================================================================================
     Type      Extracted        Success rate
     ELF       1,874 / 1,877    99.8 %
     ZIP          27 / 27      100 %
     gzip        234 / 237      98.7 %
     PNG       1,483 / 1,573    94.3 %
     XML              80        -
     SQLite           15        -
     locale          213        -

     Total payload approximately 542 MB.

================================================================================
"""

import sys
import os
import struct
import zlib
import argparse
import json

# ============================================================================
#  Constants
# ============================================================================
PAGE_RAW   = 4320                       # physical page size
PAGE_LOG   = 4096                       # logical page size
BLK_PAGES  = 128                        # pages per block
BLK_SIZE   = BLK_PAGES * PAGE_LOG       # 524288
DATA_OFF   = 10                         # start of data area within a page
BLK_STRIDE = 538                        # per-block stride within a page (512 data + 26 ECC)
DATA_LEN   = 512
ECC_LEN    = 26
FIX_OFFSET = 3904                       # logical offset requiring correction

EXPECTED_DUMP_SIZE = 1132462080


# ============================================================================
#  ECC: pure-Python BCH16 (GF(2^13), primitive polynomial 0x201B)
# ============================================================================
class BCH16:
    """
    Equivalent implementation of the i.MX6 GPMI BCH16 ECC.

    A standard BCH encoder shifts the data left by 208 bits and reduces it
    modulo the generator polynomial. The i.MX6 hardware additionally performs a
    per-byte bit reversal both before and after the BCH engine, so externally:

        ecc = bitrev8( bch_encode( bitrev8(data) ) )

    The generator polynomial is g(x) = LCM(m1(x), m3(x), ..., m31(x)) with
    degree 208, where mi(x) is the minimal polynomial of alpha^i over GF(2).
    """

    M        = 13
    PRIM     = 0x201B        # x^13 + x^4 + x^3 + x + 1
    T        = 16            # correction capability in bits
    ORDER    = (1 << M) - 1  # 8191
    ECCBITS  = M * T         # 208
    ECCBYTES = ECCBITS // 8  # 26

    def __init__(self):
        self._bitrev = bytes(int('{:08b}'.format(i)[::-1], 2) for i in range(256))
        self.gen = self._build_generator()

    # ---- GF(2^13) arithmetic ----
    def _mul(self, a, b):
        r = 0
        while b:
            if b & 1:
                r ^= a
            b >>= 1
            a <<= 1
            if a & (1 << self.M):
                a ^= self.PRIM
        return r & self.ORDER

    def _pow(self, a, e):
        r = 1
        while e:
            if e & 1:
                r = self._mul(r, a)
            a = self._mul(a, a)
            e >>= 1
        return r

    # ---- polynomial arithmetic over GF(2), represented as integers ----
    @staticmethod
    def _pmul(p, q):
        r = 0
        while q:
            if q & 1:
                r ^= p
            q >>= 1
            p <<= 1
        return r

    @staticmethod
    def _pmod(a, g):
        gl = g.bit_length() - 1
        while a.bit_length() - 1 >= gl:
            a ^= g << (a.bit_length() - 1 - gl)
        return a

    def _minpoly(self, e):
        """Minimal polynomial of alpha^e over GF(2): product over its conjugates."""
        conj = []
        c = e
        while c not in conj:
            conj.append(c)
            c = (c * 2) % self.ORDER
        poly = [1]
        for c in conj:
            root = self._pow(2, c)
            new = [0] * (len(poly) + 1)
            for i, co in enumerate(poly):
                new[i + 1] ^= co
                new[i] ^= self._mul(co, root)
            poly = new
        r = 0
        for i, co in enumerate(poly):
            if co:
                r |= (1 << i)
        return r

    def _build_generator(self):
        g = 1
        for i in range(1, 2 * self.T, 2):
            g = self._pmul(g, self._minpoly(i))
        assert g.bit_length() - 1 == self.ECCBITS, 'generator degree mismatch'
        return g

    # ---- bit reversal ----
    def _brev(self, b):
        return bytes(self._bitrev[c] for c in b)

    # ---- public interface ----
    def encode(self, data):
        """512 bytes of data -> 26 bytes of ECC."""
        d = self._brev(data)
        a = 0
        for byte in d:
            a = (a << 8) | byte
        a <<= self.ECCBITS
        r = self._pmod(a, self.gen)
        return self._brev(r.to_bytes(self.ECCBYTES, 'big'))

    def check(self, data, ecc):
        """Return True if the ECC matches the data."""
        return self.encode(data) == ecc

    def fix_single_bit(self, data, ecc, max_pos=None):
        """
        Single-bit correction: search for one flipped bit on the data side and
        then on the ECC side such that the ECC matches.

        Returns (corrected_data, 'D'|'E', position, bit) or (None, None, -1, -1).
        """
        if max_pos is None:
            max_pos = len(data)
        # data side
        for pos in range(max_pos):
            orig = data[pos]
            for bit in range(8):
                t = bytearray(data)
                t[pos] = orig ^ (1 << bit)
                if self.encode(bytes(t)) == ecc:
                    return bytes(t), 'D', pos, bit
        # ECC side
        e = self.encode(data)
        for pos in range(len(e)):
            for bit in range(8):
                t = bytearray(e)
                t[pos] ^= (1 << bit)
                if self._brev(bytes(t)) == ecc:
                    return bytes(data), 'E', pos, bit
        return None, None, -1, -1


# ============================================================================
#  NAND reader
# ============================================================================
class NandReader:
    """Reads the dump and produces the logical data stream with corrections applied."""

    def __init__(self, path, apply_fix=True):
        self.f = open(path, 'rb')
        self.size = os.path.getsize(path)
        self.npages = self.size // PAGE_RAW
        self.apply_fix = apply_fix
        self._hdr_cache = {}

    def raw_page(self, p):
        self.f.seek(p * PAGE_RAW)
        return self.f.read(PAGE_RAW)

    def _page_header_byte(self, p):
        if p not in self._hdr_cache:
            self.f.seek(p * PAGE_RAW)
            self._hdr_cache[p] = self.f.read(1)[0]
        return self._hdr_cache[p]

    def logic_page(self, p):
        """Return one logical page (4096 bytes): headers and ECC stripped, correction (a) applied."""
        d = self.raw_page(p)
        out = bytearray()
        for n in range(8):
            base = DATA_OFF + n * BLK_STRIDE
            out += d[base:base + DATA_LEN]
        if self.apply_fix and len(out) > FIX_OFFSET:
            out[FIX_OFFSET] = self._page_header_byte(p)
        return bytes(out)

    # ---- read strategies ----
    def read_plain(self, start_page, length):
        out = bytearray()
        p = start_page
        while len(out) < length and p < self.npages:
            out += self.logic_page(p)
            p += 1
        return bytes(out[:length])

    def read_ring(self, start_page, length):
        """Ring-buffer read within a block; correction (b)."""
        blk = start_page // BLK_PAGES
        base = blk * BLK_PAGES
        out = bytearray()
        p = start_page
        guard = 0
        while len(out) < length and guard < BLK_PAGES:
            out += self.logic_page(p)
            p += 1
            if p > base + BLK_PAGES - 1:
                p = base + 1
            guard += 1
        return bytes(out[:length])

    def read_skipblk(self, start_page, length):
        """Sequential read that skips each block's header page; correction (c)."""
        out = bytearray()
        p = start_page
        while len(out) < length and p < self.npages:
            if p % BLK_PAGES == 0:
                p += 1
                continue
            out += self.logic_page(p)
            p += 1
        return bytes(out[:length])

    def close(self):
        self.f.close()


# ============================================================================
#  File format identifiers
# ============================================================================
class Identifiers:
    """Determine file length by format self-validation."""

    @staticmethod
    def png(buf):
        if buf[:8] != b'\x89PNG\r\n\x1a\n':
            return None
        pos = 8
        while pos + 12 <= len(buf):
            ln = struct.unpack('>I', buf[pos:pos + 4])[0]
            typ = buf[pos + 4:pos + 8]
            if not all(65 <= c <= 90 or 97 <= c <= 122 for c in typ):
                return None
            if ln > len(buf) - pos - 12:
                return None
            data = buf[pos + 8:pos + 8 + ln]
            crc = struct.unpack('>I', buf[pos + 8 + ln:pos + 12 + ln])[0]
            if (zlib.crc32(typ + data) & 0xffffffff) != crc:
                return None
            pos += 12 + ln
            if typ == b'IEND':
                return pos
        return None

    @staticmethod
    def elf(buf):
        if buf[:4] != b'\x7fELF':
            return None
        if len(buf) < 52:
            return None
        e_phoff = struct.unpack('<I', buf[28:32])[0]
        e_shoff = struct.unpack('<I', buf[32:36])[0]
        e_phnum = struct.unpack('<H', buf[44:46])[0]
        e_shentsize = struct.unpack('<H', buf[46:48])[0]
        e_shnum = struct.unpack('<H', buf[48:50])[0]
        end = max(e_shoff + e_shentsize * e_shnum, e_phoff + 32 * e_phnum)
        if end == 0 or end > len(buf):
            return None
        return end

    @staticmethod
    def zip(buf):
        if buf[:4] != b'PK\x03\x04':
            return None
        i = buf.rfind(b'PK\x05\x06')
        if i < 0:
            return None
        return i + 22

    @staticmethod
    def gzip(buf):
        if buf[:3] != b'\x1f\x8b\x08':
            return None
        do = zlib.decompressobj(31)
        try:
            out = do.decompress(buf)
            if do.eof:
                return out
        except Exception:
            pass
        return None

    @staticmethod
    def xml(buf):
        if buf[:5] != b'<?xml':
            return None
        end = buf.rfind(b'>', 0, 262144)
        return end + 1 if end > 0 else None

    @staticmethod
    def sqlite(buf):
        if buf[:16] != b'SQLite format 3\x00':
            return None
        ps = struct.unpack('>H', buf[16:18])[0]
        if ps == 1:
            ps = 65536
        if not (512 <= ps <= 65536):
            return None
        npg = len(buf) // ps
        return npg * ps if npg else None


# ============================================================================
#  Full-chip ECC verification and repair
# ============================================================================
def ecc_scan(reader, bch, fix=True, verbose=True):
    """
    Scan the whole chip and verify every 512-byte block with BCH16.
    Block 0 of each page is skipped: its 26 trailing bytes are metadata, not ECC.

    Returns a list of (page, block, side, position, bit) records.
    """
    fixes = []
    npages = reader.npages
    for p in range(npages):
        d = reader.raw_page(p)
        for blk in range(1, 8):
            base = DATA_OFF + blk * BLK_STRIDE
            data = d[base:base + DATA_LEN]
            ecc = d[base + DATA_LEN:base + DATA_LEN + ECC_LEN]
            if ecc == b'\xff' * ECC_LEN:
                continue
            if bch.check(data, ecc):
                continue
            if not fix:
                fixes.append((p, blk, '?', -1, -1))
                continue
            _, side, pos, bit = bch.fix_single_bit(data, ecc)
            fixes.append((p, blk, side, pos, bit))
        if verbose and p and p % 20000 == 0:
            print('  ECC scan: %d/%d pages, %d records so far' % (p, npages, len(fixes)))
    return fixes


def apply_ecc_fixes(reader, fixes, out_path):
    """Apply the repairs to the logical stream and write a clean data file."""
    by_page = {}
    for p, blk, side, pos, bit in fixes:
        if side == 'D':
            by_page.setdefault(p, []).append((blk, pos, bit))
    n = 0
    with open(out_path, 'wb') as fo:
        for p in range(reader.npages):
            buf = bytearray(reader.logic_page(p))
            for blk, pos, bit in by_page.get(p, []):
                off = blk * DATA_LEN + pos
                if 0 <= off < PAGE_LOG:
                    buf[off] ^= (1 << bit)
                    n += 1
            fo.write(bytes(buf))
    return n


# ============================================================================
#  Filesystem directory parsing
# ============================================================================
DIR_BLOCKS = [388, 421, 539, 616, 670, 678, 700, 722, 828, 838, 840,
              1027, 1030, 1035, 1037, 1038, 1041, 1172, 1404, 1961, 1962]


def parse_ldir_tables(reader):
    """Parse all LDIR allocation tables. Returns {(block, offset): [(file_id, page, seq), ...]}"""
    tables = {}
    for b in DIR_BLOCKS:
        if b * BLK_PAGES >= reader.npages:
            continue
        blk_data = b''.join(reader.logic_page(b * BLK_PAGES + i)
                            for i in range(BLK_PAGES))
        st = 0
        while True:
            k = blk_data.find(b'LDIR', st)
            if k < 0:
                break
            p = k + 24
            ents = []
            while p + 8 <= len(blk_data):
                raw = blk_data[p:p + 8]
                if raw == b'\x00' * 8:
                    break
                w = struct.unpack('>HHHH', raw)
                if w[2] == 0 and w[1] == 0:
                    break
                ents.append((w[1], w[2], w[3]))
                p += 8
            if ents:
                tables[(b, k)] = ents
            st = k + 4
    return tables


def parse_dir_names(reader, blk):
    """Parse filenames from a directory block (20-byte grid)."""
    blk_data = b''.join(reader.logic_page(blk * BLK_PAGES + i)
                        for i in range(BLK_PAGES))
    start = None
    for ph in range(0, len(blk_data) - 400, 4):
        if all(32 <= c < 127 or c == 0 for c in blk_data[ph:ph + 16]) and \
           any(32 <= c < 127 for c in blk_data[ph:ph + 16]):
            cnt = 0
            q = ph
            while q + 20 <= len(blk_data) and cnt < 12:
                nm = blk_data[q:q + 16]
                if all(32 <= c < 127 or c == 0 for c in nm) and any(32 <= c < 127 for c in nm):
                    cnt += 1
                else:
                    break
                q += 20
            if cnt >= 12:
                start = ph
                break
    if start is None:
        return []
    files = []
    cur = b''
    cursz = 0
    p = start
    dead = 0
    while p + 20 <= len(blk_data):
        nm = blk_data[p:p + 16]
        sz = struct.unpack('<I', blk_data[p + 16:p + 20])[0]
        ok = all(32 <= c < 127 or c == 0 for c in nm) and any(32 <= c < 127 for c in nm)
        if not ok:
            dead += 1
            if dead > 2:
                break
            if cur:
                files.append((cur.decode('latin1'), cursz))
                cur = b''
            p += 20
            continue
        dead = 0
        if cur and sz != 0:
            files.append((cur.decode('latin1'), cursz))
            cur = b''
        if not cur:
            cursz = sz
        cur += nm
        if 0 in nm:
            files.append((cur.split(b'\x00')[0].decode('latin1'), cursz))
            cur = b''
        p += 20
    if cur:
        files.append((cur.decode('latin1'), cursz))
    return [x for x in files if x[0] and 2 < len(x[0]) < 120]


# ============================================================================
#  File extraction
# ============================================================================
SIGS = [
    ('png',    b'\x89PNG\r\n\x1a\n',   Identifiers.png),
    ('elf',    b'\x7fELF',             Identifiers.elf),
    ('zip',    b'PK\x03\x04',          Identifiers.zip),
    ('xml',    b'<?xml',               Identifiers.xml),
    ('sqlite', b'SQLite format 3\x00', Identifiers.sqlite),
]

EXT = {'png': '.png', 'elf': '', 'zip': '.zip', 'xml': '.xml', 'sqlite': '.db'}


def extract_all(reader, outdir, verbose=True):
    """Scan the whole chip and extract files by signature."""
    os.makedirs(outdir, exist_ok=True)
    stats = {}
    fails = {}
    for p in range(reader.npages):
        f = reader.f
        f.seek(p * PAGE_RAW)
        head = f.read(16)
        for name, sig, fn in SIGS:
            if not head.startswith(sig):
                continue
            got = None
            for rd in (reader.read_plain, reader.read_ring, reader.read_skipblk):
                buf = rd(p, 16 << 20)
                n = fn(buf)
                if n and n <= len(buf):
                    got = buf[:n]
                    break
            if got:
                with open(os.path.join(outdir, '%s_%06d%s' % (name, p, EXT[name])), 'wb') as fo:
                    fo.write(got)
                stats[name] = stats.get(name, 0) + 1
            else:
                fails[name] = fails.get(name, 0) + 1
        # gzip needs decompression to validate
        if head[:3] == b'\x1f\x8b\x08':
            got = None
            for rd in (reader.read_ring, reader.read_skipblk, reader.read_plain):
                out = Identifiers.gzip(rd(p, 4 << 20))
                if out is not None:
                    got = out
                    break
            if got is not None:
                with open(os.path.join(outdir, 'gzip_%06d.gz' % p), 'wb') as fo:
                    fo.write(got)
                stats['gzip'] = stats.get('gzip', 0) + 1
            else:
                fails['gzip'] = fails.get('gzip', 0) + 1
        if verbose and p and p % 40000 == 0:
            print('  scan: %d/%d pages  %s' % (p, reader.npages, stats))
    return stats, fails


# ============================================================================
#  Main
# ============================================================================
def main():
    ap = argparse.ArgumentParser(
        description='MT29F8G08ABABA NAND dump extraction tool')
    ap.add_argument('dump', help='NAND dump file (1,132,462,080 bytes)')
    ap.add_argument('-o', '--out', default='unpacked', help='output directory')
    ap.add_argument('--ecc', action='store_true',
                    help='full-chip ECC verification and repair (slow)')
    ap.add_argument('--dir', action='store_true',
                    help='parse the filesystem directory tables')
    ap.add_argument('--all', action='store_true',
                    help='scan the whole chip and extract all files')
    ap.add_argument('--no-fix', action='store_true',
                    help='disable the offset-3904 correction (debug only)')
    args = ap.parse_args()

    if not os.path.exists(args.dump):
        print('error: cannot find %s' % args.dump)
        return 1

    size = os.path.getsize(args.dump)
    print('=' * 72)
    print(' MT29F8G08ABABA NAND dump extraction')
    print('=' * 72)
    print(' dump : %s' % args.dump)
    print(' size : %d bytes = %d pages x %d = %d blocks x %d pages' % (
        size, size // PAGE_RAW, PAGE_RAW,
        size // (PAGE_RAW * BLK_PAGES), BLK_PAGES))
    if size != EXPECTED_DUMP_SIZE:
        print(' warning: size differs from expected %d' % EXPECTED_DUMP_SIZE)

    reader = NandReader(args.dump, apply_fix=not args.no_fix)
    os.makedirs(args.out, exist_ok=True)

    # ---- ECC ----
    if args.ecc:
        print()
        print('[1/4] Initialising ECC (BCH16, GF(2^13), primitive 0x201B) ...')
        bch = BCH16()
        print('      generator polynomial degree: %d' % (bch.gen.bit_length() - 1))
        print('      scanning whole chip (block 0 skipped: metadata, not ECC) ...')
        fixes = ecc_scan(reader, bch, fix=True)
        nfix = sum(1 for x in fixes if x[2] in ('D', 'E'))
        nbad = sum(1 for x in fixes if x[2] == '?')
        print('      repaired %d, unrepairable %d' % (nfix, nbad))
        json.dump([[p, b, s, po, bi] for p, b, s, po, bi in fixes],
                  open(os.path.join(args.out, 'ecc_fixes.json'), 'w'))
        clean = os.path.join(args.out, 'data_clean.bin')
        print('      writing clean data stream: %s' % clean)
        n = apply_ecc_fixes(reader, fixes, clean)
        print('      %d bits corrected in the logical stream' % n)

    # ---- directory ----
    if args.dir:
        print()
        print('[2/4] Parsing filesystem directory tables ...')
        tables = parse_ldir_tables(reader)
        print('      LDIR tables: %d' % len(tables))
        allfiles = {}
        for b in DIR_BLOCKS:
            names = parse_dir_names(reader, b)
            if names:
                allfiles[b] = names
        total = sum(len(v) for v in allfiles.values())
        print('      directory blocks: %d, filenames: %d' % (len(allfiles), total))
        json.dump({str(k): v for k, v in allfiles.items()},
                  open(os.path.join(args.out, 'dir_names.json'), 'w'),
                  ensure_ascii=False, indent=1)
        pages = sorted(set(pg for ents in tables.values()
                           for _, pg, _ in ents if pg > 0))
        print('      referenced data pages: %d' % len(pages))
        json.dump(pages, open(os.path.join(args.out, 'ldir_pages.json'), 'w'))

    # ---- extraction ----
    if args.all:
        print()
        print('[3/4] Scanning whole chip and extracting files ...')
        stats, fails = extract_all(reader, os.path.join(args.out, 'files'))
        print('      extracted: %s' % stats)
        print('      failed   : %s' % fails)

    # ---- summary ----
    print()
    print('[4/4] Extraction complete.')
    print('''
  Chip       : Micron MT29F8G08ABABA, 8 Gb SLC NAND
  Page       : 4,320 bytes = 10-byte header + 8 x (512 data + 26 ECC) + 6 pad
  ECC        : BCH16, GF(2^13), primitive 0x201B, 26 bytes per 512 bytes
               data and ECC both bit-reversed per byte
  Filesystem : proprietary log-structured format (magic "DL_FS4.00")

  See README.md for the full technical write-up.
''')
    reader.close()
    print('done. output directory: %s' % args.out)
    return 0


if __name__ == '__main__':
    sys.exit(main())
