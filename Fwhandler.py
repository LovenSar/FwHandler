"""Raw NAND firmware layout handling.

The profile path is intentionally limited to physical page parsing. It removes
the complete OOB area and does not guess an ECC byte layout that belongs to a
particular NAND controller or programmer.
"""

from __future__ import print_function

import argparse
import os
import sys
from dataclasses import dataclass, replace


LOGO = r"""       ___                               _ _
      / __\_      __/\  /\__ _ _ __   __| | | ___ _ __
      / _\ \ \ /\ / / /_/ / _` | '_ \ / _` | |/ _ \ '__|
     / /    \ V  V / __  / (_| | | | | (_| | |  __/ |
     \/      \_/\_/\/ /_/ \__,_|_| |_|\__,_|_|\___|_|
"""


class FwHandlerError(ValueError):
    """Raised when the input does not match the selected NAND layout."""


@dataclass(frozen=True)
class NandProfile:
    """Physical geometry and factory bad-block information for one NAND part."""

    name: str
    page_data_size: int
    oob_size: int
    pages_per_block: int
    blocks_per_plane: int
    planes: int
    bad_block_oob_offset: int
    bad_block_marker: int
    ecc_strength_bits: int
    ecc_span_bytes: int

    @property
    def page_size(self):
        return self.page_data_size + self.oob_size

    @property
    def blocks(self):
        return self.blocks_per_plane * self.planes

    @property
    def pages(self):
        return self.blocks * self.pages_per_block

    @property
    def raw_size(self):
        return self.pages * self.page_size

    @property
    def data_size(self):
        return self.pages * self.page_data_size


class LogoArgumentParser(argparse.ArgumentParser):
    """Argument parser that preserves the original help-screen logo."""

    def print_help(self, file=None):
        if file is None:
            file = sys.stdout
        print(LOGO, file=file)
        super().print_help(file)


def helpPrt(level=1):
    """Compatibility help entry point from the original implementation."""

    if level == 1:
        build_parser().print_help()
    else:
        print(LOGO)
    raise SystemExit(0)


def filePreCheck(binFile):
    """Compatibility input-file check."""

    if not os.path.isfile(binFile):
        print("[!] Error Open {}, Please check the path or filename.".format(binFile))
        raise SystemExit(0)
    return True


def spareHexStatistic(prtStamp):
    """Compatibility formatter for per-byte OOB statistics."""

    print("\nspare Hex value statistics (0xff):")
    for offset in range(0, len(prtStamp), 16):
        row = prtStamp[offset : offset + 16]
        print(" ".join("0x{:06x}".format(value) for value in row))
    print("\n")


MT29F8G08ABABA = NandProfile(
    name="mt29f8g08ababa",
    page_data_size=4096,
    oob_size=224,
    pages_per_block=128,
    blocks_per_plane=1024,
    planes=2,
    bad_block_oob_offset=0,
    bad_block_marker=0x00,
    ecc_strength_bits=4,
    ecc_span_bytes=540,
)


PROFILES = {
    "mt29f8g08ababa": MT29F8G08ABABA,
    "mt29f8g08ababawp": MT29F8G08ABABA,
}


def get_profile(name):
    """Return a profile by normalized name."""

    normalized = name.lower().replace("-", "").replace("_", "")
    try:
        return PROFILES[normalized]
    except KeyError:
        available = ", ".join(sorted(PROFILES))
        raise FwHandlerError(
            "Unknown profile '{}'. Available profiles: {}".format(name, available)
        )


def _profile_with_overrides(profile, args):
    """Apply explicit geometry overrides without changing the base profile."""

    overrides = {}
    for argument, field in (
        ("page_data_size", "page_data_size"),
        ("oob_size", "oob_size"),
        ("pages_per_block", "pages_per_block"),
        ("blocks_per_plane", "blocks_per_plane"),
        ("planes", "planes"),
    ):
        value = getattr(args, argument)
        if value is not None:
            overrides[field] = value

    if not overrides:
        return profile
    return replace(profile, **overrides)


def _read_exact(fd, size, description):
    data = fd.read(size)
    if len(data) != size:
        raise FwHandlerError(
            "Unexpected end of file while reading {}: expected {} bytes, got {}".format(
                description, size, len(data)
            )
        )
    return data


