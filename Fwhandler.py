import sys
import os
import getopt

ecclen = 0              # bytes
spare = 0              # bytes
unit = 0             # bytes
page = unit + spare     # bytes
block = 0              # pages
plane = 0            # blocks


def helpPrt(level=1):
    print('''
       ___                               _ _           
      / __\_      __/\  /\__ _ _ __   __| | | ___ _ __ 
     / _\ \ \ /\ / / /_/ / _` | '_ \ / _` | |/ _ \ '__|
    / /    \ V  V / __  / (_| | | | | (_| | |  __/ |   
    \/      \_/\_/\/ /_/ \__,_|_| |_|\__,_|_|\___|_|   
    ''')
    if (level == 1):
        print('''
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

MailTo: JaubertLong@gmail.com
Copyright 2022 @ JaubertLong
''')
    exit(0)


def filePreCheck(binFile):
    try:
        fd = open(binFile, "rb")
        fd.close()
    except BaseException as e:
        print("[!] Error Open %s, Please check the path or filename." % binFile)
        exit(0)


def spareHexStatistic(prtStamp):
    print("\nspare Hex value statistics (0xff):")
    for i in range(0, (len(prtStamp) >> 4), 1):
        for j in range(0, 0x10, 1):
            print("0x%06x " % prtStamp[i*0x10 + j], end="")
        print("")
    print("\n")
    pass


def fileHandle(binFile, outputFile, ecclen=ecclen, spare=spare, unit=unit, page=page, block=block, plane=plane, noTmpFile=True, noSkipBad=True, isAllCombo=False):
    totalAreas = plane * block * page
    print("Ecclen = %d, Spare = %d, Unit = %d, Page = %d, Block = %d, Plane = %d" %
          (ecclen, spare, unit, page, block, plane))

    print("binFile = %s" % binFile)
    binFileSize = os.path.getsize(binFile)
    print("Get File Size = %d bytes" % binFileSize)
    if (binFileSize != totalAreas) and (ecclen != 0):
        print("The preset size does not match the specified file size")
    handleIndex = 0
    timesIndex = 0
    if (ecclen != 0) and (spare != 0):
        tmpFile = binFile + ".fwtmp"
    if (ecclen == 0) and (spare != 0):
        tmpFile = outputFile
    if (ecclen != 0) and (spare == 0):
        tmpFile = binFile
    if (spare != 0):
        ffStamp = [0 for _ in range(spare+1)]
        with open(file=binFile, mode="rb") as fd:
            with open(file=tmpFile, mode="wb") as fdOutput:
                index = 0
                print("OutputFile: %s" % tmpFile)
                while(fd.tell() < binFileSize):
                    ub = fd.read(unit)      # unit binary
                    sb = fd.read(spare)     # spare binary
                    timesIndex = unit + spare
                    handleIndex = handleIndex + timesIndex
                    if (isAllCombo == False):
                        sh0, sh1, sh2 = sb[0], sb[1], sb[2]
                        if ((sh0, sh1) == (0xff, 0xff)):
                            for i in range(0, spare, 1):
                                if(sb[i] == 0xff):
                                    ffStamp[i] = ffStamp[i] + 1
                                    pass
                        else:
                            zeroStatic0 = 0
                            for checkNoZero in ub:
                                if (checkNoZero != 0):
                                    break
                                else:
                                    zeroStatic0 = zeroStatic0 + 1
                            zeroStatic1 = 0
                            for checkNoZero in sb:
                                if (checkNoZero != 0):
                                    break
                                else:
                                    zeroStatic1 = zeroStatic1 + 1
                            if (zeroStatic0 == int(unit)) and ((zeroStatic1 == int(spare))):
                                print("[!] Bad page at offset 0x%x" % (
                                    fd.tell() - int(unit) - int(spare)), end="")
                                if (noSkipBad == True):
                                    print("")
                                    pass
                                else:
                                    print(", Skipped.")
                                    continue
                    index = index + 1
                    fdOutput.write(ub)
                    if(ecclen != 0):
                        fdOutput.write(sb[2:34])
                        pass
                if (ecclen != 0):
                    spareHexStatistic(ffStamp)
                    if (ffStamp[0] != ffStamp[1]) or ((ffStamp[0] * (unit + spare)) != binFileSize):
                        isAllCombo = False
                        print("Exist bad block.")
                    else:
                        isAllCombo = True
                        print("All Combo.")
                    pass
    if (ecclen != 0):
        if (spare != 0):
            unit = pow(2, ecclen+1)
        fileHandle(binFile=tmpFile, outputFile=outputFile, ecclen=0, spare=ecclen, unit=unit,
                   page=page, block=block, plane=plane, noTmpFile=noTmpFile, noSkipBad=noSkipBad, isAllCombo=isAllCombo)
    print(noTmpFile)
    if(noTmpFile == True):
        try:
            os.unlink(binFile + ".fwtmp")
        except:
            pass
    pass


def parsePara(argv):
    global spare, unit, page, block, plane, ecclen
    try:
        options, args = getopt.getopt(argv, "hf:e:s:u:p:b:P:n", [
                                      "help", "file=", "ecclen=", "spare=", "unit=", "page=", "block=", "plane=", "tmp", "noskipbad"])
        # print("argc =", len(options))
        # print(options)
        if (len(options) == 0):
            helpPrt()
    except getopt.GetoptError:
        print("FwHandler: getopt Fail.")
    pass
    try:
        for option, value in options:
            if option in ("-h", "--help"):
                helpPrt()
            if option in ("-f", "--file"):
                binFile = format(value)
                filePreCheck(binFile)
                pass
            if option in ("-s", "--spare"):
                spare = int(format(value))
                pass
            if option in ("-u", "--unit"):
                unit = int(format(value))
                pass
            if option in ("-p", "--page"):
                page = int(format(value))
                pass
            if option in ("-b", "--block"):
                block = int(format(value))
                pass
            if option in ("-P", "--plane"):
                plane = int(format(value))
                pass
            if option in ("-e", "--ecclen"):
                ecclen = int(format(value))
                pass
            if option in ("--tmp"):
                noTmpFile = False
                pass
            else:
                noTmpFile = True
                pass
            if option in ("--noskipbad"):
                noSkipBad = True
                pass
            else:
                noSkipBad = False
                pass
        page = unit + spare
        if (unit == 0):
            print("ERROR: Unit CAN NOT be zero. \nYou can enter the -h (--help) parameter to obtain instructions.\nAborted.")
            exit(0)
        if (spare == 0) and (ecclen == 0):
            print("Spare and ecc cannot be 0 at the same time.\nFor detailed format layout information, please refer to the relevant datasheet.\nAborted.")
            exit(0)
        return binFile, noTmpFile, noSkipBad
    except:
        helpPrt(2)


if __name__ == "__main__":
    binFile, noTmpFile, noSkipBad = parsePara(sys.argv[1:])
    fileHandle(binFile, outputFile=binFile+".fwhd.bin", ecclen=ecclen, spare=spare, unit=unit,
               page=page, block=block, plane=plane, noTmpFile=noTmpFile, noSkipBad=noSkipBad, isAllCombo=False)
