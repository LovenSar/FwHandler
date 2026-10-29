# MT29F8G08ABABA real-dump recovery notes

Technical notes from recovering a real 1,132,462,080-byte dump of this chip.
Everything below is method and layout knowledge — no firmware content, no dump
data. The dump itself and anything extracted from it stay local and are never
committed (see `.gitignore`).

These notes exist because the actual dump does **not** use the plain
`4096 + 224` page layout that the data sheet and the `mt29f8g08ababa` profile
assume. The controller interleaves ECC into the page and adds its own header,
so profile-mode OOB removal alone does not recover usable data.

---

## 1. Summary

| Item | Value |
|---|---|
| ECC algorithm | BCH16, GF(2^13), primitive `0x201B` |
| ECC size | 26 bytes per 512 bytes of data |
| Critical detail | data and ECC both bit-reversed per byte |
| Full-chip scan | 2,097,152 sector positions across 262,144 pages |
| Nonblank data sectors | 898,024 |
| Blank sector positions | 1,199,128 |
| Corrected sectors | 9,018 sectors, with 9,074 data or ECC bit errors corrected |
| Unrepairable sectors | 0 |

The recovered layout requires per-byte bit reversal around the BCH engine,
and the first sector on each page includes the 10-byte page header in its BCH
message. The decoder must correct up to 16 bit errors rather than search only
for single-bit flips.

---

## 2. Actual page layout

```
Offset        Length   Content
0..9          10       Page header: [b0][FF][page_no][type][FF x6]
                       page_no = (physical page - 1) & 0xFF
                       type    = 0x40 normal page / 0x48 block-first page
10..4313      4304     8 x (512 data + 26 ECC)
                       block n: data = 10 + n*538 .. 10 + n*538 + 511
                                ECC  = 10 + n*538 + 512 .. 10 + n*538 + 537
4314..4319    6        Zero padding
```

So the page is `10 + 8*538 + 6 = 4320` bytes, **not** `4096 + 224`.

Logical data stream = 8 x 512 = 4,096 bytes per page, 1,073,741,824 bytes total.

### How this was established

The interleaved stride was found by requiring that known content line up across
page boundaries. Once `10 + n*538` is used, an ARM kernel image at a known
offset has its magic at the exact expected position and instructions are
continuous across page boundaries. The stride is also confirmed by the
filesystem block-header interval being an exact multiple of the page count.

---

## 3. ECC algorithm

The controller is an **i.MX6 GPMI + BCH** engine.

```
Algorithm        BCH16
Field            GF(2^13), order 8191
Primitive poly   0x201B   (x^13 + x^4 + x^3 + x + 1)
Generator        g(x) = LCM(m1, m3, ..., m31), degree 208
ECC size         26 bytes per 512 bytes of data
```

### The bit-reversal step

```
ecc = bitrev8( bch_encode( bitrev8(data) ) )
```

`bitrev8` reverses the bit order within each byte. The hardware performs this
on both the input and the output of the BCH engine, so an external
implementation must do the same or it will never match.

This is the kind of detail that is invisible from the data sheet. If a
candidate parameter set produces a valid-length ECC but never matches stored
values, bit reversal is worth testing before discarding the parameter set.

### First sector on each page

The first sector also uses BCH16. Its message is the 10-byte page header
followed by the 512-byte sector data. The 26 bytes after that data are BCH
parity, not an unverified metadata field. This was confirmed by reproducing
the stored ECC with the header included and by scanning all eight sectors on
every page.

### Full-chip verification

The supplied dump contains 2,097,152 sector positions. The scan classifies
1,199,128 positions as blank and 898,024 as data sectors. Of the data sectors,
889,006 already matched their ECC and 9,018 were corrected. The decoder
reported zero unrepairable sectors. This describes the BCH16 scan result and
does not by itself guarantee that every extracted file passes its own format
validation.

---

## 4. Three structural corrections

These are independent of ECC and must be applied even on a clean dump.

### (a) Per-page offset 3904

Throughout the entire dump, the byte at logical offset 3904 of every page reads
`0xFF`. The correct value is the **first byte of that page's header** (physical
offset 0).

Verification: backfilling this byte makes 30 pages of a known reference stream
byte-for-byte identical to the reference. Chip-wide total: **109,106 bytes**
corrected (41.6 % of all pages).

This is easy to miss. A sample-based sanity check can pass while the defect
remains, if the sampled files happen to be shorter than 3,904 bytes and never
reach the affected offset.