def validate_profile_input(bin_file, profile):
    """Validate a raw dump before writing any output bytes."""

    if profile.page_data_size <= 0 or profile.oob_size <= 0:
        raise FwHandlerError("Page data size and OOB size must be positive")
    if profile.pages_per_block <= 0:
        raise FwHandlerError("Pages per block must be positive")
    if profile.blocks_per_plane <= 0 or profile.planes <= 0:
        raise FwHandlerError("Block and plane counts must be positive")
    if not 0 <= profile.bad_block_oob_offset < profile.oob_size:
        raise FwHandlerError("Bad-block OOB offset is outside the OOB area")

    file_size = os.path.getsize(bin_file)
    if file_size == 0:
        raise FwHandlerError("Input file is empty")
    if file_size % profile.page_size != 0:
        raise FwHandlerError(
            "Input size {} is not a multiple of physical page size {} ({} + {}); "
            "the file is not recognized as a {} raw dump".format(
                file_size,
                profile.page_size,
                profile.page_data_size,
                profile.oob_size,
                profile.name,
            )
        )

    page_count = file_size // profile.page_size
    if page_count % profile.pages_per_block != 0:
        raise FwHandlerError(
            "Input contains {} pages, not a whole number of {}-page blocks; "
            "block-aligned input is required when bad-block handling is enabled".format(
                page_count, profile.pages_per_block
            )
        )
    if page_count > profile.pages:
        raise FwHandlerError(
            "Input contains {} pages, exceeding the profile capacity of {} pages".format(
                page_count, profile.pages
            )
        )

    return file_size, page_count


def process_profile(
    bin_file,
    output_file,
    profile,
    skip_bad=True,
    keep_intermediate=False,
):
    """Extract main-area data from a block-aligned physical NAND dump."""

    if os.path.abspath(bin_file) == os.path.abspath(output_file):
        raise FwHandlerError("Input and output files must be different")

    file_size, page_count = validate_profile_input(bin_file, profile)
    block_count = page_count // profile.pages_per_block
    intermediate_file = bin_file + ".fwtmp" if keep_intermediate else None
    bad_blocks = []
    pages_written = 0

    with open(bin_file, "rb") as fd:
        output_fd = open(output_file, "wb")
        intermediate_fd = (
            open(intermediate_file, "wb") if intermediate_file is not None else None
        )
        try:
            for block_index in range(block_count):
                first_page_data = _read_exact(
                    fd, profile.page_data_size, "block {} page 0 data".format(block_index)
                )
                first_page_oob = _read_exact(
                    fd, profile.oob_size, "block {} page 0 OOB".format(block_index)
                )
                first_page = first_page_data + first_page_oob
                is_bad = (
                    first_page_oob[profile.bad_block_oob_offset]
                    == profile.bad_block_marker
                )
                if is_bad:
                    bad_blocks.append(block_index)

                if not is_bad or not skip_bad:
                    output_fd.write(first_page_data)
                    pages_written += 1
                    if intermediate_fd is not None:
                        intermediate_fd.write(first_page)

                for page_index in range(1, profile.pages_per_block):
                    data = _read_exact(
                        fd,
                        profile.page_data_size,
                        "block {} page {} data".format(block_index, page_index),
                    )
                    oob = _read_exact(
                        fd,
                        profile.oob_size,
                        "block {} page {} OOB".format(block_index, page_index),
                    )
                    if not is_bad or not skip_bad:
                        output_fd.write(data)
                        pages_written += 1
                        if intermediate_fd is not None:
                            intermediate_fd.write(data + oob)
        finally:
            output_fd.close()
            if intermediate_fd is not None:
                intermediate_fd.close()

    print("Profile = {}".format(profile.name))
    print(
        "Geometry = {} data + {} OOB bytes/page, {} pages/block, {} blocks/plane, {} planes".format(
            profile.page_data_size,
            profile.oob_size,
            profile.pages_per_block,
            profile.blocks_per_plane,
            profile.planes,
        )
    )
    print(
        "ECC requirement = {}-bit per {} bytes; ECC byte layout is not inferred".format(
            profile.ecc_strength_bits, profile.ecc_span_bytes
        )
    )
    print("Input size = {} bytes ({} physical pages)".format(file_size, page_count))
    print("OutputFile = {} ({} main-data pages)".format(output_file, pages_written))
    if bad_blocks:
        action = "skipped" if skip_bad else "retained"
        print("Bad blocks = {} ({})".format(len(bad_blocks), action))
        print("Bad block indexes = {}".format(", ".join(map(str, bad_blocks))))
    else:
        print("Bad blocks = 0")
    if intermediate_file is not None:
        print("IntermediateFile = {}".format(intermediate_file))

    return {
        "input_size": file_size,
        "page_count": page_count,
        "pages_written": pages_written,
        "bad_blocks": bad_blocks,
        "output_size": pages_written * profile.page_data_size,
        "intermediate_file": intermediate_file,
    }


