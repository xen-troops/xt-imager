#!/usr/bin/env python3

import os
import pathlib
import serial
import logging
import argparse
import traceback
from typing import List
from string import printable

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger(__name__)


def main():
    parser = argparse.ArgumentParser(
        description='Flash image files through u-boot and tftp')

    parser.add_argument(
        'image',
        type=pathlib.Path,
        help='Name of the image file to flash')

    parser.add_argument(
        '-s',
        '--serial',
        help='Serial console to use')

    parser.add_argument(
        '-e',
        '--ext_tftp_root',
        type=str,
        required=True,
        help='Root of external TFTP directory, where chunk.bin will be created')

    args = parser.parse_args()

    do_flash_image(args)

def do_flash_image(args):

    log.info(args.image)

    conn = open_connection(args)

    uboot_prompt = "=>"
    # Send 'CR', and check for one of the possible options:
    # - uboot_prompt appears, if u-boot console is already active
    # - u-boot is just starting, so we will get "Hit any key.."
    log.info('Waiting for u-boot prompt...')
    conn_send(conn, "\r")
    conn_wait_for_any(conn, [uboot_prompt, "Hit any key to stop autoboot:"])
    # In case we got "Hit any key", let's stop the boot
    conn_send(conn, "\r")
    conn_wait_for_any(conn, [uboot_prompt])

    image_size = os.path.getsize(args.image)

    ###########
    base_addr = 0x0
    mmc_device = 1
    mmc_part = 0
    mmc_block_size = 512
    ###########

    chunk_filename = "chunk.bin"
    chunk_size_in_bytes = 20*1024*1024

    f_img = open(args.image, "rb")
    bytes_sent = 0
    block_start = base_addr // mmc_block_size
    out_fullname = os.path.join(args.ext_tftp_root, chunk_filename)

    # switch to the required MMC device/partition
    conn_send(conn, f"mmc dev {mmc_device} {mmc_part}\r")
    conn_wait_for_any(conn, [uboot_prompt])

    try:
        # do in loop:
        # - read X MB chunk from image file
        # - save chunk to file in tftp root
        # - tell u-boot to 'tftp-and-emmc' chunk
        while bytes_sent < image_size:
            # create chunk
            data = f_img.read(chunk_size_in_bytes)
            f_out = open(out_fullname, "wb")
            f_out.write(data)
            f_out.close()

            chunk_size_in_blocks = len(data) // mmc_block_size
            if len(data) % mmc_block_size:
                chunk_size_in_blocks += 1

            # instruct u-boot to tftp-and-emmc file
            conn_send(conn, f"tftp 0x48000000 {chunk_filename}\r")
            # check that all bytes are transmitted
            conn_wait_for_any(conn, [f"Bytes transferred = {len(data)}"])
            conn_wait_for_any(conn, [uboot_prompt])

            conn_send(conn, f"mmc write 0x48000000 0x{block_start:X} 0x{chunk_size_in_blocks:X}\r")
            # check that all blocks are written properly
            conn_wait_for_any(conn, [f"{chunk_size_in_blocks} blocks written: OK"])
            conn_wait_for_any(conn, [uboot_prompt])

            bytes_sent += len(data)
            block_start += chunk_size_in_blocks

            print(f"\nProgress: {bytes_sent:_}/{image_size:_} ({bytes_sent * 100 // image_size}%)")
            print("===============================")
    finally:
        # remove chunk from tftp root
        os.remove(out_fullname)

    f_img.close()
    conn.close()

    log.info("Image was flashed successfully.")


def open_connection(args):
    # Default value
    dev_name = '/dev/ttyUSB0'
    if args.serial:
        dev_name = args.serial
    baud = 115200

    log.info(f"Using serial port {dev_name} with baudrate {baud}")
    return serial.Serial(port=dev_name, baudrate=baud, timeout=20)


def conn_wait_for_any(conn, expect: List[str]):
    rcv_str = ""
    # stay in the read loop until any of expected string is received
    # in other words - all expected substrings are not in received buffer
    while all([x not in rcv_str for x in expect]):
        data = conn.read(1)
        if not data:
            raise TimeoutError(f"Timeout waiting for `{expect}` from the device")
        rcv_char = chr(data[0])
        if rcv_char in printable or rcv_char == '\b':
            print(rcv_char, end='', flush=True)
        rcv_str += rcv_char


def conn_send(conn, data):
    conn.write(data.encode("ascii"))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.fatal(e)
        log.fatal(traceback.format_exc())