### (b) Block ring buffer

Each block is 128 pages. Page 0 holds a filesystem block header (56 bytes of
live fields followed by `0xFF` padding). Data written past the end of a block
**wraps around to immediately after the block header**.

Read order for a file starting at page *P* in block *B*:

```
P, P+1, ..., B*128+127, B*128+1, B*128+2, ...
```

Verification: two known streams of 86,324 and 129,053 bytes match completely
when read in ring order, and fail under plain sequential read.

### (c) Cross-block continuation

A file spanning a block boundary is interrupted by the next block's header.
Continuation reads must **skip page 0 of each subsequent block**.

Verification: corrected 23 ELF files whose section headers previously fell
outside the read window.

---

## 5. Filesystem

Proprietary log-structured format, magic `DL_FS4.00`.

Directory magic: `LDIR` / `LALC`. The full run parsed 210 allocation tables
and 3,330 filenames across 20 directory blocks.

**Directory entry** (20-byte grid):

```
[16-byte name fragment][u32]
```

Names longer than 16 characters span multiple entries. The first entry carries
the file size; subsequent entries carry 0. A fragment that fills all 16 bytes
means "continues"; a fragment containing `0x00` padding means "ends".

**Allocation table entry** (8 bytes, big-endian u16 x4):

```
[u16 pad][u16 file id][u16 page number][u16 sequence]
```

`page_number x 4096` gives the byte offset of file data within the logical
stream. Page numbers are scattered rather than contiguous, which confirms
log-structured storage — files are not stored in contiguous runs.

---

## 6. Method notes for the next unknown dump

Ordered by how much time each step saved.

1. **Verify the dump size against geometry first.** `blocks x pages x page_size`
   should match exactly. A mismatch means the dump is not raw.

2. **Do not assume the data sheet layout.** The data sheet defines the chip's
   physical page; the controller defines how data and ECC are arranged inside
   it. They are different things.

3. **Find the interleaved stride by continuity, not by inspection.** Take a
   region with a known structure (kernel image, filesystem magic) and search
   for the stride that makes it continuous across page boundaries.

4. **A constant byte value across the whole chip is a defect signature.** If
   every page has the same value at the same offset, that offset is not data.
   Look for where the real value is stored elsewhere.

5. **Check the complete ECC message, including metadata inputs.** A sector may
   include page-header bytes in the BCH message. Do not assume its parity bytes
   are metadata just because the other sectors verify without those header bytes.

6. **Test bit reversal early.** If a parameter set gives the right ECC length
   but never matches, try reversing bits within bytes on both input and output
   before abandoning the parameters.

7. **Estimate bit error rate only after scanning every programmed sector.**
   Leaving a sector position out of the scan can distort the estimate and the
   number of reported unrepairable sectors.

8. **Files with self-validation (PNG, ZIP, gzip) are the best ground truth.**
   They let you confirm a read strategy is correct without needing a reference
   copy. Formats without self-validation (raw ELF, plain binaries) cannot tell
   you whether a recovered byte is right.

---

## 7. Relationship to the existing tools

The profile path in `Fwhandler.py` removes the full 224-byte OOB area, which is
correct for a dump stored as `4096 + 224`. For this dump that path is not
applicable: the ECC is interleaved, not appended, so there is no 224-byte OOB
region to strip.

To handle dumps of this shape, a profile would need:

- the interleaved stride (`538` here) and the per-record split (`512 + 26`),
- the page header length (`10` here),
- the per-page correction at logical offset `3904`,
- the block ring-buffer read order,
- the ECC parameters and the bit-reversal step.

A worked reference implementation is available as `unpack_mt29f8g08.py`; it is
self-contained (standard library only, pure-Python BCH16) and documents the
same layout described above. It corrects up to 16 data or ECC bit errors per
sector and includes the page header in the first sector's BCH message.

## 8. Extraction results and limits

The full run extracted 1,876 ELF files, 27 ZIP files, 223 gzip files, 1,544
PNG files, 80 XML files, and 15 SQLite databases. All 223 extracted gzip files
passed decompression checks. A further 29 PNG starts, one ELF start, and four
gzip starts did not pass complete-file validation. These file-level results
are separate from the ECC scan's zero unrepairable sectors.

The allocation-table entry count matches the directory's file count, but the
ordering could not be aligned. Files are therefore recovered by signature
scanning rather than by directory lookup. No U-Boot or SPL image was found in
this dump; it may be stored on a separate medium.