def _validate_legacy_capacity(page_count, pages_per_block, blocks_per_plane, planes):
    """Validate legacy geometry only when all hierarchy values are supplied."""

    values = (pages_per_block, blocks_per_plane, planes)
    if any(value is not None for value in values) and not all(
        value is not None for value in values
    ):
        raise FwHandlerError(
            "Specify --page, --block, and --plane together for capacity validation"
        )
    if all(value is not None for value in values):
        expected = pages_per_block * blocks_per_plane * planes
        if page_count != expected:
            raise FwHandlerError(
                "Input contains {} legacy units, but the specified geometry requires {}".format(
                    page_count, expected
                )
            )


def process_legacy(
    bin_file,
    output_file,
    ecclen,
    spare,
    unit,
    pages_per_block=None,
    blocks_per_plane=None,
    planes=None,
    skip_bad=True,
    keep_intermediate=False,
):
    """Keep the original generic command-line path for existing examples.

    With an OOB size, the legacy path writes only the main area because it
    cannot safely infer a controller-specific ECC layout. Without OOB, it
    supports the original interleaved ``unit + ecclen`` record format.
    """

    if os.path.abspath(bin_file) == os.path.abspath(output_file):
        raise FwHandlerError("Input and output files must be different")

    if unit <= 0:
        raise FwHandlerError("--unit must be positive")
    if spare < 0 or ecclen < 0:
        raise FwHandlerError("--spare and --ecclen cannot be negative")
    if spare == 0 and ecclen == 0:
        raise FwHandlerError("--spare and --ecclen cannot both be zero")

    if spare:
        record_size = unit + spare
        mode = "page + OOB"
    else:
        record_size = unit + ecclen
        mode = "interleaved data + ECC"

    file_size = os.path.getsize(bin_file)
    if file_size == 0 or file_size % record_size != 0:
        raise FwHandlerError(
            "Input size {} is not a multiple of legacy record size {}".format(
                file_size, record_size
            )
        )

    record_count = file_size // record_size
    _validate_legacy_capacity(
        record_count, pages_per_block, blocks_per_plane, planes
    )

    intermediate_file = bin_file + ".fwtmp" if keep_intermediate else None
    bad_blocks = []
    bad_block_indexes = set()
    records_written = 0

    with open(bin_file, "rb") as fd:
        output_fd = open(output_file, "wb")
        intermediate_fd = (
            open(intermediate_file, "wb") if intermediate_file is not None else None
        )
        try:
            for record_index in range(record_count):
                data = _read_exact(fd, unit, "legacy record {} data".format(record_index))
                extra = _read_exact(
                    fd,
                    record_size - unit,
                    "legacy record {} spare/ECC".format(record_index),
                )
                is_bad = False
                if spare and pages_per_block:
                    block_index = record_index // pages_per_block
                    is_first_page = record_index % pages_per_block == 0
                    if is_first_page and extra[0] == 0x00:
                        bad_block_indexes.add(block_index)
                        bad_blocks.append(block_index)
                    is_bad = block_index in bad_block_indexes

                if not is_bad or not skip_bad:
                    output_fd.write(data)
                    records_written += 1
                    if intermediate_fd is not None:
                        intermediate_fd.write(data + extra)
        finally:
            output_fd.close()
            if intermediate_fd is not None:
                intermediate_fd.close()

    print("Legacy mode = {}".format(mode))
    print("Input size = {} bytes ({} records)".format(file_size, record_count))
    print("OutputFile = {} ({} data records)".format(output_file, records_written))
    if bad_blocks:
        action = "skipped" if skip_bad else "retained"
        print("Bad blocks = {} ({})".format(len(bad_blocks), action))
    if intermediate_file is not None:
        print("IntermediateFile = {}".format(intermediate_file))

    return {
        "input_size": file_size,
        "record_count": record_count,
        "records_written": records_written,
        "bad_blocks": bad_blocks,
        "output_size": records_written * unit,
        "intermediate_file": intermediate_file,
    }


