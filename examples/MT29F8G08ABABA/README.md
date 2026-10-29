# MT29F8G08ABABA example

This directory contains the public data-sheet reference and a small synthetic
raw-dump fixture for the `MT29F8G08ABABA` profile.

## Chip layout

- NAND: Micron MT29F8G08ABABA, SLC, x8, 8 Gb.
- Read ID: `2C 38 00 26 85` at address `00h`.
- Physical page: `4096` bytes main data + `224` bytes OOB = `4320` bytes.
- Block: `128` pages.
- Device: `2` planes x `1024` blocks per plane.
- Factory bad-block marker: OOB byte `0` of the first page in a bad block is
  `00h`.
- Minimum ECC requirement: 4-bit ECC per 540 bytes.

The ECC requirement is a correction-strength requirement. It does not identify
the ECC byte offsets produced by a particular programmer or NAND controller.
The FwHandler profile therefore removes the complete OOB area and does not
perform bit-error correction.

## Controller-specific recovery reference

The real dump analyzed for this example uses a controller-specific layout with
interleaved ECC, rather than the datasheet's `4096 + 224` main/OOB split. The
standalone reference script is [`tools/unpack_mt29f8g08.py`](../../tools/unpack_mt29f8g08.py).
It performs BCH16 correction for all eight sectors on each page. The first
sector's BCH message includes the 10-byte page header followed by its 512-byte
data area. The full-dump scan corrected 9,018 sectors and reported zero
unrepairable sectors. See [`RECOVERY_NOTES.md`](RECOVERY_NOTES.md) for details.
The prepared archive is
[`FwHandler-MT29F8G08ABABA-ECC16.zip`](../../dist/FwHandler-MT29F8G08ABABA-ECC16.zip).

## Files

- `MT29F8G08ABABA.pdf` is a public mirror of the Micron data sheet.
  SHA-256: `47476250A989B987BF168DD63AD91772900B2EA2BB7EBA661571872B1EB7020A`
- `MT29F8G08ABABA.synthetic.bin` is a generated, non-device-specific fixture.
  It contains two 128-page blocks. The second block is marked bad so the
  default profile command skips it. Its SHA-256 is
  `F17F8EA600E50309D40D01CD1A123F10102F8D1E1628C7969FF6A03B66CE0E0C`.
- A real service dump is intentionally not included. Public search results
  mostly point to paid television firmware dumps that may contain serial
  numbers, MAC addresses, keys, or other device-specific data.

Data-sheet source:

<https://e2echina.ti.com/cfs-file/__key/communityserver-discussions-components-files/120/MT29F8G08ABABAWPB_2D00_datasheet_2D00_pdf_2D00_www.findic.com.pdf>

Equivalent public mirror:

<https://doc.platan.ru/pdf/datasheets/micron/MT29F8G08ABABA_MICRON.pdf>

Potential real-dump sources found during research, not downloaded or endorsed:

- <https://firmware.dou.pt/downloads/philips-55pfl6007k-12-main-310431365664-firmware-nand/>
- <https://dump-tv.blogspot.com/2018/04/philips-55pfl6678k-12.html>

## Synthetic fixture command

Run from the repository root:

```powershell
python .\Fwhandler.py --profile mt29f8g08ababa `
  -f .\examples\MT29F8G08ABABA\MT29F8G08ABABA.synthetic.bin
```

Expected result: 256 physical pages are read, block `1` is skipped, and the
output contains 128 main-data pages of 4096 bytes each.

The checked-in fixture is synthetic and is not a real device dump. It is
intended only to demonstrate the generic page/OOB profile path, not to test the
controller-specific BCH16 reference script.
