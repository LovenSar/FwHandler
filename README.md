# FwHandler

This tool is used to handle firmware that binwalk cannot unpack directly and is mainly used for **OOB removal, ECC removal, and bad block removal** of raw firmware extracted from **Nand Flash**. 

# Usage

```bash
-h, --help          Print this information
-f, --file          Choose a binary file to handler
-e, --ecclen        Error correction code length, the unit is byte, and the 
                    program will automatically eliminate ECC according to the 
                    byte length of error correction code. You can refer to the 
                    chip datasheet for the length of ECC, or you can manually 
                    analyze the firmware. Cannot be 0 at the same time as ecclean
-s, --spare         A number, input the spare bytes. The spare field is generally 
                    used for TLC and MLC. The spare field may not exist in SLC, 
                    and only ecc will be used to ensure data correctness. Cannot be 0 
                    at the same time as spare
-u, --unit          Cannot be 0. Storage unit, available storage stored in one page, 
                    default is 2048 bytes
-p, --page          Number of pages stored in a block
                    1 page = (unit + spare) bytes
-b, --block         Number of blocks stored in a plane. By default, bad blocks are 
                    managed in blocks. 1 block = (unit + spare) bytes * page
-P, --plane         How many planes are there in the whole chip
                    1 plane = (unit + spare) bytes * pages * blocks
--tmp               Save the cache file to facilitate the debugging personnel 
                    to check whether the workflow is correct. Not enabled by default
--noskipbad         Whether the generated final file skips bad blocks. If this 
                    option is enabled, bad blocks will not be skipped. Bad blocks
                    are skipped by default. 
                    The currently supported bad block management mechanism is to 
                    skip bad blocks and place data in the next physically available 
                    block.

TIPS: For detailed format layout information, please refer to the relevant datasheet.
```

## Development environment

- Python 3.8.8rc1 64-bit

By default, output files with the ending of "`.fwhd.bin`" will be output.

If the "`--tmp`" parameter is added, the file ending in "`.fwtmp`" will be output.

Take the firmware in the '`examples`' folder as an example.

In general SLC, because it has very good read and write performance and life, it is less likely to generate bad blocks. Therefore, the OOB bad block management module is canceled at the management level, and only ECC is used to maintain the correctness of data. In the '`TC58BVG0S3HTA00`' chip, through preliminary analysis (combined with datasheet and manual analysis), it can be known that every 512 bytes of data are checked and corrected with 16 bytes of ECC.

```python
python ./Fwhandler.py -f ./TC58BVG0S3HTA00@TSOP48_1338.BIN -e 16 -u 512
```

For general TLC and MLC, there is OOB in raw firmware, and you need to fill in the length in the spare field.

**BE CAREFUL: Spare and ecc cannot be 0 at the same time.**

```python
python ./Fwhandler.py -f ./W29N01HV@TSOP48_3530.BIN -e 8 -s 64 -u 2048 --noskipbad
```

If you want to preserve the intermediate results for the firmware that uses both OOB and ECC, you can enter the `--tmp` parameter.

```python
python ./Fwhandler.py -f ./S34ML01G200TFI00@TSOP48_5332.BIN -e 8 -s 64 -u 2048 -b 64 -P 1024 --tmp
```

# FAQ

- Prompt `The preset size does not match the specified file size` after running?

    Under normal circumstances, the normal size of a file is '`plane * block * page`' (the value includes the number of bytes used for physical hierarchy management), which is used in script processing. The default value of each item is 0. This information will be output when no '`block`' or '`plane`' is specified, or when the product of values does not meet the file size.

- Prompt After Running ` [!] Bad page at offset 0xXXXXXXXXXX, Skipped.`?

    By default, when `FwHandler `encounters a bad block, it will skip, which is not part of the output file, so it will skip. If you do not want to skip bad blocks, you can add the '`--noskipbad`' parameter.

- more Questions...

# TODO

- It is known that when a bad block is detected, it is erased in blocks by default. Therefore, we can design a bad page statistics calculation. Whether the continuous bad blocks are satisfied is a block unit.
- Use logical value/regular expression to guess `ecclen`——This is one of the variables in the script.
- Try to add some commonly used ECC algorithms.
- Solve the problem that -- tmp parameter and -- noskipbad parameter cannot exist at the same time
- Adapt more firmware. Welcome to provide more firmware.