def fileHandle(
    binFile,
    outputFile,
    ecclen=0,
    spare=0,
    unit=2048,
    page=0,
    block=0,
    plane=0,
    noTmpFile=True,
    noSkipBad=True,
    isAllCombo=False,
):
    """Compatibility wrapper for the original ``fileHandle`` entry point.

    ``page`` is interpreted as pages per block, matching the documented
    command-line meaning. ``isAllCombo`` remains accepted for callers of the
    old implementation but is no longer needed by the streaming parser.
    """

    del isAllCombo
    return process_legacy(
        bin_file=binFile,
        output_file=outputFile,
        ecclen=ecclen,
        spare=spare,
        unit=unit,
        pages_per_block=page or None,
        blocks_per_plane=block or None,
        planes=plane or None,
        skip_bad=not noSkipBad,
        keep_intermediate=not noTmpFile,
    )


def build_parser():
    parser = LogoArgumentParser(
        description=(
            "Extract main data from raw NAND dumps. Profile mode removes the "
            "complete OOB area and does not perform ECC correction."
        )
    )
    parser.add_argument("-f", "--file", required=True, help="Input binary dump")
    parser.add_argument(
        "-o", "--output", help="Output file; defaults to <input>.fwhd.bin"
    )
    parser.add_argument(
        "--profile",
        help="NAND profile, currently: mt29f8g08ababa",
    )
    parser.add_argument(
        "-e",
        "--ecclen",
        type=int,
        default=0,
        help=(
            "Legacy ECC byte count. With --profile this value is not used to "
            "infer an ECC layout."
        ),
    )
    parser.add_argument(
        "-s",
        "--spare",
        dest="oob_size",
        type=int,
        default=None,
        help="Legacy OOB/spare bytes per record",
    )
    parser.add_argument(
        "-u",
        "--unit",
        dest="page_data_size",
        type=int,
        default=None,
        help="Main data bytes per page/legacy record",
    )
    parser.add_argument(
        "-p",
        "--page",
        "--pages-per-block",
        dest="pages_per_block",
        type=int,
        default=None,
        help="Pages/records per block",
    )
    parser.add_argument(
        "-b",
        "--block",
        "--blocks-per-plane",
        dest="blocks_per_plane",
        type=int,
        default=None,
        help="Blocks per plane",
    )
    parser.add_argument(
        "-P",
        "--plane",
        "--planes",
        dest="planes",
        type=int,
        default=None,
        help="Planes in the device",
    )
    parser.add_argument(
        "--tmp",
        action="store_true",
        help="Keep retained physical records in <input>.fwtmp",
    )
    parser.add_argument(
        "--noskipbad",
        "--keep-bad-blocks",
        dest="keep_bad_blocks",
        action="store_true",
        help="Retain data from blocks marked bad instead of skipping them",
    )
    return parser


def parse_args(argv):
    args = build_parser().parse_args(argv)
    if not os.path.isfile(args.file):
        raise FwHandlerError("Input file does not exist: {}".format(args.file))
    if args.output is None:
        args.output = args.file + ".fwhd.bin"
    return args


def parsePara(argv):
    """Compatibility parser returning the original three-value tuple."""

    args = parse_args(argv)
    return args.file, not args.tmp, args.keep_bad_blocks


def main(argv=None):
    try:
        args = parse_args(sys.argv[1:] if argv is None else argv)
        if args.profile:
            profile = _profile_with_overrides(get_profile(args.profile), args)
            if args.ecclen:
                print(
                    "Warning: --ecclen is ignored in profile mode; the profile "
                    "does not infer an ECC byte layout."
                )
            process_profile(
                bin_file=args.file,
                output_file=args.output,
                profile=profile,
                skip_bad=not args.keep_bad_blocks,
                keep_intermediate=args.tmp,
            )
        else:
            process_legacy(
                bin_file=args.file,
                output_file=args.output,
                ecclen=args.ecclen,
                spare=args.oob_size or 0,
                unit=args.page_data_size or 2048,
                pages_per_block=args.pages_per_block,
                blocks_per_plane=args.blocks_per_plane,
                planes=args.planes,
                skip_bad=not args.keep_bad_blocks,
                keep_intermediate=args.tmp,
            )
        return 0
    except (FwHandlerError, OSError) as error:
        print("[!] {}".format(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
